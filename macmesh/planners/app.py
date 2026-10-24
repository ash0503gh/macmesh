"""Frame-range jobs for an app described by a profile (see macmesh/profiles.py)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .. import config, mediarefs, profiles, toolinfo
from .range import NEEDS_FPS, _fill, plan_range


def _ask(profile: dict, probe: dict, project: str, vars: dict, cfg: dict, what: str, home: Path | None = None) -> str:
    exe = toolinfo.programs(cfg, home).get(profile["name"])
    if not exe:
        raise ValueError(f"{profile['name']} isn't on this Mac, so it can't report the project's {what}")
    args = [_fill(a, {**vars, "project": str(Path(project).expanduser().resolve())}) for a in probe["args"]]
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"couldn't ask {profile['name']} for the {what} ({e})") from e
    if out.returncode != 0:
        raise ValueError(f"{profile['name']} couldn't report the {what}: {(out.stderr or out.stdout).strip()[-300:]}")
    return out.stdout


def probe_fps(profile: dict, project: str, vars: dict, cfg: dict, home: Path | None = None) -> float:
    """Ask the app on this Mac for the project's frame rate, as its profile says how."""
    probe = profile.get("fps_probe")
    if not probe:
        raise ValueError(f"{profile['name']} needs the frame rate: pass --fps")
    m = re.search(probe["pattern"], _ask(profile, probe, project, vars, cfg, "frame rate", home))
    if not m:
        raise ValueError(f"{profile['name']} didn't say the project's frame rate: pass --fps")
    return float(m.group(1))


def _json_path(text: str, path: str):
    value = json.loads(text)
    for key in path.split("."):
        value = value[key]
    return value


def probe_frames(profile: dict, project: str, vars: dict, cfg: dict, fps: float | None = None,
                 home: Path | None = None) -> int:
    """How many frames the project renders, from the app on this Mac (for a whole-project job)."""
    probe = profile.get("frames_probe")
    if not probe:
        raise ValueError(f"{profile['name']} can't report the project's length: give a frame range")
    text = _ask(profile, probe, project, vars, cfg, "length", home)
    try:
        if "json" in probe:
            return int(_json_path(text, probe["json"]))
        seconds = float(_json_path(text, probe["json_seconds"]))
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"{profile['name']} didn't report the length where its profile says ({e})") from None
    return round(seconds * (fps or probe_fps(profile, project, vars, cfg, home)))


def plan_app(app: str, project: str, start: int, end: int, chunk: int = 100, fps: float | None = None,
             vars: dict | None = None, output: str = "", workdir: str = "", name: str = "",
             cfg: dict | None = None, home: Path | None = None) -> dict:
    cfg = cfg or config.load()
    known = profiles.load(home)
    if app not in known:
        raise ValueError(f"no app profile called {app!r} (have: {', '.join(sorted(known)) or 'none'}); see `macmesh apps`")
    prof = known[app]
    vars = {**prof.get("vars", {}), **(vars or {})}
    rng = prof["range"]
    if fps is None and any(NEEDS_FPS.search(a) for step in rng["steps"] for a in step):
        fps = probe_fps(prof, project, vars, cfg, home)
    media = mediarefs.scan(project) if prof.get("media") == "scan" else []
    return plan_range(app, project, start, end, steps=rng["steps"], chunk=chunk, out_ext=rng.get("out_ext", ""),
                      merge=rng.get("merge", "auto"), output=output, name=name, workdir=workdir, fps=fps,
                      vars=vars, fail_if=prof.get("fail_if"), media=media)
