"""Frame-range jobs for FilmCraft / EffectCraft / Grade (or any CLI renderer).

The command template is the app's own CLI with placeholders:
  {in:project}  the project file/bundle (copied to each Mac once)
  {out:result}  where the chunk should be written (a file, or a directory of frames)
  {start} {end} {count} {index}  this chunk's frame range (inclusive)
"""

from __future__ import annotations

from pathlib import Path


def plan_range(tool: str, project: str, start: int, end: int, template: list[str],
               chunk: int = 100, out_ext: str = "", merge: str = "auto",
               output: str = "", extra_inputs: dict | None = None, name: str = "",
               workdir: str = "") -> dict:
    """out_ext like ".mov" means each chunk is a movie; empty means a frames directory.

    merge: "concat" (join movie chunks), "dirs" (pool frames into one folder),
    "none", or "auto" (concat for movies, dirs for frames).
    """
    if end < start:
        raise ValueError("end frame is before start frame")
    proj = Path(project).expanduser().resolve()
    name = name or f"{proj.stem}-{tool}"
    wd = Path(workdir or f"./macmesh-out/{name}").expanduser().resolve()
    inputs = {"project": str(proj)}
    for k, v in (extra_inputs or {}).items():
        inputs[k] = str(Path(v).expanduser().resolve())
    if merge == "auto":
        merge = "concat" if out_ext else "dirs"

    tasks, outs = [], []
    for index, s in enumerate(range(start, end + 1, chunk)):
        e = min(s + chunk - 1, end)
        tid = f"f{s:06d}-{e:06d}"
        dest = f"chunks/{tid}{out_ext}"
        outs.append(dest)
        fill = {"{start}": str(s), "{end}": str(e), "{count}": str(e - s + 1), "{index}": str(index)}
        argv = [tool] + [_fill(a, fill) for a in template]
        tasks.append({"id": tid, "argv": argv, "inputs": dict(inputs),
                      "outputs": {"result": dest}, "cost": float(e - s + 1)})

    steps = []
    if merge == "concat":
        steps.append({"op": "concat_video", "inputs": outs, "output": output or f"{name}{out_ext or '.mov'}"})
    elif merge == "dirs":
        steps.append({"op": "merge_dirs", "dirs": outs, "into": output or "frames"})
    return {"name": name, "workdir": str(wd), "tasks": tasks, "merge": steps}


def _fill(arg: str, fill: dict) -> str:
    for k, v in fill.items():
        arg = arg.replace(k, v)
    return arg
