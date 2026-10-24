"""Shared pieces of the coordinator <-> worker protocol.

Requests are signed with HMAC-SHA256 over the method, path, a timestamp and
the body hash, so the mesh token itself never crosses the network. Blob
uploads are content-addressed (the worker re-hashes them), so their bodies
are covered by the path instead of the signature.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import platform
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

MAX_SKEW = 300  # seconds of clock difference tolerated between Macs
UNSIGNED_BODY = "UNSIGNED"
CHUNK = 1 << 20


def sign(token: str, method: str, path: str, ts: str, body_hash: str) -> str:
    msg = f"{method}\n{path}\n{ts}\n{body_hash}".encode()
    return hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()


def auth_headers(token: str, method: str, path: str, body: bytes | None) -> dict:
    ts = str(int(time.time()))
    body_hash = UNSIGNED_BODY if body is None else hashlib.sha256(body).hexdigest()
    return {
        "X-Mesh-Time": ts,
        "X-Mesh-Body": body_hash,
        "X-Mesh-Sig": sign(token, method, path, ts, body_hash),
    }


def verify(token: str, method: str, path: str, headers, body: bytes | None) -> bool:
    ts, sig, body_hash = headers.get("X-Mesh-Time"), headers.get("X-Mesh-Sig"), headers.get("X-Mesh-Body")
    if not (ts and sig and body_hash) or not ts.isdigit():
        return False
    if abs(time.time() - int(ts)) > MAX_SKEW:
        return False
    if body is not None and body_hash != hashlib.sha256(body).hexdigest():
        return False
    if body is None and body_hash != UNSIGNED_BODY:
        return False
    return hmac.compare_digest(sig, sign(token, method, path, ts, body_hash))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def tar_dir(src: Path, dest: Path) -> None:
    """Pack a directory (e.g. a .ecproj bundle) with stable ordering."""
    def reset(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti

    with tarfile.open(dest, "w") as tf:
        for root, dirs, files in os.walk(src):
            dirs.sort()
            for name in sorted(files) + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
                full = Path(root, name)
                tf.add(full, arcname=str(full.relative_to(src)), recursive=False, filter=reset)
            for d in dirs:
                full = Path(root, d)
                if not full.is_symlink():
                    tf.add(full, arcname=str(full.relative_to(src)), recursive=False, filter=reset)


def safe_extract(archive: Path, dest: Path) -> None:
    """Extract a tar without letting entries escape `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
            return
        # Python < 3.9.17 (e.g. macOS's bundled 3.9.6): check by hand.
        root = dest.resolve()
        members = []
        for m in tf.getmembers():
            target = (dest / m.name).resolve()
            if os.path.isabs(m.name) or not str(target).startswith(str(root) + os.sep):
                raise ValueError(f"unsafe path in archive: {m.name}")
            if m.issym() or m.islnk():
                link = (target.parent / m.linkname).resolve()
                if os.path.isabs(m.linkname) or not str(link).startswith(str(root) + os.sep):
                    raise ValueError(f"unsafe link in archive: {m.name}")
            if m.isdev():
                continue
            members.append(m)
        tf.extractall(dest, members=members)


def clone(src: Path, dst: Path) -> None:
    """Copy a file or directory; instant copy-on-write clone on APFS."""
    if platform.system() == "Darwin":
        if subprocess.run(["cp", "-cR", str(src), str(dst)], capture_output=True).returncode == 0:
            return
        shutil.rmtree(dst, ignore_errors=True)  # not APFS: fall back to a real copy
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)
