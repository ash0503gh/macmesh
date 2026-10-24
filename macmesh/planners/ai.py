"""AI planner: ask Claude Code or Antigravity (headless CLI) how to split a task.

The agent never runs anything. It sees the Macs, the files and your tools,
and answers with JSON choosing a deterministic planner and its parameters
(or, for unusual work, a raw job spec). macmesh validates the answer and
shows it to you before any work is sent anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import ai, mediarefs, media, profiles, toolinfo
from .video import ENCODE_PRESETS

MEDIA_EXT = {".mov", ".mp4", ".m4v", ".mkv", ".mxf", ".avi", ".webm", ".wav", ".aif", ".aiff", ".mp3", ".m4a"}

SCHEMA_DOC = """\
Reply with ONE JSON object and nothing else:
{
  "explanation": "1-3 sentences: how you are splitting the work and why",
  "planner": "app" | "video" | "range" | "batch" | "job",
  "params": { ... }
}

planner "app" — render a frame range with one of the APPS below (prefer this for their projects):
  params: {"app": app name, "project": path, "start": int, "end": int (inclusive), "chunk": frames per
           task, "fps": number (only if the app needs it and can't report it), "vars": {name: value}
           for the app's placeholders (see its vars), "output": final name}

planner "video" — re-encode/filter one video across Macs (split at keyframes, stitch losslessly):
  params: {"src": path, "output": "name.mp4", "preset": one of PRESETS, "vargs": optional raw
           ffmpeg video args overriding preset, "vf": optional ffmpeg filter e.g. "scale=3840:-2",
           "chunk_seconds": number (aim for >= 3 chunks per Mac slot, each >= 5 s)}

planner "range" — render a frame range with any other command-line renderer:
  params: {"tool": tool name, "project": path, "start": int, "end": int (inclusive),
           "chunk": frames per task, "template": [CLI args AFTER the tool name, using
           {in:project} {out:result} {start} {end} {count} {index}],
           "out_ext": ".mov" for a movie per chunk or "" for a folder of frames,
           "merge": "concat" | "dirs" | "none", "output": final name,
           "extra_inputs": {name: path} for additional assets, referenced as {in:name}}

planner "batch" — one task per file:
  params: {"files": [paths], "template": [full argv including program, using {in:file}
           {out:result} {stem}], "out_ext": ".ext", "name": job name}

planner "job" — anything else: params is a raw job spec:
  {"name": str, "tasks": [{"id": str, "argv": [program, ...{in:NAME}..{out:NAME}...],
   "inputs": {NAME: local path}, "outputs": {NAME: relative dest path}, "cost": number}],
   "merge": [{"op": "concat_video", "inputs": [rel paths], "output": rel, "audio_from": path|null}
           | {"op": "merge_dirs", "dirs": [rel], "into": rel}
           | {"op": "command", "argv": [... "{workdir}" ...]}]}

Rules:
- argv[0] must be a program available on the Macs (see "programs" per node).
- Prefer more, smaller chunks over a fixed split: Macs pull chunks as they free up, so a
  fanless MacBook Air naturally takes fewer. Never hard-code a percentage split.
- Only use files listed below. Do not invent CLI flags beyond the documented usage.
- For an app in APPS, use planner "app": its commands are already known and tested.
- If the request cannot be done with the available tools, use planner "job" with an empty
  tasks list and explain why in "explanation".
"""


def build_prompt(request: str, files: list[str], nodes: list[dict], tools: dict, apps: dict | None = None) -> str:
    file_info = []
    for f in files:
        p = Path(f).expanduser().resolve()
        entry = {"path": str(p), "kind": "dir" if p.is_dir() else "file"}
        if p.is_file():
            entry["size_mb"] = round(p.stat().st_size / 1e6, 1)
            if p.suffix.lower() in MEDIA_EXT:
                try:
                    entry["media"] = media.summary(p)
                except RuntimeError:
                    pass
        elif p.is_dir():
            entry["contents"] = sorted(x.name for x in p.iterdir())[:30]
        file_info.append(entry)
    node_info = [{
        "name": n["node_name"], "model": n["hardware"]["model"], "chip": n["hardware"]["chip"],
        "perf_cores": n["hardware"]["perf_cores"], "gpu_cores": n["hardware"]["gpu_cores"],
        "memory_gb": n["hardware"]["memory_gb"], "fanless": n["hardware"]["fanless"],
        "slots": n["slots"], "on_battery": n["power"]["on_battery"], "programs": n["programs"],
    } for n in nodes]
    tool_info = {k: {"description": v.get("description", ""), "usage": v.get("usage", "")}
                 for k, v in tools.items() if k not in (apps or {})}
    app_info = {k: {"description": p.get("description", ""), "notes": p.get("notes", ""),
                    "vars": p.get("vars", {}), "chunk_output": p["range"].get("out_ext") or "folder of frames"}
                for k, p in (apps or {}).items()}
    return (
        "You are the planner for macmesh, which splits compute jobs across nearby Macs.\n"
        "Decide how to split the user's request into independent tasks.\n\n"
        f"USER REQUEST:\n{request}\n\n"
        f"FILES:\n{json.dumps(file_info, indent=1)}\n\n"
        f"PROJECT CONTENTS (text files among them, for reading names of comps, sequences, settings):\n"
        f"{json.dumps(ai.project_contents(files), indent=1)}\n\n"
        f"MACS ON THE MESH:\n{json.dumps(node_info, indent=1)}\n\n"
        f"APPS (profiles):\n{json.dumps(app_info, indent=1)}\n\n"
        f"OTHER TOOLS (CLI):\n{json.dumps(tool_info, indent=1)}\n\n"
        f"VIDEO PRESETS: {sorted(ENCODE_PRESETS)}\n\n"
        f"{SCHEMA_DOC}"
    )


def extract_plan(raw: str) -> dict:
    """Pull the plan object out of whatever the CLI printed."""
    try:
        return ai.extract_json(raw, "planner")
    except ai.AgentError as e:
        raise ValueError(str(e)) from None


def plan_with_agent(request: str, files: list[str], nodes: list[dict], cfg: dict,
                    agent: str | None = None, home: Path | None = None) -> dict:
    apps = {k: p for k, p in profiles.load(home).items() if p.get("range")}  # the rest are plain tools
    prompt = build_prompt(request, files, nodes, toolinfo.tools(cfg, home), apps)
    plan = extract_plan(ai.ask(prompt, cfg, agent))
    if plan.get("planner") not in ("app", "video", "range", "batch", "job"):
        raise ValueError(f"agent chose unknown planner {plan.get('planner')!r}")
    if not isinstance(plan.get("params"), dict):
        raise ValueError("agent plan has no params object")
    return plan


def plan_to_job(plan: dict, files: list[str], cfg: dict, workdir: str = "", home: Path | None = None) -> dict:
    """Turn the agent's answer into a job, through the same planners people use. Raises ValueError.

    `workdir` (where the job's files go) overrides whatever the agent chose."""
    from . import PLANNERS, PRIVATE_PARAMS
    params = dict(plan["params"])
    if workdir:
        params["workdir"] = workdir
    if plan["planner"] == "job":
        job = params
    else:
        fn = PLANNERS[plan["planner"]]
        allowed = set(fn.__code__.co_varnames[:fn.__code__.co_argcount]) - PRIVATE_PARAMS
        unknown = set(params) - allowed
        if unknown:
            raise ValueError(f"unexpected params for {plan['planner']}: {sorted(unknown)}")
        if plan["planner"] == "app":
            params["cfg"], params["home"] = cfg, home
        try:
            job = fn(**params)
        except TypeError as e:
            raise ValueError(f"the plan's parameters don't fit: {e}") from None
    if not job.get("tasks"):
        raise ValueError(plan.get("explanation") or "the plan has no tasks")
    check_inputs_within(job, files)
    return job


def check_inputs_within(job: dict, files: list[str]) -> None:
    """An AI plan may only ship the files you named, things inside them, and the media they use."""
    roots = [Path(f).expanduser().resolve() for f in files]
    used = {Path(m) for f in files for m in mediarefs.scan(f)} if files else set()
    workdir = Path(job.get("workdir") or ".").expanduser().resolve()
    for t in job["tasks"]:
        for name, local in t.get("inputs", {}).items():
            p = Path(local).expanduser().resolve()
            if p.is_relative_to(workdir) or p in used:
                continue  # made by the planner (video segments), or media a named project uses
            if not any(p == r or r in p.parents for r in roots):
                raise ValueError(f"{t['id']}: input {name} ({p}) is not one of the files you gave")


SAFE_PROGRAMS = {"ffmpeg", "ffprobe"}


def unsafe_reasons(job: dict, app_names: set[str]) -> list[str]:
    """Why a plan needs a person's OK before it runs; empty when it only uses app profiles or ffmpeg.

    Workers sandbox every task anyway; what's left to guard is programs beyond the known ones and
    merge commands, which run on this Mac without a sandbox.
    """
    from .. import jobspec
    reasons = []
    programs = {p for t in job["tasks"] for p in jobspec.programs(t)}
    if extra := programs - SAFE_PROGRAMS - app_names:
        reasons.append(f"runs {', '.join(sorted(extra))}, which isn't an app profile or ffmpeg")
    if any(step["op"] == "command" for step in job.get("merge", [])):
        reasons.append("runs a command on this Mac to put the result together")
    return reasons
