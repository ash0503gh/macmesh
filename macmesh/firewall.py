"""Is the macOS firewall going to stop other Macs reaching this one?

The engine listens for other Macs (the worker port, and the pairing port on the first Mac). With
the firewall on, macOS asks once whether the engine's Python may accept connections; if that was
denied or missed, other Macs can't connect and nothing says why. This reports the state so the
app can explain it and offer to allow it (which needs an administrator, through macOS's own prompt).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

TOOL = "/usr/libexec/ApplicationFirewall/socketfilterfw"
_cache: tuple[float, dict] | None = None


def _ask(*args: str) -> str:
    try:
        return subprocess.run([TOOL, *args], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def state(program: str | None = None) -> dict:
    """{"on", "block_all", "blocked", "program"}; re-read at most every 30 s."""
    global _cache
    if _cache and time.time() - _cache[0] < 30 and program is None:
        return _cache[1]
    program = os.path.realpath(program or sys.executable)
    if not os.path.exists(TOOL):
        result = {"on": False, "block_all": False, "blocked": False, "program": program}
    else:
        on = "enabled" in _ask("--getglobalstate").lower()
        block_all = on and "block all state set to enabled" in _ask("--getblockall").lower()
        verdict = _ask("--getappblocked", program).lower() if on else ""
        result = {"on": on, "block_all": block_all, "blocked": on and ("blocked" in verdict or block_all),
                  "program": program}
    _cache = (time.time(), result)
    return result
