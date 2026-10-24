"""Run each task in a macOS sandbox, so a task can't harm or snoop on the Mac lending its power.

Inside the sandbox a task:
- has no IP networking (it can't phone home or reach other machines);
- can write only inside its own task folder (plus /dev/null and terminals);
- can't read the owner's home folder, apart from its task folder, the tool's
  own executable, ~/Library/Fonts and any paths the tool's config names.

Everything outside the home folder (system frameworks, /Applications,
Homebrew) stays readable so apps can load. `sandbox-exec` is deprecated by
Apple but still works; it's the only sandbox an unprivileged process can apply
to a program it starts.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .sysinfo import IS_MAC


def available() -> bool:
    return IS_MAC and shutil.which("sandbox-exec") is not None


def _q(path: str | Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _real(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def profile(task_dir: Path, exe: str, read: list[str] = (), write: list[str] = ()) -> str:
    home = _real(Path.home())
    task_dir = _real(task_dir)
    readable = [task_dir, _real(exe), home / "Library/Fonts", *(_real(p) for p in read), *(_real(p) for p in write)]
    writable = [task_dir, *(_real(p) for p in write)]
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network-outbound (remote ip))",
        "(deny network-inbound (local ip))",
        "(deny network-bind (local ip))",
        "(deny file-write*)",
        "(allow file-write* " + " ".join(f"(subpath {_q(p)})" for p in writable)
        + ' (literal "/dev/null") (literal "/dev/zero") (regex #"^/dev/tty") (regex #"^/dev/fd/"))',
        f"(deny file-read* (subpath {_q(home)}))",
        # Paths through ~ still resolve without reading ~ itself, so a task can't even list it.
        "(allow file-read* " + " ".join(f"(subpath {_q(p)})" for p in readable) + ")",
    ]
    return "\n".join(lines) + "\n"


def wrap(argv: list[str], task_dir: Path, tool_cfg: dict) -> list[str]:
    """argv run under the sandbox, or unchanged when the tool opts out."""
    rules = tool_cfg.get("sandbox", {})
    if rules is False:
        return argv
    prof = task_dir / "sandbox.sb"
    prof.write_text(profile(task_dir, argv[0], rules.get("read", []), rules.get("write", [])))
    return ["sandbox-exec", "-f", str(prof), *argv]
