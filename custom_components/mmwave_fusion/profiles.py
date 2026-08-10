"""Validation helpers for reusable radar calibration profiles."""

from __future__ import annotations

import time
from math import isfinite
from typing import Any

from .const import SPATIAL_MODELS

CALIBRATION_KEYS = ("radar_x", "radar_y", "radar_z", "yaw", "pitch", "roll")


def normalize_calibration_profile(
    raw: dict[str, Any],
    previous: dict[str, Any] | None = None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Return a versioned, JSON-safe calibration profile."""

    profile_id = str(raw.get("profile_id") or "").strip()
    if not profile_id or len(profile_id) > 160:
        raise ValueError("profile_id must contain 1 to 160 characters")
    device_id = str(raw.get("device_id") or "").strip()
    if not device_id:
        raise ValueError("device_id is required")
    radar_model = str(raw.get("radar_model") or "").strip()
    if radar_model not in SPATIAL_MODELS:
        raise ValueError(f"Unsupported spatial radar model: {radar_model!r}")
    raw_calibration = raw.get("calibration")
    if not isinstance(raw_calibration, dict):
        raise ValueError("calibration must be an object")
    calibration: dict[str, float | list[object]] = {}
    for key in CALIBRATION_KEYS:
        value = raw_calibration.get(key)
        if not isinstance(value, (int, float)) or not isfinite(float(value)):
            raise ValueError(f"calibration.{key} must be a finite number")
        calibration[key] = round(float(value), 3)
    polygon = raw_calibration.get("polygon")
    calibration["polygon"] = polygon if isinstance(polygon, list) else []
    residual = raw.get("residual_cm")
    if residual is not None and (
        not isinstance(residual, (int, float))
        or not isfinite(float(residual))
        or float(residual) < 0
    ):
        raise ValueError("residual_cm must be a non-negative finite number")
    revision = int(previous.get("revision", 0)) + 1 if previous else 1
    return {
        "profile_id": profile_id,
        "device_id": device_id,
        "radar_model": radar_model,
        "name": str(raw.get("name") or profile_id).strip()[:160],
        "calibration": calibration,
        "revision": revision,
        "residual_cm": round(float(residual), 2) if residual is not None else None,
        "updated_at": float(now if now is not None else time.time()),
    }
