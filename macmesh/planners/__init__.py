"""Planners turn a request ("export this", "render frames 0-1200") into a job spec."""

from .app import plan_app
from .batch import plan_batch
from .range import plan_range
from .video import ENCODE_PRESETS, plan_video

PLANNERS = {"video": plan_video, "range": plan_range, "batch": plan_batch, "app": plan_app}
# Parameters an AI-written plan may not set (they'd let it choose what runs on this Mac).
PRIVATE_PARAMS = {"cfg", "home"}

__all__ = ["PLANNERS", "PRIVATE_PARAMS", "ENCODE_PRESETS", "plan_video", "plan_range", "plan_batch", "plan_app"]
