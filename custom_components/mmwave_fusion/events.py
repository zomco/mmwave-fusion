"""Compatibility shim. Implementation lives in the mmwave-engine package."""

from ._engine_path import ensure_mmwave_engine

ensure_mmwave_engine()

from mmwave_engine.events import ZoneEventEngine  # noqa: F401
