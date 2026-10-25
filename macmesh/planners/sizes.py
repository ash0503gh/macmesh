"""Chunk sizes that start big and shrink toward the end (guided self-scheduling).

With equal chunks, the job ends when the last chunk on the slowest Mac ends. Taking a share of what
is left each time (here half of an equal split) gives big chunks while there's plenty of work and
small ones at the end, so the Macs finish close together. The scheduler hands out the longest first.
"""

from __future__ import annotations

import math


def shrinking(total: float, workers: int, smallest: float) -> list[float]:
    """Sizes adding up to `total`: each about what's left / (2 x workers), never below `smallest`."""
    workers, sizes, left = max(1, workers), [], float(total)
    while left > 1e-9:
        size = min(left, max(smallest, left / (2 * workers)))
        if left - size < smallest / 2:  # don't leave a sliver
            size = left
        sizes.append(size)
        left -= size
    return sizes


def shrinking_frames(count: int, workers: int, smallest: int) -> list[int]:
    """The same in whole frames."""
    sizes = [max(1, math.floor(s)) for s in shrinking(count, workers, smallest)]
    sizes[-1] += count - sum(sizes)  # rounding goes into the last chunk
    return [s for s in sizes if s > 0]
