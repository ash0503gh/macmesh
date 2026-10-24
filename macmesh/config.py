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

# Your own apps. `path` is the CLI binary; `description`/`usage` are shown to
# the AI planner so it knows how to drive the tool.
DEFAULT_TOOLS = {
    "filmcraft": {
        "path": "/Applications/FilmCraft.app/Contents/MacOS/filmcraft",
        "description": "FilmCraft video editor CLI: render/export a timeline.",
        "usage": "filmcraft render <project> --in <frame> --out <frame> --output <file.mov>",
    },
    "effectcraft": {
        "path": "/Applications/EffectCraft.app/Contents/MacOS/effectcraft",
        "description": "EffectCraft motion graphics CLI: render a composition to frames or a movie.",
        "usage": "effectcraft render <project> --comp <name> --start <frame> --end <frame> --output <dir|file>",
    },
    "grade": {
        "path": "/Applications/Grade.app/Contents/MacOS/grade",
        "description": "Grade colour/AI CLI: apply grades, AI subject masks, export.",
        "usage": "grade export <project> --start <frame> --end <frame> --output <file.mov>",
    },
}

DEFAULT_AGENTS = {
    # The planner only needs text in, JSON out: all agent tools are disabled.
    "claude": ["claude", "-p", "{prompt}", "--output-format", "json",
               "--tools", "", "--no-session-persistence"],
    # Antigravity CLI. Some versions hang without a TTY; see README.
    "antigravity": ["agy", "-p", "{prompt}"],
}


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
        "slots": 1,                 # concurrent tasks this node runs
        "nice": 0,                  # raise to keep the Mac responsive while rendering
        "min_battery": 30,          # refuse new work on battery below this %
        "accept_on_battery": True,
        "peers": [],                # static "host:port" entries (skip Bonjour)
        "allowed_programs": list(DEFAULT_ALLOWED),
        "tools": json.loads(json.dumps(DEFAULT_TOOLS)),
        "agent": "claude",
        "agents": json.loads(json.dumps(DEFAULT_AGENTS)),
        "agent_timeout": 300,
    }


def load() -> dict:
    p = config_path()
    if not p.exists():
        raise SystemExit(f"No config at {p}. Run `macmesh init` first.")
    cfg = defaults()
    cfg.update(json.loads(p.read_text()))
    return cfg


def save(cfg: dict) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(tmp, 0o600)  # holds the mesh token
    tmp.replace(p)
    return p
