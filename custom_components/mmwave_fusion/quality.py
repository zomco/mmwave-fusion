"""Compatibility shim. The implementation is engine.quality."""

from .engine.quality import (  # noqa: F401
    TrajectoryAssessment,
    TrajectoryQualityEngine,
    TrajectorySample,
    assess_trajectory,
)
