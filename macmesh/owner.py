"""Is this Mac's owner using it? Measured so macmesh can step aside.

CPU is attributed exactly: everything that isn't a macmesh task (or the worker
itself) counts as the owner's. GPU use can't be split by process without root
(powermetrics), so the Governor keeps the whole GPU (and CPU) under the owner's
limit by slowing our tasks, rather than guessing whose GPU work it is.
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
    """Decides, sample by sample, whether the owner needs the Mac back entirely.

    In "shared" mode the Governor already slows our tasks so the owner has room on CPU and GPU;
    this only says "step aside" once the owner leaves us less than 10% of the CPU under their
    limit. In "idle" mode, any recent keyboard or mouse use does. busy() returns a reason
    string while the owner is busy, else "". `sample` is injectable for tests.
    """

    def __init__(self, policy: dict, sample=None):
        self.policy = policy
        self.sample = sample or self._sample

    @staticmethod
    def _sample(ours: set[int]) -> dict:
        return {"cpu": owner_cpu(ours), "idle": idle_seconds()}

    def busy(self, ours: set[int], paused: bool) -> str:
        p, s = self.policy, self.sample(ours)
        if p.get("mode") == "idle":
            idle = s["idle"]
            if idle is not None and idle < p["idle_seconds"]:
                return f"owner active {idle:.0f}s ago"
            return ""
        if s["cpu"] > min(0.95, max(0.5, float(p.get("limit", 0.9)))) - 0.10:
            return f"owner using {s['cpu']:.0%} CPU"
        return ""


class Governor:
    """Keeps the whole Mac, the owner's work and macmesh's together, at or under the owner's limit
    (90% unless they chose otherwise), for CPU and GPU alike.

    macmesh's tasks run for `duty` of every short period and are stopped (SIGSTOP) for the rest.
    Each step measures the Mac and nudges `duty`: down in proportion when the Mac is over the
    limit, back up gently when there's room. So an idle owner gets ~90% of the Mac working for
    others, and a busy owner gets whatever they need, with macmesh slowing rather than stopping.
    `sample` is injectable for tests: () -> {"cpu": 0-1 whole Mac, "gpu": 0-1 or None}.
    """

    FLOOR = 0.05   # never slower than this; if the owner leaves less room than that, the worker steps aside
    MARGIN = 0.05  # speed back up only when the Mac is this far under the limit

    def __init__(self, policy: dict, sample=None):
        self.policy = policy
        self.duty = 1.0
        self.last = {"cpu": None, "gpu": None}
        self.sample = sample or self._sample
        self._cpu_times: dict[int, float] = {}
        self._cpu_at = 0.0

    @property
    def limit(self) -> float:
        return min(0.95, max(0.5, float(self.policy.get("limit", 0.9))))

    def _sample(self) -> dict:
        from .sysinfo import gpu_percent, process_table
        now, table = time.time(), process_table()
        cpu = None
        if self._cpu_at:
            used = sum(max(0.0, t - self._cpu_times.get(pid, t)) for pid, (_, t) in table.items())
            cpu = used / max(now - self._cpu_at, 0.1) / (os.cpu_count() or 1)
        self._cpu_times, self._cpu_at = {pid: t for pid, (_, t) in table.items()}, now
        gpu = gpu_percent()
        return {"cpu": cpu, "gpu": None if gpu is None else gpu / 100.0}

    def step(self) -> float:
        s = self.sample()
        self.last = s
        loads = [v for v in (s.get("cpu"), s.get("gpu")) if v is not None]
        if not loads:
            return self.duty
        over = max(loads) - self.limit
        if over > 0.01:
            self.duty = max(self.FLOOR, self.duty * (1 - min(0.6, over * 2.5)))
        elif over < -self.MARGIN:
            self.duty = min(1.0, self.duty + 0.08)
        return self.duty

    def reset(self) -> None:
        """Nothing is running: start at full speed next time, measuring afresh."""
        self.duty, self._cpu_at = 1.0, 0.0
