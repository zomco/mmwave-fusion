"""Compatibility shim. Implementation lives in the mmwave-engine package."""

from ._engine_path import ensure_mmwave_engine

ensure_mmwave_engine()

from mmwave_engine.quality import (  # noqa: E402,F401
    TrajectoryAssessment,
    TrajectoryQualityEngine,
    TrajectorySample,
    assess_trajectory,
)
