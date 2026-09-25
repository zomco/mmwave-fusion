"""Compatibility shim. The implementation is engine.fusion."""

from .engine.fusion import (  # noqa: F401
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
