"""Which events keep a still and a clip. No Home Assistant, no camera I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class RecordingDecision:
    camera_entity_id: str
    status: str
    retry_after_s: float | None = None
    clip: dict[str, object] | None = None
    lookback: int = 0
    duration: int = 0
    recording_key: tuple[str, str, str] | None = None
    absolute_path: str = ""
    buffer_truncated: bool = False


def plan_recordings(
    event: dict[str, object],
    cameras: list[dict[str, object]],
    last_recordings: dict[tuple[str, str, str], float],
    now: float,
    fusion_id: str,
    media_root: str = "/media",
) -> list[RecordingDecision]:
    """Return one decision per camera. Scheduled plans include a clip row."""

    metadata = event.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    decisions: list[RecordingDecision] = []
    for camera in cameras:
        entity_id = str(camera["entity_id"])
        zones = camera.get("zones") or []
        if zones and event["zone_id"] not in zones:
            decisions.append(RecordingDecision(entity_id, "zone_filtered"))
            continue
        if event["event_type"] not in (camera.get("event_types") or []):
            decisions.append(RecordingDecision(entity_id, "event_type_filtered"))
            continue
        recording_key = (entity_id, str(event["zone_id"]), str(event["event_type"]))
        event_timestamp = float(event["timestamp"])
        last_recording = last_recordings.get(recording_key)
        cooldown = int(camera.get("cooldown_s") or 0)
        if last_recording is not None and event_timestamp - last_recording < cooldown:
            decisions.append(
                RecordingDecision(
                    entity_id,
                    "cooldown",
                    retry_after_s=round(cooldown - (event_timestamp - last_recording), 1),
                )
            )
            continue
        base_lookback = int(camera.get("lookback") or 0)
        trajectory_start = float(metadata.get("start_ts", event_timestamp))
        requested_lookback = max(0, ceil(event_timestamp - trajectory_start) + base_lookback)
        lookback = min(requested_lookback, int(camera.get("buffer_seconds") or 0))
        duration = int(camera.get("duration") or 10)
        safe_camera = re.sub(r"[^a-zA-Z0-9_-]+", "_", entity_id)
        date_path = datetime.fromtimestamp(event_timestamp).strftime("%Y-%m-%d")  # noqa: DTZ006
        clip_id = uuid4().hex
        relative_path = f"mmwave_fusion/{fusion_id}/{date_path}/{event['event_id']}_{safe_camera}.mp4"
        clip = {
            "clip_id": clip_id,
            "event_id": event["event_id"],
            "camera_entity_id": entity_id,
            "path": relative_path,
            "requested_at": now,
            "start_ts": event_timestamp - lookback,
            "end_ts": event_timestamp + duration,
            "status": "waiting",
            "provider": "ha_live",
            "updated_at": now,
            "completed_at": None,
            "file_size": None,
            "error": None,
            "review_verdict": None,
            "review_summary": None,
            "review_error": None,
            "snapshot_path": None,
        }
        decisions.append(
            RecordingDecision(
                entity_id,
                "scheduled",
                clip=clip,
                lookback=lookback,
                duration=duration,
                recording_key=recording_key,
                absolute_path=f"{media_root.rstrip('/')}/{relative_path}",
                buffer_truncated=requested_lookback > lookback,
            )
        )
    return decisions
