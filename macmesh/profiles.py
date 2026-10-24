"""App profiles: everything macmesh knows about a particular app, as data.

macmesh's core knows nothing about any app. A profile is one JSON file that
says how to drive an app's command-line tool: where it lives, how to ask its
version, how to render a range of frames (as one or more steps), what it may
touch inside the sandbox, and which output text means it failed. Adding an
app means adding a file, in macmesh/apps/ (examples shipped with macmesh) or
~/.macmesh/apps/ (yours; a file there replaces a shipped one of the same name).

{
  "name": "myapp",                                  # the program name tasks use
  "description": "What the app does",
  "notes": "Anything the AI planner should know",
  "program": {"path": "~/bin/myapp-cli", "version_args": ["--version"]},
  "sandbox": {"read": ["~/Library/Application Support/MyApp"], "write": ["~/Library/Caches/MyApp"]},
  "media": "scan",                                  # ship files the project refers to (see mediarefs)
  "vars": {"comp": "-"},                            # defaults for {name} placeholders (--var name=value)
  "extensions": [".myproj"],                        # project files this app opens (for drag and drop)
  "fps_probe": {"args": ["info", "{project}"], "pattern": "\"fps\":\\s*([0-9.]+)"},
  "frames_probe": {"args": ["info", "{project}"], "json": "timeline.frames"},  # or "json_seconds"
  "range": {
    "steps": [["relink", "{in:project}", "{indir}"],
              ["render", "{in:project}", "--from", "{start}", "--to", "{end_excl}", "-o", "{out:result}"]],
    "out_ext": ".mov",                              # "" when a chunk is a folder of frames
    "merge": "concat"                               # concat | dirs | none | auto
  },
  "fail_if": ["\"missing\":\\s*[1-9]"]          # regexes: output that means failure despite exit 0
}

A profile without "range" is a plain tool, for batch jobs (one task per file).

"install" says how a Mac that lacks the program can set it up, after its owner clicks Allow:
  "install": {
    "summary": "Demucs stem separation (about 1 GB)",   # what the owner is asked to allow
    "size_gb": 1.0,                                      # free space it needs
    "files": {"myapp-cli": "#!/bin/sh\nexec {dir}/env/bin/myapp \"$@\"\n"},  # written after the steps
    "executable": ["myapp-cli"],
    "steps": [["{python}", "-m", "venv", "{dir}/env"],                  # argument lists, no shell
              ["{dir}/env/bin/pip", "install", "myapp==1.2.3"]]
  }
It runs in ~/.macmesh/tools/<name> ({dir}) with macmesh's own Python ({python}), without the
sandbox (it downloads) and without administrator rights. "program.path" should point into {dir}.
The recipe is always the lending Mac's own: another Mac can only ask for a program by name.

Steps are the tool's arguments without the program name. Range placeholders:
{start} {end} (inclusive), {end_excl}, {count}, {index}, {start_s} {end_s}
(seconds, needs the frame rate) and {fps}; plus {in:project} {out:result}
{indir} and any {var}.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from . import config

PACKAGED = Path(__file__).parent / "apps"
NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class ProfileError(ValueError):
    pass


def _strings(value, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ProfileError(f"{what} must be a list of strings")
    return value


def validate(p: dict) -> dict:
    if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not NAME.match(p["name"]):
        raise ProfileError("a profile needs a \"name\" of letters, digits, '.', '_' or '-'")
    prog = p.get("program")
    if not isinstance(prog, dict) or not isinstance(prog.get("path"), str):
        raise ProfileError("\"program\" needs a \"path\"")
    _strings(prog.get("version_args", []), "program.version_args")
    rng = p.get("range")
    if rng is not None:
        if not isinstance(rng, dict) or not isinstance(rng.get("steps"), list) or not rng["steps"]:
            raise ProfileError("\"range\" needs \"steps\": a list of argument lists")
        for i, step in enumerate(rng["steps"]):
            _strings(step, f"range.steps[{i}]")
        if rng.get("merge", "auto") not in ("auto", "concat", "dirs", "none"):
            raise ProfileError("range.merge is auto, concat, dirs or none")
    inst = p.get("install")
    if inst is not None:
        if not isinstance(inst, dict) or not isinstance(inst.get("summary"), str):
            raise ProfileError("install needs a \"summary\" to show the Mac's owner")
        if not isinstance(inst.get("steps"), list) or not inst["steps"]:
            raise ProfileError("install needs \"steps\": a list of argument lists")
        for i, step in enumerate(inst["steps"]):
            if not _strings(step, f"install.steps[{i}]"):
                raise ProfileError(f"install.steps[{i}] is empty")
        files = inst.get("files", {})
        if not isinstance(files, dict) or not all(isinstance(v, str) and NAME.match(k) for k, v in files.items()):
            raise ProfileError("install.files maps plain file names to their text")
        for name in _strings(inst.get("executable", []), "install.executable"):
            if name not in files:
                raise ProfileError(f"install.executable names {name!r}, which isn't in install.files")
    for pattern in _strings(p.get("fail_if", []), "fail_if"):
        try:
            re.compile(pattern)
        except re.error as e:
            raise ProfileError(f"fail_if {pattern!r} isn't a valid regular expression: {e}") from None
    if p.get("media", "none") not in ("scan", "none"):
        raise ProfileError("media is \"scan\" or \"none\"")
    sb = p.get("sandbox", {})
    if sb is not False:
        if not isinstance(sb, dict):
            raise ProfileError("sandbox is an object with read/write lists, or false")
        _strings(sb.get("read", []), "sandbox.read")
        _strings(sb.get("write", []), "sandbox.write")
    if not isinstance(p.get("vars", {}), dict) or not all(isinstance(v, str) for v in p.get("vars", {}).values()):
        raise ProfileError("vars maps names to strings")
    probe = p.get("fps_probe")
    if probe is not None:
        if not isinstance(probe, dict) or not isinstance(probe.get("pattern"), str):
            raise ProfileError("fps_probe needs \"args\" and a \"pattern\" with one group")
        _strings(probe.get("args"), "fps_probe.args")
        re.compile(probe["pattern"])
    probe = p.get("frames_probe")
    if probe is not None:
        if not isinstance(probe, dict) or not (isinstance(probe.get("json"), str) ^ isinstance(probe.get("json_seconds"), str)):
            raise ProfileError("frames_probe needs \"args\" and one of \"json\" (frames) or \"json_seconds\"")
        _strings(probe.get("args"), "frames_probe.args")
    for ext in _strings(p.get("extensions", []), "extensions"):
        if not ext.startswith("."):
            raise ProfileError(f"extension {ext!r} must start with '.'")
    return p


def load(home: Path | None = None) -> dict[str, dict]:
    """Shipped profiles, then the user's (which win). Broken files are reported and skipped."""
    found: dict[str, dict] = {}
    for folder in (PACKAGED, (home or config.home()) / "apps"):
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.json")):
            try:
                p = validate(json.loads(f.read_text()))
            except (ValueError, OSError) as e:
                print(f"macmesh: skipping app profile {f}: {e}", file=sys.stderr)
                continue
            found[p["name"]] = dict(p, source=str(f))
    return found


def tool_entry(p: dict) -> dict:
    """The profile as an entry of the config's `tools` table."""
    steps = (p.get("range") or {}).get("steps", [])
    entry = {"path": p["program"]["path"], "description": p.get("description", ""),
             "usage": " ; then ".join(" ".join([p["name"], *step]) for step in steps) or p.get("usage", "")}
    if p["program"].get("version_args"):
        entry["version_args"] = p["program"]["version_args"]
    if "sandbox" in p:
        entry["sandbox"] = p["sandbox"]
    return entry
