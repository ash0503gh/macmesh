"""Node configuration, stored at ~/.macmesh/config.json (override with MACMESH_HOME)."""

from __future__ import annotations

import json
import os
import secrets
import socket
import uuid
from pathlib import Path

from . import DEFAULT_PORT

# Programs a worker will execute. argv[0] of every task must be one of these
# names, or a key of the `tools` table below. Anything else is rejected.
# Add interpreters (python3, sh) only if you want the mesh to run scripts.
DEFAULT_ALLOWED = ["ffmpeg", "ffprobe"]

# Extra programs or overrides of app profiles (macmesh/apps/, ~/.macmesh/apps/):
# {"name": {"path": ..., "version_args": [...], "sandbox": {"read": [...], "write": [...]} | false}}.
DEFAULT_TOOLS: dict = {}

# macmesh 0.1 wrote these app entries into every config; they point at the GUI apps, not their
# command-line tools, and would hide the app profiles. load() drops them.
OLD_DEFAULT_PATHS = {"/Applications/FilmCraft.app/Contents/MacOS/filmcraft",
                     "/Applications/EffectCraft.app/Contents/MacOS/effectcraft",
                     "/Applications/Grade.app/Contents/MacOS/grade"}

DEFAULT_AGENTS = {
    # The planner only needs text in, JSON out: all agent tools are disabled.
    "claude": ["claude", "-p", "{prompt}", "--output-format", "json",
               "--tools", "", "--no-session-persistence"],
    # Antigravity CLI: it can't turn its tools off, so it runs with its terminal sandboxed and slash
    # commands off. Not --mode plan: that starts agy's /plan workflow, which waits for someone to
    # review the plan and never ends a non-interactive turn. Low effort answers in ~15 s instead of
    # minutes, and agy's own time limit ends a stuck turn before macmesh's (agent_timeout).
    "antigravity": ["agy", "-p", "{prompt}", "--output-format", "json", "--sandbox", "--disable-slash-commands",
                    "--effort", "low", "--print-timeout", "280s"],
}
# Earlier default commands, replaced on load (a user's own edits are kept).
OLD_AGENTS = {"antigravity": [["agy", "-p", "{prompt}", "--output-format", "json", "--mode", "plan", "--sandbox"]]}


def home() -> Path:
    return Path(os.environ.get("MACMESH_HOME", Path.home() / ".macmesh")).expanduser().resolve()


def config_path() -> Path:
    return home() / "config.json"


def defaults() -> dict:
    return {
        "node_id": uuid.uuid4().hex[:12],
        "node_name": socket.gethostname().split(".")[0],
        "port": DEFAULT_PORT,
        "token": secrets.token_urlsafe(24),
        "lend": True,               # let other Macs on the mesh use this one
        "output_dir": "~/Movies/macmesh",  # where jobs started from the app land
        "slots": 1,                 # concurrent tasks this node runs
        "nice": 0,                  # raise to keep the Mac responsive while rendering
        "priority": "utility",      # macOS QoS clamp for tasks: normal | utility | background
        "min_battery": 30,          # refuse new work on battery below this %
        "accept_on_battery": True,
        "min_free_gb": 5,           # refuse new work when the disk is this full
        # Owner first: pause tasks while this Mac's owner needs it, give them back if it lasts.
        # mode "shared" yields to heavy use, "idle" runs only while nobody touches the Mac,
        # "off" never yields. cpu/gpu are the owner's share of the Mac that counts as heavy use.
        "owner": {"mode": "shared", "idle_seconds": 120,
                  "calm_seconds": 10, "yield_after": 30,
                  "limit": 0.9},  # the whole Mac (owner + macmesh) stays at or under this, CPU and GPU
        "blob_ttl": 1800,           # seconds an unused uploaded file is kept
        "sandbox": True,            # run tasks under sandbox-exec (no network, writes only to the task)
        "tls": True,                # mutual TLS with certificates from the mesh's CA (see `macmesh invite`)
        "revoked": [],              # SHA-256 fingerprints of certificates this Mac refuses
        "peers": [],                # static "host:port" entries (skip Bonjour)
        "allowed_programs": list(DEFAULT_ALLOWED),
        "tools": json.loads(json.dumps(DEFAULT_TOOLS)),
        "agent": "auto",            # claude | antigravity | auto (whichever is installed)
        "ai_autorun": True,         # run AI plans at once when they only use app profiles or ffmpeg
        "agents": json.loads(json.dumps(DEFAULT_AGENTS)),
        "agent_timeout": 300,
    }


def load() -> dict:
    p = config_path()
    if not p.exists():
        raise SystemExit(f"No config at {p}. Run `macmesh init` first.")
    cfg = defaults()
    saved = json.loads(p.read_text())
    cfg["owner"].update(saved.pop("owner", {}))
    cfg.update(saved)
    cfg["tools"] = {name: t for name, t in cfg["tools"].items()
                    if not (t.get("path") in OLD_DEFAULT_PATHS and set(t) <= {"path", "description", "usage"})}
    for name, old in OLD_AGENTS.items():
        if cfg["agents"].get(name) in old:
            cfg["agents"][name] = list(DEFAULT_AGENTS[name])
    return cfg


def save(cfg: dict) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(tmp, 0o600)  # holds the mesh token
    tmp.replace(p)
    return p
