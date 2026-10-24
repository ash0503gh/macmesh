"""Setting up a program another Mac's job needs, once this Mac's owner clicks Allow.

When a job needs a program a lending Mac doesn't have, the Mac running the job asks for it by
name (POST /install). If this Mac's own app profile for that program has an "install" recipe,
the request waits for the owner: the app shows what would be installed, and nothing happens
until they click Allow. The recipe always comes from this Mac's profiles, never from the asking
Mac, so another Mac can't make this one run anything. See profiles.py for the recipe format.

The recipe runs in ~/.macmesh/tools/<name> with macmesh's own Python, without administrator
rights, logging to install.log there. When it finishes, the program is found like any other
profile's, and jobs still running pick this Mac up.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, profiles

STEP_TIMEOUT = 2400.0   # pip installing torch over slow Wi-Fi
FORGET_AFTER = 600.0    # a finished, failed or declined request drops off the list after this
ASK_AGAIN_AFTER = 600.0  # a declined program isn't asked about again for this long
ANSWER_WITHIN = 900.0   # a request nobody answers counts as declined after this


def tool_dir(name: str, home: Path | None = None) -> Path:
    return (home or config.home()) / "tools" / name


def _fill(text: str, values: dict) -> str:
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    return text


class Installer:
    """This Mac's install requests: {program: {"program", "summary", "by", "state", "detail", "error", "at"}}.
    States: asking (waiting for the owner), installing, done, failed, declined."""

    def __init__(self, home: Path | None = None, on_change=None, have=None):
        self.home = home
        self.on_change = on_change or (lambda: None)
        self.have = have or (lambda name: False)  # whether this Mac already has the program
        self.lock = threading.Lock()
        self.requests: dict[str, dict] = {}

    def recipe(self, name: str) -> dict | None:
        prof = profiles.load(self.home).get(name)
        return prof if prof and prof.get("install") else None

    def ask(self, name: str, by: str) -> dict:
        """Another Mac's job needs `name`. Returns the request's state ("installed" if already here)."""
        if self.have(name):
            return {"program": name, "state": "installed"}
        prof = self.recipe(name)
        if not prof:
            return {"program": name, "state": "unavailable",
                    "error": f"this Mac has no install recipe for {name}"}
        with self.lock:
            self._forget_old()
            r = self.requests.get(name)
            if r and r["state"] in ("asking", "installing", "declined"):
                return dict(r)
            self.requests[name] = r = {"program": name, "summary": prof["install"]["summary"],
                                       "description": prof.get("description", ""), "by": by[:64],
                                       "state": "asking", "detail": "", "error": "", "at": time.time()}
        self.on_change()
        return dict(r)

    def list(self) -> list[dict]:
        with self.lock:
            self._forget_old()
            return [dict(r) for r in self.requests.values()]

    def states(self) -> dict[str, str]:
        with self.lock:
            return {k: r["state"] for k, r in self.requests.items()}

    def decide(self, name: str, allow: bool) -> bool:
        with self.lock:
            r = self.requests.get(name)
            if not r or r["state"] != "asking":
                return False
            r.update(state="installing" if allow else "declined", at=time.time())
        if allow:
            threading.Thread(target=self._install, args=(name,), daemon=True).start()
        self.on_change()
        return True

    def _forget_old(self) -> None:
        now = time.time()
        for r in self.requests.values():
            if r["state"] == "asking" and now - r["at"] > ANSWER_WITHIN:
                r.update(state="declined", error="nobody answered", at=now)
        for k, r in list(self.requests.items()):
            wait = ASK_AGAIN_AFTER if r["state"] == "declined" else FORGET_AFTER
            if r["state"] in ("done", "failed", "declined") and now - r["at"] > wait:
                del self.requests[k]

    def _set(self, name: str, **fields) -> None:
        with self.lock:
            self.requests[name].update(fields)
        self.on_change()

    def _install(self, name: str) -> None:
        try:
            self._run_recipe(name)
            self._set(name, state="done", detail="", at=time.time())
        except Exception as e:  # reported to the owner and the asking Mac, never raised
            self._set(name, state="failed", error=str(e)[:500], at=time.time())

    def _run_recipe(self, name: str) -> None:
        prof = self.recipe(name)
        if not prof:
            raise RuntimeError(f"the install recipe for {name} has gone")
        inst = prof["install"]
        d = tool_dir(name, self.home)
        need = float(inst.get("size_gb", 0)) + 1.0
        d.parent.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(d.parent).free / 1e9
        if free < need:
            raise RuntimeError(f"needs about {need:.0f} GB free, this Mac has {free:.0f} GB")
        if d.exists():
            shutil.rmtree(d)  # a half-finished earlier attempt
        d.mkdir(parents=True)
        values = {"dir": str(d), "python": sys.executable, "home": str(Path.home())}
        env = dict(os.environ, PATH=f"{d}/env/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                   PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONNOUSERSITE="1")
        env.pop("PYTHONPATH", None)  # the engine's own packages must not leak into the tool
        with open(d / "install.log", "a") as log:
            for i, step in enumerate(inst["steps"], 1):
                argv = [_fill(a, values) for a in step]
                self._set(name, detail=f"Step {i} of {len(inst['steps'])}: {Path(argv[0]).name} {' '.join(argv[1:3])}"[:120])
                log.write(f"$ {' '.join(argv)}\n")
                log.flush()
                try:
                    rc = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        cwd=d, env=env, timeout=STEP_TIMEOUT).returncode
                except subprocess.TimeoutExpired:
                    raise RuntimeError(f"step {i} took longer than {STEP_TIMEOUT / 60:.0f} minutes") from None
                except OSError as e:
                    raise RuntimeError(f"step {i} couldn't start: {e}") from None
                if rc != 0:
                    tail = (d / "install.log").read_text(errors="replace").strip().splitlines()[-3:]
                    raise RuntimeError(f"step {i} failed (exit {rc}): " + " / ".join(tail))
        # The files go in last: the program (often one of them) must not appear before it works,
        # or a running job would send it chunks mid-install.
        for fname, text in inst.get("files", {}).items():
            f = d / fname
            f.write_text(_fill(text, values))
            if fname in inst.get("executable", []):
                f.chmod(0o755)
        program = Path(os.path.expanduser(prof["program"]["path"]))
        if not (program.is_file() and os.access(program, os.X_OK)):
            raise RuntimeError(f"the recipe finished but {program} isn't there")
