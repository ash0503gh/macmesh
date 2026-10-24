"""The job format: what planners (built-in or AI) produce and `macmesh run` executes.

{
  "name": "export-4k",
  "workdir": "./macmesh-out/export-4k",          # where results land locally
  "tasks": [
    {
      "id": "c0001",
      "argv": ["ffmpeg", "-i", "{in:src}", "-c:v", "libx264", "{out:video}"],
      "inputs":  {"src": "/path/on/this/mac/seg0001.mkv"},   # files or directories
      "outputs": {"video": "chunks/c0001.mp4"},               # relative to workdir
      "cost": 1.0,                                           # relative work estimate
      "timeout": 3600,                                       # optional, seconds
      "fail_if": ["placeholder frames"]                      # optional: regexes for output that means failure
    }
  ],
  "merge": [                                     # runs locally once all tasks finish
    {"op": "concat_video", "inputs": ["chunks/c0001.mp4"], "audio_from": "/path/src.mov",
     "output": "final.mp4"},
    {"op": "merge_dirs", "dirs": ["frames/c0001"], "into": "frames"},
    {"op": "command", "argv": ["ffmpeg", "-i", "{workdir}/frames/%05d.png", "{workdir}/out.mov"]}
  ]
}

Inside argv, {in:NAME} and {out:NAME} become paths on whichever Mac runs the
task, and {indir} the folder holding every input (so an app can relink media
copied there). Inputs are uploaded once per Mac (content-addressed), so a
project file shared by every task only crosses Wi-Fi once.

A task may run several commands in its own folder instead of one: give
"steps": [[argv], [argv], ...] in place of "argv" (e.g. relink media, then
render). They run in order and the task fails at the first that fails.
validate() turns "argv" into a one-step "steps". "fail_if" lists regular
expressions that, matched anywhere in the task's output, mean it failed even
though it exited 0 (some apps render placeholders for missing media).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
PLACEHOLDER = re.compile(r"\{(in|out):([A-Za-z0-9_.-]+)\}")
MERGE_OPS = {"concat_video", "merge_dirs", "command"}


class JobError(ValueError):
    pass


def load(path: str | Path) -> dict:
    return validate(json.loads(Path(path).read_text()))


def _rel(p: str, what: str) -> str:
    if not isinstance(p, str) or not p or Path(p).is_absolute() or ".." in Path(p).parts:
        raise JobError(f"{what}: must be a relative path inside the workdir, got {p!r}")
    return p


def validate(job: dict) -> dict:
    if not isinstance(job, dict):
        raise JobError("job must be an object")
    job.setdefault("name", "job")
    job.setdefault("workdir", f"./macmesh-out/{job['name']}")
    job.setdefault("merge", [])
    tasks = job.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise JobError("job needs a non-empty 'tasks' list")
    seen, dests = set(), set()
    for t in tasks:
        tid = t.get("id")
        if not isinstance(tid, str) or not NAME.match(tid) or tid in seen:
            raise JobError(f"task id {tid!r} missing, invalid or duplicated")
        seen.add(tid)
        steps = t.get("steps", [t.get("argv")] if "argv" in t else None)
        if not isinstance(steps, list) or not steps or not all(
                isinstance(a, list) and a and all(isinstance(x, str) for x in a) for a in steps):
            raise JobError(f"{tid}: argv (or each of steps) must be a non-empty list of strings")
        t["steps"] = steps
        t.pop("argv", None)
        fail_if = t.setdefault("fail_if", [])
        if not isinstance(fail_if, list) or not all(isinstance(m, str) and m for m in fail_if):
            raise JobError(f"{tid}: fail_if must be a list of non-empty strings")
        for pattern in fail_if:
            try:
                re.compile(pattern)
            except re.error as e:
                raise JobError(f"{tid}: fail_if {pattern!r} isn't a valid regular expression: {e}") from None
        t.setdefault("inputs", {})
        t.setdefault("outputs", {})
        t["cost"] = float(t.get("cost", 1.0))
        for name, local in t["inputs"].items():
            if not NAME.match(name):
                raise JobError(f"{tid}: bad input name {name!r}")
            if not Path(local).exists():
                raise JobError(f"{tid}: input {name} not found: {local}")
        for name, dest in t["outputs"].items():
            if not NAME.match(name):
                raise JobError(f"{tid}: bad output name {name!r}")
            _rel(dest, f"{tid}: output {name}")
            if dest in dests:
                raise JobError(f"{tid}: output path {dest} used by more than one task")
            if Path(dest).name.startswith("."):
                raise JobError(f"{tid}: output file names may not start with '.'")
            dests.add(dest)
        names = {"in": set(t["inputs"]), "out": set(t["outputs"])}
        for arg in (a for step in steps for a in step):
            for kind, name in PLACEHOLDER.findall(arg):
                if name not in names[kind]:
                    raise JobError(f"{tid}: {{{kind}:{name}}} has no matching {kind}put")
    for step in job["merge"]:
        op = step.get("op")
        if op not in MERGE_OPS:
            raise JobError(f"unknown merge op {op!r} (expected one of {sorted(MERGE_OPS)})")
        if op == "concat_video":
            for p in step.get("inputs", []):
                _rel(p, "concat_video input")
            _rel(step.get("output", ""), "concat_video output")
        elif op == "merge_dirs":
            for p in step.get("dirs", []):
                _rel(p, "merge_dirs dir")
            _rel(step.get("into", ""), "merge_dirs into")
        elif not isinstance(step.get("argv"), list) or not step["argv"]:
            raise JobError("command merge step needs argv")
    return job


def programs(task: dict) -> list[str]:
    """Every program a (validated) task runs, in order of first use."""
    return list(dict.fromkeys(step[0] for step in task["steps"]))


def save(job: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(job, indent=2) + "\n")
