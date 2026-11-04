"""Scripts sent with a job: Python, zsh and bash, run in the sandbox on whichever Mac takes the task.

The script is one of the task's inputs ({in:script}); the program is an interpreter every Mac has:
- "python": macmesh's own Python (standard library only);
- "python-<id>": a Python with packages the job names. Each Mac builds it once, in
  ~/.macmesh/tools/python-<id>, after its owner clicks Allow on the exact list. <id> is a hash of
  the list, so the name stands for those packages and nothing else. pip only takes ready-built
  wheels (--only-binary), so no package's own setup code runs while installing;
- "zsh", "bash": the system shells, for scripts that call programs the Macs have.

Interpreters are only offered while this Mac sandboxes tasks: without it, a script from another
Mac could do anything this Mac's owner can.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from . import config

SHELLS = {"zsh": "/bin/zsh", "bash": "/bin/bash"}
ENV_PREFIX = "python-"
MAX_PACKAGES = 40
# "numpy", "numpy==2.5.4", "scikit-learn>=1.7,<2", "opencv-python-headless", "torch[cpu]": names and
# version specifiers only, never URLs, paths or pip options.
_VERSION = r"(===|==|!=|~=|>=|<=|<|>)[A-Za-z0-9.*+!_-]+"
PACKAGE = re.compile(rf"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?:\[[A-Za-z0-9._,-]+\])?(?:{_VERSION}(?:,{_VERSION})*)?$")
READY = "packages.json"  # written last: until it's there the Python isn't ready for tasks


def check_packages(packages) -> list[str]:
    """The packages, cleaned up and sorted, or ValueError."""
    if not isinstance(packages, list) or not packages or not all(isinstance(p, str) for p in packages):
        raise ValueError("packages must be a non-empty list of names, e.g. [\"numpy\", \"pandas==2.3.3\"]")
    out = sorted({p.strip() for p in packages}, key=str.lower)
    if len(out) > MAX_PACKAGES:
        raise ValueError(f"at most {MAX_PACKAGES} packages")
    for p in out:
        if not PACKAGE.match(p):
            raise ValueError(f"{p!r} isn't a package name with an optional version (no URLs, paths or options)")
    return out


def env_name(packages: list[str]) -> str:
    """The program name for a Python with these packages: the same on every Mac."""
    key = json.dumps([p.lower() for p in check_packages(packages)])
    return ENV_PREFIX + hashlib.sha256(key.encode()).hexdigest()[:12]


def is_env(name: str) -> bool:
    return bool(re.fullmatch(ENV_PREFIX + r"[0-9a-f]{12}", name))


def env_recipe(packages: list[str]) -> dict:
    """An install recipe (install.py) for a Python with these packages."""
    packages = check_packages(packages)
    shown = ", ".join(packages)
    return {
        "name": env_name(packages),
        "description": "Python packages another Mac's script needs. They're downloaded from PyPI as "
                       "ready-built packages; the script itself always runs in the sandbox.",
        "program": {"path": "{dir}/env/bin/python"},
        "install": {
            "summary": f"Python packages for a script: {shown}",
            "packages": packages,
            "size_gb": 2.0,
            "steps": [["{python}", "-m", "venv", "--copies", "{dir}/env"],
                      ["{dir}/env/bin/python", "-m", "pip", "install", "--only-binary", ":all:", "--no-input",
                       "--progress-bar", "off", *packages]],
            "files": {READY: json.dumps(packages, indent=1) + "\n"},
        },
    }


def envs(home: Path | None = None) -> dict[str, Path]:
    """The Pythons with packages that are ready on this Mac: {name: its python}."""
    found = {}
    tools = (home or config.home()) / "tools"
    for d in tools.glob(ENV_PREFIX + "*") if tools.is_dir() else []:
        python = d / "env/bin/python"
        if is_env(d.name) and (d / READY).is_file() and python.is_file():
            found[d.name] = python
    return found


def programs(home: Path | None = None) -> dict[str, str]:
    """Interpreter name -> executable, for those present here."""
    found = {"python": sys.executable}
    found.update({k: v for k, v in SHELLS.items() if Path(v).exists()})
    found.update({k: str(v) for k, v in envs(home).items()})
    return found


def tool_entries(home: Path | None = None) -> dict[str, dict]:
    """Their `tools` entries: what each may read inside the sandbox beyond its own folder."""
    # Build matching compares Python versions; shells aren't compared (see toolinfo.fingerprints).
    table = {"python": {"path": sys.executable, "version_args": ["--version"], "sandbox": {"read": ["{engine}"]}}}
    table.update({k: {"path": v} for k, v in SHELLS.items()})
    for name, python in envs(home).items():
        table[name] = {"path": str(python), "version_args": ["--version"],
                       "sandbox": {"read": [str(python.parent.parent), "{engine}"]}}
    return table


def use_packages(job: dict, packages: list | None) -> dict:
    """Have the job's "python" steps run on a Python with these packages (none: macmesh's own)."""
    if not packages:
        return job
    name = env_name(packages)
    used = False
    for t in job["tasks"]:
        for step in t.get("steps") or [t.get("argv")]:
            if step and step[0] == "python":
                step[0], used = name, True
    if not used:
        raise ValueError("packages are for Python scripts: start the command with \"python\"")
    job["envs"] = {name: check_packages(packages)}
    return job


def shown(program: str, envs: dict) -> str:
    """A program's name for people: a script's Python says which packages it has."""
    return f"Python with {', '.join(envs[program])}" if program in envs else program
