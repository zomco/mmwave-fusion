"""Fusion core with no integration runtime.

HA and any future Docker shell import this package. Tracks, zone events,
quality scores, SQLite, frame decode, and clip admission live here.
"""

from .events import ZoneEventEngine
from .frames import parse_target_frame
from .fusion import FusionEngine, Observation, transform_point
from .quality import TrajectoryQualityEngine
from .recording import plan_recordings
from .storage import TrajectoryStore

__all__ = [
    "FusionEngine",
    "Observation",
    "TrajectoryQualityEngine",
    "TrajectoryStore",
    "ZoneEventEngine",
    "parse_target_frame",
    "plan_recordings",
    "transform_point",
]
