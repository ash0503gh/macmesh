"""Which files a project uses, so they can travel with it to the other Macs.

Works for any app whose project is JSON (a file, or JSON files inside a bundle
folder): every string that is an absolute path, or a file:// URL, naming a
file that exists on this Mac counts. System locations that every Mac has
(/System, /Library, /Applications...) are left out. On the other Mac the files
arrive side by side in the task's input folder ({indir}), and the app's
profile relinks the project to them there.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote, urlparse

SHARED = ("/System/", "/Library/", "/Applications/", "/usr/", "/bin/", "/sbin/", "/opt/homebrew/", "/private/var/db/")
MAX_JSON = 64 << 20


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _as_path(text: str) -> Path | None:
    if text.startswith("file://"):
        text = unquote(urlparse(text).path)
    if not text.startswith("/") or "\n" in text or len(text) > 4096:
        return None
    return Path(text)


def scan(project: str | Path) -> list[str]:
    """Absolute paths of the files the project refers to, sorted."""
    root = Path(project).expanduser().resolve()
    docs = [root] if root.is_file() else [f for f in root.rglob("*") if f.is_file()]
    found = set()
    for doc in docs:
        if doc.stat().st_size > MAX_JSON:
            continue
        try:
            data = json.loads(doc.read_text())
        except (ValueError, UnicodeDecodeError, OSError):
            continue
        for s in _strings(data):
            p = _as_path(s)
            if p is None or str(p).startswith(SHARED):
                continue
            try:
                if not p.is_file():
                    continue
                real = p.resolve()
            except OSError:
                continue
            if real != root and root not in real.parents:  # files inside a bundle travel with it already
                found.add(str(real))
    return sorted(found)


def as_inputs(files: list[str]) -> dict[str, str]:
    """Task inputs for the files, named m0001... Two different files with one name can't share {indir}."""
    by_name: dict[str, str] = {}
    for f in files:
        name = Path(f).name
        if name in by_name and by_name[name] != f:
            raise ValueError(f"two media files are both called {name!r} ({by_name[name]} and {f}); "
                             "rename one so the other Mac can tell them apart")
        by_name[name] = f
    return {f"m{i:04d}": f for i, f in enumerate(sorted(by_name.values()), 1)}
