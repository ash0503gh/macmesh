"""Frame-range jobs for an app described by a profile (see macmesh/profiles.py)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .. import config, mediarefs, profiles, toolinfo
from .range import NEEDS_FPS, _fill, plan_range


def probe_fps(profile: dict, project: str, vars: dict, cfg: dict) -> float:
    """Ask the app on this Mac for the project's frame rate, as its profile says how."""
    probe = profile.get("fps_probe")
    exe = toolinfo.programs(cfg).get(profile["name"])
    if not probe or not exe:
        raise ValueError(f"{profile['name']} needs the frame rate: pass --fps")
    args = [_fill(a, {**vars, "project": str(Path(project).expanduser().resolve())}) for a in probe["args"]]
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"couldn't ask {profile['name']} for the frame rate ({e}): pass --fps") from e
    m = re.search(probe["pattern"], out.stdout + out.stderr)
    if out.returncode != 0 or not m:
        raise ValueError(f"{profile['name']} didn't say the project's frame rate: pass --fps")
    return float(m.group(1))


def plan_app(app: str, project: str, start: int, end: int, chunk: int = 100, fps: float | None = None,
             vars: dict | None = None, output: str = "", workdir: str = "", name: str = "",
             cfg: dict | None = None) -> dict:
    cfg = cfg or config.load()
    known = profiles.load()
    if app not in known:
        raise ValueError(f"no app profile called {app!r} (have: {', '.join(sorted(known)) or 'none'}); see `macmesh apps`")
    prof = known[app]
    vars = {**prof.get("vars", {}), **(vars or {})}
    rng = prof["range"]
    if fps is None and any(NEEDS_FPS.search(a) for step in rng["steps"] for a in step):
        fps = probe_fps(prof, project, vars, cfg)
    media = mediarefs.scan(project) if prof.get("media") == "scan" else []
    return plan_range(app, project, start, end, steps=rng["steps"], chunk=chunk, out_ext=rng.get("out_ext", ""),
                      merge=rng.get("merge", "auto"), output=output, name=name, workdir=workdir, fps=fps,
                      vars=vars, fail_if=prof.get("fail_if"), media=media)
