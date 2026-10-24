"""Planners turn a request ("export this", "render frames 0-1200") into a job spec."""

from .batch import plan_batch
from .range import plan_range
from .video import ENCODE_PRESETS, plan_video

PLANNERS = {"video": plan_video, "range": plan_range, "batch": plan_batch}

__all__ = ["PLANNERS", "ENCODE_PRESETS", "plan_video", "plan_range", "plan_batch"]
