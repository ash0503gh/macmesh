"""One task per input file: transcode, upscale, transcribe, mask, convert...

Template placeholders:
  {in:file}     the input file on the worker
  {out:result}  where to write the result (named <stem><out_ext>)
  {stem}        the input's file name without extension
"""

from __future__ import annotations

from pathlib import Path


def plan_batch(files: list[str], template: list[str], out_ext: str, name: str = "batch",
               workdir: str = "", extra_inputs: dict | None = None) -> dict:
    if not files:
        raise ValueError("no input files")
    if not out_ext.startswith("."):
        raise ValueError("out_ext must look like '.mp4' or '.txt'")
    wd = Path(workdir or f"./macmesh-out/{name}").expanduser().resolve()
    shared = {k: str(Path(v).expanduser().resolve()) for k, v in (extra_inputs or {}).items()}
    tasks, stems = [], set()
    for i, f in enumerate(files, 1):
        p = Path(f).expanduser().resolve()
        stem = p.stem
        if stem in stems:  # same name in two folders
            stem = f"{stem}-{i}"
        stems.add(stem)
        argv = [a.replace("{stem}", stem) for a in template]
        size = p.stat().st_size if p.is_file() else 1
        tasks.append({"id": f"b{i:05d}", "argv": argv, "inputs": {"file": str(p), **shared},
                      "outputs": {"result": f"results/{stem}{out_ext}"},
                      "cost": max(size / 1e6, 0.01)})
    return {"name": name, "workdir": str(wd), "tasks": tasks, "merge": []}
