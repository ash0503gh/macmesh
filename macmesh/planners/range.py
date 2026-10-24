"""Frame-range jobs for any renderer with a command line.

The command template is the tool's own CLI (without the program name), one
list per step, with placeholders:
  {in:project}  the project file/bundle (copied to each Mac once)
  {out:result}  where the chunk should be written (a file, or a directory of frames)
  {indir}       the folder holding the project and its media on the Mac running the task
  {start} {end} this chunk's frames, both inclusive; {end_excl} = end + 1
  {count} {index}
  {start_s} {end_s} {fps}  the same range in seconds (start, and end exclusive), given the frame rate
  {name}        a value from `vars` (e.g. {comp})
"""

from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path

from .. import mediarefs

LEFTOVER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
NEEDS_FPS = re.compile(r"\{(start_s|end_s|fps)\}")
RESERVED = {"start", "end", "end_excl", "count", "index", "start_s", "end_s", "fps", "indir"}


def exact_fps(fps: float) -> Fraction:
    """23.976 means 24000/1001 (and 29.97, 59.94...): seconds must land exactly on frames."""
    for n in (24, 30, 48, 60, 120):
        ntsc = Fraction(n * 1000, 1001)
        if abs(fps - float(ntsc)) < 0.005:
            return ntsc
    return Fraction(fps).limit_denominator(1001)


def _seconds(frame: int, fps: Fraction) -> str:
    return repr(float(Fraction(frame) / fps))


def plan_range(tool: str, project: str, start: int, end: int, template: list[str] | None = None,
               chunk: int = 100, out_ext: str = "", merge: str = "auto",
               output: str = "", extra_inputs: dict | None = None, name: str = "",
               workdir: str = "", steps: list[list[str]] | None = None, fps: float | None = None,
               vars: dict | None = None, fail_if: list[str] | None = None, media: list[str] | None = None) -> dict:
    """out_ext like ".mov" means each chunk is a movie; empty means a frames directory.

    merge: "concat" (join movie chunks), "dirs" (pool frames into one folder),
    "none", or "auto" (concat for movies, dirs for frames). `media` files go to
    every Mac alongside the project, into {indir}.
    """
    if end < start:
        raise ValueError("end frame is before start frame")
    if chunk < 1:
        raise ValueError("chunk must be at least 1 frame")
    steps = steps or ([template] if template else None)
    if not steps:
        raise ValueError("no command template")
    vars = dict(vars or {})
    if clash := RESERVED & set(vars):
        raise ValueError(f"{sorted(clash)} are built-in placeholders, not vars")
    words = [a for step in steps for a in step]
    rate = None
    if any(NEEDS_FPS.search(a) for a in words):
        if not fps:
            raise ValueError("this command takes seconds, so it needs the frame rate: pass --fps")
        rate = exact_fps(float(fps))
    proj = Path(project).expanduser().resolve()
    name = name or f"{proj.stem}-{tool}"
    wd = Path(workdir or f"./macmesh-out/{name}").expanduser().resolve()
    inputs = {"project": str(proj)}
    for k, v in (extra_inputs or {}).items():
        inputs[k] = str(Path(v).expanduser().resolve())
    for k, v in mediarefs.as_inputs(media or []).items():
        inputs.setdefault(k, v)
    if merge == "auto":
        merge = "concat" if out_ext else "dirs"

    tasks, outs = [], []
    for index, s in enumerate(range(start, end + 1, chunk)):
        e = min(s + chunk - 1, end)
        tid = f"f{s:06d}-{e:06d}"
        dest = f"chunks/{tid}{out_ext}"
        outs.append(dest)
        fill = {"start": str(s), "end": str(e), "end_excl": str(e + 1), "count": str(e - s + 1),
                "index": str(index), **vars}
        if rate is not None:
            fill.update(start_s=_seconds(s, rate), end_s=_seconds(e + 1, rate), fps=repr(float(rate)))
        filled = [[tool] + [_fill(a, fill) for a in step] for step in steps]
        task = {"id": tid, "steps": filled, "inputs": dict(inputs), "outputs": {"result": dest},
                "cost": float(e - s + 1)}
        if fail_if:
            task["fail_if"] = list(fail_if)
        tasks.append(task)
    for word in (a for step in tasks[0]["steps"] for a in step):
        for unknown in LEFTOVER.findall(word):
            if unknown != "indir":
                raise ValueError(f"the command uses {{{unknown}}}: give it a value with --var {unknown}=...")

    merge_steps = []
    if merge == "concat":
        merge_steps.append({"op": "concat_video", "inputs": outs, "output": output or f"{name}{out_ext or '.mov'}"})
    elif merge == "dirs":
        merge_steps.append({"op": "merge_dirs", "dirs": outs, "into": output or "frames"})
    return {"name": name, "workdir": str(wd), "tasks": tasks, "merge": merge_steps}


def _fill(arg: str, fill: dict) -> str:
    return LEFTOVER.sub(lambda m: fill.get(m.group(1), m.group(0)), arg)
