"""ffmpeg/ffprobe helpers and the local merge steps."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

# Audio codecs that can be stream-copied into an .mp4/.mov without re-encoding.
MP4_AUDIO = {"aac", "alac", "mp3", "ac3", "eac3"}


def probe(path: str | Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def summary(path: str | Path) -> dict:
    """Compact metadata for planners (and the AI prompt)."""
    info = probe(path)
    fmt = info.get("format", {})
    s = {"duration": float(fmt.get("duration", 0) or 0), "size_mb": round(int(fmt.get("size", 0)) / 1e6, 1),
         "video": None, "audio": None}
    for st in info.get("streams", []):
        if st.get("codec_type") == "video" and not s["video"] and st.get("disposition", {}).get("attached_pic") != 1:
            s["video"] = {"codec": st.get("codec_name"), "width": st.get("width"), "height": st.get("height"),
                          "fps": st.get("avg_frame_rate"), "pix_fmt": st.get("pix_fmt")}
        elif st.get("codec_type") == "audio" and not s["audio"]:
            s["audio"] = {"codec": st.get("codec_name"), "channels": st.get("channels"),
                          "sample_rate": st.get("sample_rate")}
    return s


def run(argv: list[str]) -> None:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed:\n{proc.stderr[-2000:]}")


def concat_video(workdir: Path, step: dict) -> Path:
    """Join encoded chunks losslessly, then add the original audio back in one pass.

    Chunks are video-only: encoding audio per chunk leaves tiny gaps (AAC
    priming) at every seam, so audio is taken whole from the source instead.
    """
    out = workdir / step["output"]
    listing = workdir / ".concat.txt"
    with open(listing, "w") as f:
        for rel in step["inputs"]:
            p = (workdir / rel).resolve()
            f.write("file '" + str(p).replace("'", "'\\''") + "'\n")
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(listing)]
    audio_src = step.get("audio_from")
    if audio_src:
        argv += ["-i", str(audio_src), "-map", "0:v:0", "-map", "1:a?", "-map_metadata", "1"]
        mode = step.get("audio", "auto")
        if mode == "auto":
            a = summary(audio_src)["audio"]
            mode = "copy" if a and a["codec"] in MP4_AUDIO else "aac"
        argv += ["-c:a", "copy"] if mode == "copy" else ["-c:a", "aac", "-b:a", "256k"]
    argv += ["-c:v", "copy"]
    if out.suffix.lower() in (".mp4", ".mov", ".m4v"):
        argv += ["-movflags", "+faststart"]
    argv.append(str(out))
    out.parent.mkdir(parents=True, exist_ok=True)
    run(argv)
    listing.unlink()
    return out


def merge_dirs(workdir: Path, step: dict) -> Path:
    into = workdir / step["into"]
    into.mkdir(parents=True, exist_ok=True)
    for rel in step["dirs"]:
        src = workdir / rel
        for item in sorted(src.iterdir()):
            target = into / item.name
            if target.exists():
                raise FileExistsError(f"merge_dirs: {item.name} produced by more than one chunk")
            os.replace(item, target)
        src.rmdir()
    return into


def command(workdir: Path, step: dict) -> None:
    argv = [a.replace("{workdir}", str(workdir)) for a in step["argv"]]
    if not shutil.which(argv[0]):
        raise RuntimeError(f"merge command not found: {argv[0]}")
    run(argv)


def run_merge(workdir: Path, steps: list[dict], log=print) -> None:
    for step in steps:
        op = step["op"]
        log(f"merge: {op}")
        {"concat_video": concat_video, "merge_dirs": merge_dirs, "command": command}[op](workdir, step)
