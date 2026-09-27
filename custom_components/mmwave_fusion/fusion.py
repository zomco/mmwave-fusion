"""Compatibility shim. Implementation lives in the mmwave-engine package."""

from ._engine_path import ensure_mmwave_engine

ensure_mmwave_engine()

from mmwave_engine.fusion import (  # noqa: E402,F401
    FusedTrack,
    FusionEngine,
    Observation,
    StepResult,
    _minimum_cost_assignment,
    observations_in_room,
    observations_inside,
    point_in_polygon,
    transform_point,
)
