"""One task per input file: transcode, upscale, transcribe, mask, convert...

Template placeholders:
  {in:file}     the input file on the worker
  {out:result}  where to write the result (named <stem><out_ext>)
  {stem}        the input's file name without extension
  {in:NAME}     a file every task gets too (a script, a model), from `extra_inputs`

With `packages`, the template's "python" is a Python with those packages (scripts.py).
"""

from __future__ import annotations

from pathlib import Path

from .. import scripts


MEDIA = {".mp3", ".m4a", ".wav", ".aif", ".aiff", ".flac", ".ogg", ".mov", ".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mxf"}


def _durations(paths: list[Path]) -> dict[Path, float]:
    """Length in seconds of each audio/video file (ffprobe, several at once); others are left out."""
    from concurrent.futures import ThreadPoolExecutor

    from .. import media

    def length(p: Path):
        try:
            return p, float(media.summary(p)["duration"])
        except (RuntimeError, OSError, ValueError, KeyError):
            return p, 0.0
    media_files = [p for p in paths if p.suffix.lower() in MEDIA and p.is_file()]
    with ThreadPoolExecutor(8) as pool:
        return {p: d for p, d in pool.map(length, media_files) if d > 0}


def plan_batch(files: list[str], template: list[str], out_ext: str, name: str = "batch",
               workdir: str = "", extra_inputs: dict | None = None, packages: list | None = None) -> dict:
    """One task per file. A task's cost is the file's length when they're all audio or video (work
    on media scales with its duration, not its size), else its size in MB."""
    if not files:
        raise ValueError("no input files")
    if not out_ext.startswith("."):
        raise ValueError("out_ext must look like '.mp4' or '.txt'")
    wd = Path(workdir or f"./macmesh-out/{name}").expanduser().resolve()
    shared = {k: str(Path(v).expanduser().resolve()) for k, v in (extra_inputs or {}).items()}
    paths = [Path(f).expanduser().resolve() for f in files]
    lengths = _durations(paths)
    by_length = len(lengths) == len(paths)
    tasks, stems = [], set()
    for i, p in enumerate(paths, 1):
        stem = p.stem
        if stem in stems:  # same name in two folders
            stem = f"{stem}-{i}"
        stems.add(stem)
        argv = [a.replace("{stem}", stem) for a in template]
        size = p.stat().st_size if p.is_file() else 1
        tasks.append({"id": f"b{i:05d}", "argv": argv, "inputs": {"file": str(p), **shared},
                      "outputs": {"result": f"results/{stem}{out_ext}"},
                      "cost": max(lengths[p], 0.1) if by_length else max(size / 1e6, 0.01),
                      "unit": "media-seconds" if by_length else "mb"})
    return scripts.use_packages({"name": name, "workdir": str(wd), "tasks": tasks, "merge": []}, packages)
