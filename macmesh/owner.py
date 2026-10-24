"""Is this Mac's owner using it? Measured so macmesh can step aside.

CPU is attributed exactly: everything that isn't a macmesh task (or the worker
itself) counts as the owner's. GPU use can't be split by process without root
(powermetrics), so it's read as a total and only trusted while the owner is at
the keyboard; once our tasks are paused the reading is the owner's alone.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

from .sysinfo import IS_MAC


def _run(*argv: str) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def owner_cpu(ours: set[int]) -> float:
    """CPU used by everything outside the process groups in `ours`, as a share of the whole Mac."""
    ours = ours | {os.getpgrp()}
    total = 0.0
    for line in _run("ps", "-A", "-o", "pgid=,pcpu=").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) not in ours:
            try:
                total += float(parts[1])
            except ValueError:
                pass
    return total / (100.0 * (os.cpu_count() or 1))


def gpu_utilization() -> float | None:
    """Whole-GPU busy share (0-1) from the IOAccelerator statistics, or None if unreadable."""
    if not IS_MAC:
        return None
    m = re.search(r'"Device Utilization %"=(\d+)', _run("ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"))
    return int(m.group(1)) / 100.0 if m else None


def idle_seconds() -> float | None:
    """Seconds since the last keyboard, mouse or trackpad input."""
    if not IS_MAC:
        return None
    m = re.search(r'"HIDIdleTime" = (\d+)', _run("ioreg", "-c", "IOHIDSystem", "-d", "4"))
    return int(m.group(1)) / 1e9 if m else None


class OwnerWatch:
    """Decides, sample by sample, whether the owner needs the Mac.

    busy() takes the process groups of our running tasks and whether they're
    paused right now (then every GPU cycle is the owner's). It returns a reason
    string while the owner is busy, else "". `sample` is injectable for tests.
    """

    GPU_BACKOFF = 120.0  # after a GPU reading turned out to be ours, ignore the GPU this long

    def __init__(self, policy: dict, sample=None):
        self.policy = policy
        self.sample = sample or self._sample
        self.gpu_ignored_until = 0.0
        self.gpu_triggered = False

    @staticmethod
    def _sample(ours: set[int]) -> dict:
        return {"cpu": owner_cpu(ours), "gpu": gpu_utilization(), "idle": idle_seconds()}

    def busy(self, ours: set[int], paused: bool) -> str:
        p, s, now = self.policy, self.sample(ours), time.time()
        idle = s["idle"]
        if p.get("mode") == "idle":
            if idle is not None and idle < p["idle_seconds"]:
                return f"owner active {idle:.0f}s ago"
            return ""
        if s["cpu"] > p["cpu"]:
            self.gpu_triggered = False
            return f"owner using {s['cpu']:.0%} CPU"
        gpu = s["gpu"]
        if gpu is None:
            return ""
        if paused and self.gpu_triggered:
            if gpu > p["gpu"]:
                return f"owner using {gpu:.0%} GPU"
            # We paused for the GPU and it went quiet at once: it was our own task.
            self.gpu_triggered = False
            self.gpu_ignored_until = now + self.GPU_BACKOFF
            return ""
        at_keyboard = idle is not None and idle < 10
        if gpu > p["gpu"] and (paused or at_keyboard) and now >= self.gpu_ignored_until:
            self.gpu_triggered = True
            return f"owner using {gpu:.0%} GPU"
        return ""
