"""Which programs this Mac can run, and exactly which build of each.

Two Macs with different builds of an app (or different ffmpeg versions) can
render different pixels for the same frames, so the coordinator only sends a
task to Macs whose build matches its own.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import profiles, wire

# Version flags known to be safe (print and exit). App tools declare theirs as
# `version_args` in the `tools` table; running an unknown GUI binary with
# --version could open the app, so nothing is guessed.
VERSION_ARGS = {"ffmpeg": ["-version"], "ffprobe": ["-version"]}

_hash_cache: dict[tuple, str] = {}


def tools(cfg: dict, home: Path | None = None) -> dict[str, dict]:
    """The `tools` table: app profiles' programs, with the config's entries layered on top."""
    table = {name: profiles.tool_entry(p) for name, p in profiles.load(home).items()}
    for name, entry in cfg["tools"].items():
        table[name] = {**table.get(name, {}), **entry}
    return table


def programs(cfg: dict, home: Path | None = None) -> dict[str, str]:
    """Allowed program name -> resolved executable, for those present here."""
    found = {}
    for name in cfg["allowed_programs"]:
        path = shutil.which(name)
        if path:
            found[name] = path
    for name, tool in tools(cfg, home).items():
        path = os.path.expanduser(tool.get("path", ""))
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            found[name] = path
        elif shutil.which(name):
            found[name] = shutil.which(name)
    return found


def file_sha256(path: str) -> str:
    """SHA-256 of the real executable, cached until its size or mtime changes."""
    real = Path(path).resolve()
    st = real.stat()
    key = (str(real), st.st_size, st.st_mtime_ns)
    if key not in _hash_cache:
        _hash_cache[key] = wire.sha256_file(real)
    return _hash_cache[key]


def version(name: str, path: str, table: dict) -> str:
    args = table.get(name, {}).get("version_args") or VERSION_ARGS.get(name)
    if not args:
        return ""
    try:
        out = subprocess.run([path, *args], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = (out.stdout or out.stderr).strip().splitlines()
    return lines[0].strip() if lines else ""


def fingerprints(cfg: dict, home: Path | None = None) -> dict[str, dict]:
    out, table = {}, tools(cfg, home)
    for name, path in programs(cfg, home).items():
        try:
            out[name] = {"sha256": file_sha256(path), "version": version(name, path, table)}
        except OSError:
            continue
    return out


def describe(fp: dict) -> str:
    return fp.get("version") or fp["sha256"][:12]


def same_build(a: dict | None, b: dict | None, match: str) -> bool:
    if match == "none" or a is None:
        return True  # nothing to compare against
    if b is None:
        return False
    # Compare like with like: a version only counts when both sides have one.
    if match == "version" and a.get("version") and b.get("version"):
        return a["version"] == b["version"]
    return a["sha256"] == b["sha256"]

