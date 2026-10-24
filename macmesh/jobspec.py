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
      "timeout": 3600                                        # optional, seconds
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
task. Inputs are uploaded once per Mac (content-addressed), so a project file
shared by every task only crosses Wi-Fi once.
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
        argv = t.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise JobError(f"{tid}: argv must be a non-empty list of strings")
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
        for arg in argv:
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


def save(job: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(job, indent=2) + "\n")
