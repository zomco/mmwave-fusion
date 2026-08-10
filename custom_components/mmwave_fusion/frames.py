"""Versioned atomic radar target-frame parsing."""

from __future__ import annotations

from dataclasses import dataclass
import json
from math import isfinite


@dataclass(frozen=True, slots=True)
class FrameTarget:
    x: float
    y: float
    z: float = 0.0
    speed: float | None = None


@dataclass(frozen=True, slots=True)
class TargetFrame:
    frame_id: str
    source_timestamp: float
    targets: tuple[FrameTarget, ...]


def parse_target_frame(value: str) -> TargetFrame | None:
    """Parse the v1 compact JSON frame published by radar firmware.

    V1 uses ``{"v":1,"f":42,"ts":1234,"t":[[x,y,speed], ...]}``.
    Object targets (x/y/z/speed) are accepted as well, which lets other radar
    components publish the same envelope without copying LD2450's array form.
    """

    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    frame_id = payload.get("f")
    timestamp = _number(payload.get("ts"))
    raw_targets = payload.get("t")
    if isinstance(frame_id, bool) or not isinstance(frame_id, (int, str)) or timestamp is None:
        return None
    if not isinstance(raw_targets, list) or len(raw_targets) > 32:
        return None

    targets: list[FrameTarget] = []
    for raw_target in raw_targets:
        if isinstance(raw_target, list) and 2 <= len(raw_target) <= 4:
            x = _number(raw_target[0])
            y = _number(raw_target[1])
            if len(raw_target) == 3:
                z = 0.0
                speed = _number(raw_target[2])
            else:
                z = _number(raw_target[2]) if len(raw_target) >= 4 else 0.0
                speed = _number(raw_target[3]) if len(raw_target) >= 4 else None
        elif isinstance(raw_target, dict):
            x = _number(raw_target.get("x"))
            y = _number(raw_target.get("y"))
            z = _number(raw_target.get("z", 0.0))
            speed = _number(raw_target.get("speed")) if raw_target.get("speed") is not None else None
        else:
            return None
        if x is None or y is None or z is None:
            return None
        if max(abs(x), abs(y), abs(z)) > 100_000:
            return None
        if x == 0 and y == 0 and z == 0:
            continue
        targets.append(FrameTarget(x=x, y=y, z=z, speed=abs(speed) if speed is not None else None))
    return TargetFrame(str(frame_id), timestamp, tuple(targets))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if isfinite(result) else None
