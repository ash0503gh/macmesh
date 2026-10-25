"""How many tasks a Mac runs at once: found by measuring, never set by hand.

Some tools leave the GPU or most cores idle (running four at once can be twice as fast); others
use the whole Mac (two at once is slower than one). So each Mac climbs: it runs n tasks at once,
measures the work it finishes per second (task cost, e.g. seconds of audio, per second), and tries
n + 1 while that keeps rising by GAIN, stepping back when it doesn't. It never adds a task while
memory is short or the Mac is already at its owner's limit, and drops one at once when memory runs
low. The best n per program is remembered (tuning.json), so the next job starts there.

A setting is only judged after a full round of tasks ran at it with the Mac kept full, so a quiet
moment (no work queued) or one slow file doesn't decide anything.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

GAIN = 1.05          # one more task at once must be at least this much faster to keep
MEM_ADD = 25         # % of memory free needed to add a task
MEM_LOW = 15         # % of memory free below which a task is dropped
MIN_WINDOW = 20.0    # seconds at a setting before judging it
RETRY_AFTER = 300.0  # a setting that didn't help is tried again after this
START = 2            # tasks at once for a program never seen before


def memory_free_percent() -> int | None:
    """macOS's own measure of free memory (0-100), or None if unreadable."""
    try:
        out = subprocess.run(["sysctl", "-n", "kern.memorystatus_level"], capture_output=True, text=True,
                             timeout=5).stdout.strip()
        return int(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


class AutoWorkers:
    def __init__(self, path: Path | None, max_n: int | None = None, memory=memory_free_percent,
                 at_limit=lambda: False):
        self.path = path
        self.max_n = max(1, max_n or os.cpu_count() or 1)
        self.memory = memory
        self.at_limit = at_limit  # the Mac is at its owner's limit (the governor is slowing work)
        self.lock = threading.Lock()
        self.best: dict[str, int] = self._load()
        self.program: str | None = None
        self.n = START
        self.rates: dict[int, float] = {}  # tasks at once -> work finished per second
        self.ceiling, self.ceiling_until = self.max_n, 0.0
        self._reset_window(None)
        self.idle_since: float | None = None
        self.note = ""  # why n is what it is, in plain words

    def _load(self) -> dict:
        try:
            return {k: int(v) for k, v in json.loads(self.path.read_text()).items()} if self.path else {}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save(self) -> None:
        if self.path:
            try:
                self.path.write_text(json.dumps(self.best, indent=1))
            except OSError:
                pass

    def _reset_window(self, now: float | None) -> None:
        """Start measuring afresh (from the next step, when `now` is None)."""
        self.since, self.work, self.finished_n, self.full_s, self.total_s = now, 0.0, 0, 0.0, 0.0

    # ---- what the worker tells it ------------------------------------------------------
    def started(self, program: str) -> None:
        """A task is starting: for a program not seen in this run, begin from what worked before."""
        with self.lock:
            if program != self.program:
                self.program = program
                self.n = min(self.max_n, self.best.get(program, START))
                self.rates = {}
                self.ceiling, self.ceiling_until = self.max_n, 0.0
                self._reset_window(None)
                self.note = f"starting at {self.n} ({'remembered' if program in self.best else 'first time'})"

    def finished(self, cost: float) -> None:
        with self.lock:
            self.work += max(cost, 0.0)
            self.finished_n += 1

    # ---- every couple of seconds ---------------------------------------------------------
    def step(self, running: int, waiting: int, now: float | None = None, dt: float = 2.0) -> int:
        """`running`: tasks running now; `waiting`: tasks queued here for a free slot."""
        now = time.time() if now is None else now
        with self.lock:
            if self.since is None:
                self.since = now
            if running == 0 and waiting == 0:
                if self.idle_since is not None and now - self.idle_since > 60 and self.program:
                    self._remember()
                return self.n
            self.idle_since = now
            self.total_s += dt
            if running + waiting >= self.n:
                self.full_s += dt  # it had as much work as it could take
            mem = self.memory()
            if mem is not None and mem < MEM_LOW and self.n > 1:
                return self._set(self.n - 1, now, f"memory low ({mem}% free)", ceiling=True)
            if self.at_limit() and self.n > 1 and self.total_s > MIN_WINDOW:
                return self._set(self.n - 1, now, "at the owner's limit", ceiling=True)
            window = now - self.since
            if window < MIN_WINDOW or self.finished_n < self.n or self.full_s < 0.8 * self.total_s:
                return self.n  # not enough seen at this setting yet
            rate = self.work / window
            self.rates[self.n] = rate
            lower = self.rates.get(self.n - 1)
            if lower is not None and rate < lower * GAIN:
                return self._set(self.n - 1, now, f"{self.n} at once wasn't faster than {self.n - 1}", ceiling=True)
            ceiling = self.ceiling if now < self.ceiling_until else self.max_n
            room = mem is None or mem >= MEM_ADD
            if self.n < ceiling and room and not self.at_limit():
                return self._set(self.n + 1, now, f"trying {self.n + 1} at once")
            self._remember()
            self._reset_window(now)
            self.note = f"{self.n} at once is fastest so far"
            return self.n

    def _set(self, n: int, now: float, why: str, ceiling: bool = False) -> int:
        self.n = max(1, min(self.max_n, n))
        if ceiling:
            self.ceiling, self.ceiling_until = self.n, now + RETRY_AFTER
            self._remember()
        self.note = why
        self._reset_window(now)
        return self.n

    def _remember(self) -> None:
        if self.program and self.rates:
            fastest = max(self.rates, key=self.rates.get)
            self.best[self.program] = min(fastest, self.n) if self.ceiling_until else fastest
            self._save()
