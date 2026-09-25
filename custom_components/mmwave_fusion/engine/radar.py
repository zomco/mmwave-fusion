"""Turn a decoded 2-D target frame into room-frame observations."""

from __future__ import annotations

from math import hypot

from .frames import TargetFrame
from .fusion import Observation, transform_point


def observations_from_frame(
    radar_id: str,
    frame: TargetFrame,
    calibration: dict[str, object],
    *,
    scale: float,
    timestamp: float,
    weight: float,
) -> list[Observation]:
    """Map firmware frame targets into the shared room frame.

    1-D presence sensors do not produce a TargetFrame, so they never enter
    fusion through this adapter.
    """

    observations: list[Observation] = []
    for slot, target in enumerate(frame.targets):
        local_x = target.x * scale
        local_y = target.y * scale
        x, y, _ = transform_point(local_x, local_y, target.z * scale, calibration)
        observations.append(
            Observation(
                radar_id=radar_id,
                slot=slot,
                timestamp=timestamp,
                x=x,
                y=y,
                speed=target.speed * scale if target.speed is not None else None,
                weight=weight,
                frame_id=frame.frame_id,
                source_timestamp=frame.source_timestamp,
                range_cm=hypot(local_x, local_y),
            )
        )
    return observations
