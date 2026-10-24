"""Split a video at keyframes, encode chunks across Macs, stitch losslessly."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from .. import media

ENCODE_PRESETS = {
    # Software encoders are where a second Mac helps most: they scale with cores.
    "h264": ["-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p"],
    "hevc": ["-c:v", "libx265", "-crf", "22", "-preset", "medium", "-pix_fmt", "yuv420p", "-tag:v", "hvc1"],
    "prores": ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"],
    # Media-engine encoders are already very fast; splitting pays off mainly
    # when heavy filters (--vf) dominate.
    "h264-hw": ["-c:v", "h264_videotoolbox", "-b:v", "20M"],
    "hevc-hw": ["-c:v", "hevc_videotoolbox", "-b:v", "16M", "-tag:v", "hvc1"],
}


def plan_video(src: str, output: str, preset: str = "h264", vargs: str = "", vf: str = "",
               chunk_seconds: float = 20.0, workdir: str = "", name: str = "") -> dict:
    srcp = Path(src).expanduser().resolve()
    meta = media.summary(srcp)
    if not meta["video"]:
        raise ValueError(f"{src} has no video stream")
    name = name or f"{srcp.stem}-{preset}"
    wd = Path(workdir or f"./macmesh-out/{name}").expanduser().resolve()
    segdir = wd / ".segments"
    segdir.mkdir(parents=True, exist_ok=True)
    for old in segdir.glob("seg*.mkv"):
        old.unlink()

    # Lossless stream-copy split; each piece starts on a keyframe so it can be
    # decoded on its own. Only these pieces travel over Wi-Fi, not the whole file.
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(srcp),
        "-map", "0:v:0", "-c", "copy", "-f", "segment", "-segment_time", str(chunk_seconds),
        "-reset_timestamps", "1", str(segdir / "seg%05d.mkv"),
    ], check=True)
    segments = sorted(segdir.glob("seg*.mkv"))

    encode = shlex.split(vargs) if vargs else list(ENCODE_PRESETS[preset])
    if vf:
        encode = ["-vf", vf] + encode
    out_ext = ".mov" if "prores" in " ".join(encode) else ".mp4"
    tasks, chunks = [], []
    for i, seg in enumerate(segments, 1):
        dest = f"chunks/c{i:05d}{out_ext}"
        chunks.append(dest)
        tasks.append({
            "id": f"c{i:05d}",
            "argv": ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", "{in:src}",
                     "-map", "0:v:0", *encode, "-an", "{out:video}"],
            "inputs": {"src": str(seg)},
            "outputs": {"video": dest},
            "cost": max(media.summary(seg)["duration"], 0.1),
        })
    return {
        "name": name,
        "workdir": str(wd),
        "tasks": tasks,
        "merge": [{"op": "concat_video", "inputs": chunks, "output": Path(output).name,
                   "audio_from": str(srcp) if meta["audio"] else None}],
        "final": str(Path(output).expanduser().resolve()),
    }
