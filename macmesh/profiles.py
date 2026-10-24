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
  "fps_probe": {"args": ["info", "{project}"], "pattern": "\"fps\":\\s*([0-9.]+)"},
  "range": {
    "steps": [["relink", "{in:project}", "{indir}"],
              ["render", "{in:project}", "--from", "{start}", "--to", "{end_excl}", "-o", "{out:result}"]],
    "out_ext": ".mov",                              # "" when a chunk is a folder of frames
    "merge": "concat"                               # concat | dirs | none | auto
  },
  "fail_if": ["\"missing\":\\s*[1-9]"]          # regexes: output that means failure despite exit 0
}

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
    if not isinstance(rng, dict) or not isinstance(rng.get("steps"), list) or not rng["steps"]:
        raise ProfileError("\"range\" needs \"steps\": a list of argument lists")
    for i, step in enumerate(rng["steps"]):
        _strings(step, f"range.steps[{i}]")
    if rng.get("merge", "auto") not in ("auto", "concat", "dirs", "none"):
        raise ProfileError("range.merge is auto, concat, dirs or none")
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
    entry = {"path": p["program"]["path"], "description": p.get("description", ""),
             "usage": " ; then ".join(" ".join([p["name"], *step]) for step in p["range"]["steps"])}
    if p["program"].get("version_args"):
        entry["version_args"] = p["program"]["version_args"]
    if "sandbox" in p:
        entry["sandbox"] = p["sandbox"]
    return entry
