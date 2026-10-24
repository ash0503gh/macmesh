"""Run each task in a macOS sandbox, so a task can't harm or snoop on the Mac lending its power.

Inside the sandbox a task:
- has no IP networking (it can't phone home or reach other machines);
- can write only inside its own task folder (plus /dev/null and terminals);
- can't read the owner's home folder, apart from its task folder, the tool's
  own folder (or the .app bundle it's in), ~/Library/Fonts and any paths the
  tool's config names;
- can't read the owner's private temporary and cache folders under
  /var/folders either (other apps' temp files live there).

Everything outside the home folder (system frameworks, /Applications,
Homebrew) stays readable so apps can load. `sandbox-exec` is deprecated by
Apple but still works; it's the only sandbox an unprivileged process can apply
to a program it starts.
"""

from __future__ import annotations

import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from .sysinfo import IS_MAC


SANDBOX_EXEC = "/usr/bin/sandbox-exec"  # by full path: apps and login items start with a bare PATH


def available() -> bool:
    return IS_MAC and Path(SANDBOX_EXEC).exists()


def _q(path: str | Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _real(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


@lru_cache(maxsize=1)
def user_private_dir() -> Path | None:
    """/var/folders/xx/<id>: the owner's own temp (T), cache (C) and per-user (0) folders."""
    try:
        temp = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return None
    return _real(temp).parent if temp else None


def tool_folder(exe: str | Path) -> Path:
    """Where a program keeps what it loads beside itself: its .app bundle, else its folder."""
    exe = _real(exe)
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return exe.parent


def _ancestors(paths: list[Path]) -> list[Path]:
    return sorted({a for p in paths for a in p.parents if a != Path("/")})


def profile(task_dir: Path, exe: str, read: list[str] = (), write: list[str] = ()) -> str:
    home = _real(Path.home())
    task_dir = _real(task_dir)
    readable = [task_dir, tool_folder(exe), home / "Library/Fonts", *(_real(p) for p in read),
                *(_real(p) for p in write)]
    writable = [task_dir, *(_real(p) for p in write)]
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network-outbound (remote ip))",
        "(deny network-inbound (local ip))",
        "(deny network-bind (local ip))",
        "(deny file-write*)",
        "(allow file-write* " + " ".join(f"(subpath {_q(p)})" for p in writable)
        + ' (literal "/dev/null") (literal "/dev/zero") (regex #"^/dev/tty") (regex #"^/dev/fd/"))',
        f"(deny file-read* (subpath {_q(home)}))",
        *([f"(deny file-read* (subpath {_q(user_private_dir())}))"] if user_private_dir() else []),
        # Paths through ~ still resolve without reading ~ itself, so a task can't even list it.
        "(allow file-read* " + " ".join(f"(subpath {_q(p)})" for p in readable) + ")",
        # Tools that walk down to their folders (mkdir -p) need to see that the folders above exist:
        # their metadata only, never what's in them.
        "(allow file-read-metadata " + " ".join(f"(literal {_q(a)})" for a in _ancestors(readable)) + ")",
    ]
    return "\n".join(lines) + "\n"


def wrap(argv: list[str], task_dir: Path, tool_cfg: dict, name: str = "sandbox.sb") -> list[str]:
    """argv run under the sandbox, or unchanged when the tool opts out."""
    rules = tool_cfg.get("sandbox", {})
    if rules is False:
        return argv
    prof = task_dir / name
    # {engine}: macmesh's own Python, which tools installed by a recipe (install.py) run on.
    read = [r.replace("{engine}", sys.prefix) for r in rules.get("read", [])]
    prof.write_text(profile(task_dir, argv[0], read, rules.get("write", [])))
    # Run the real path: the sandbox checks a symlinked one (e.g. .build/release) against ~ too.
    return [SANDBOX_EXEC, "-f", str(prof), str(_real(argv[0])), *argv[1:]]
