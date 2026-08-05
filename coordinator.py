"""Runtime coordinator for radar ingestion, fusion, persistence and recording."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
import logging
from pathlib import Path
import re
import time
from typing import Any
from uuid import uuid4

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store

from .archive import (
    ArchiveError,
    RecordingNotReadyError,
    async_extract_hikvision_clip,
    async_resolve_hikvision_source,
    redact_credentials,
    search_recordings,
)
from .const import (
    DEFAULT_ASSOCIATION_GATE_CM,
    DEFAULT_FRAME_DEBOUNCE_S,
    DEFAULT_FUSION_ID,
    DEFAULT_MERGE_GATE_CM,
    DEFAULT_POINT_FLUSH_S,
    DEFAULT_RATE_HZ,
    DEFAULT_TRACK_TTL_S,
    DOMAIN,
    ENTITY_OCCUPIED,
    ENTITY_TARGET_COUNT,
    EVENT_TYPE,
    MODEL_COORDINATE_SCALE,
    SIGNAL_UPDATE,
    SPATIAL_MODELS,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .events import ZoneEventEngine
from .frames import parse_target_frame
from .fusion import FusedTrack, FusionEngine, Observation, transform_point
from .storage import TrajectoryStore

_LOGGER = logging.getLogger(__name__)


class FusionCoordinator:
    """Own all configured floor fusion systems and the shared trajectory DB."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.config_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.trajectory_store = TrajectoryStore(hass.config.path(".storage", "mmwave_fusion.sqlite"))
        self.systems: dict[str, FusionSystem] = {}
        self.configs: dict[str, dict[str, Any]] = {}

    async def async_initialize(self) -> None:
        await self.hass.async_add_executor_job(self.trajectory_store.initialize)
        interrupted = await self.hass.async_add_executor_job(
            self.trajectory_store.fail_incomplete_clips,
            "Home Assistant restarted before extraction completed",
        )
        if interrupted:
            _LOGGER.warning("Marked %s interrupted recording request(s) as failed", interrupted)
        stored = await self.config_store.async_load() or {}
        systems = stored.get("systems", {})
        if isinstance(systems, dict):
            for fusion_id, config in systems.items():
                if isinstance(config, dict):
                    try:
                        await self.async_configure({**config, "fusion_id": fusion_id}, persist=False)
                    except ValueError as error:
                        _LOGGER.error("Ignoring invalid stored fusion system %s: %s", fusion_id, error)

    async def async_configure(self, config: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        normalized = normalize_config(config)
        fusion_id = normalized["fusion_id"]
        if self.configs.get(fusion_id) == normalized and fusion_id in self.systems:
            return self.systems[fusion_id].status()
        previous = self.systems.pop(fusion_id, None)
        if previous is not None:
            await previous.async_stop()

        system = FusionSystem(self.hass, self.trajectory_store, normalized)
        self.systems[fusion_id] = system
        self.configs[fusion_id] = normalized
        await system.async_start()
        if persist:
            await self.config_store.async_save({"systems": self.configs})
        return system.status()

    async def async_shutdown(self) -> None:
        for system in tuple(self.systems.values()):
            await system.async_stop()
        await self.hass.async_add_executor_job(self.trajectory_store.close)

    async def async_remove(self, fusion_id: str) -> bool:
        system = self.systems.pop(fusion_id, None)
        self.configs.pop(fusion_id, None)
        if system is None:
            return False
        await system.async_stop()
        system.remove_summary_states()
        await self.config_store.async_save({"systems": self.configs})
        return True

    def get_config(self, fusion_id: str) -> dict[str, Any] | None:
        return self.configs.get(fusion_id)

    def get_status(self, fusion_id: str) -> dict[str, Any] | None:
        system = self.systems.get(fusion_id)
        return system.status() if system else None


class FusionSystem:
    def __init__(self, hass: HomeAssistant, storage: TrajectoryStore, config: dict[str, Any]) -> None:
        self.hass = hass
        self.storage = storage
        self.config = config
        self.fusion_id = str(config["fusion_id"])
        settings = config["fusion"]
        self.engine = FusionEngine(
            association_gate_cm=float(settings["association_gate_cm"]),
            merge_gate_cm=float(settings["merge_gate_cm"]),
            track_ttl_s=float(settings["track_ttl_s"]),
            confirm_hits=int(settings["confirm_hits"]),
        )
        self.events = ZoneEventEngine(self.fusion_id, config["zones"])
        self._pending: list[Observation] = []
        self._radar_by_entity: dict[str, str] = {}
        self._radars = {str(radar["id"]): radar for radar in config["radars"]}
        self._last_signatures: dict[str, tuple[str, ...]] = {}
        self._last_camera_recordings: dict[tuple[str, str, str], float] = {}
        self._flush_tasks: dict[str, asyncio.Task[None]] = {}
        self._point_buffer: list[tuple[str, float, FusedTrack]] = []
        self._last_point_flush = time.time()
        self._latest_tracks: tuple[FusedTrack, ...] = ()
        self._remove_listener: Callable[[], None] | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._clip_tasks: set[asyncio.Task[None]] = set()
        self._last_summary_signature: tuple[object, ...] | None = None

    async def async_start(self) -> None:
        for radar_id, radar in self._radars.items():
            for entity_id in radar_entity_ids(radar):
                self._radar_by_entity[entity_id] = radar_id
        if self._radar_by_entity:
            self._remove_listener = async_track_state_change_event(
                self.hass,
                list(self._radar_by_entity),
                self._state_changed,
            )
        self._tick_task = self.hass.async_create_background_task(
            self._tick_loop(),
            f"mmwave_fusion_{self.fusion_id}",
        )
        for radar_id in self._radars:
            self._schedule_radar_flush(radar_id)

    async def async_stop(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        for task in self._flush_tasks.values():
            task.cancel()
        self._flush_tasks.clear()
        if self._tick_task is not None:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None
        if self._clip_tasks:
            for task in self._clip_tasks:
                task.cancel()
            await asyncio.gather(*self._clip_tasks, return_exceptions=True)
            self._clip_tasks.clear()
        if self._point_buffer:
            points, self._point_buffer = self._point_buffer, []
            await self.hass.async_add_executor_job(self.storage.append_points, points)

    @callback
    def _state_changed(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        radar_id = self._radar_by_entity.get(entity_id)
        if radar_id is not None:
            radar = self._radars[radar_id]
            if entity_id == radar.get("frame_entity"):
                self._read_radar_frame(radar_id)
            else:
                self._schedule_radar_flush(radar_id)

    @callback
    def _schedule_radar_flush(self, radar_id: str) -> None:
        previous = self._flush_tasks.get(radar_id)
        if previous is not None and not previous.done():
            previous.cancel()
        self._flush_tasks[radar_id] = self.hass.async_create_background_task(
            self._flush_radar_after_delay(radar_id),
            f"mmwave_fusion_frame_{self.fusion_id}_{radar_id}",
        )

    async def _flush_radar_after_delay(self, radar_id: str) -> None:
        try:
            await asyncio.sleep(DEFAULT_FRAME_DEBOUNCE_S)
            self._read_radar_frame(radar_id)
        except asyncio.CancelledError:
            return

    @callback
    def _read_radar_frame(self, radar_id: str) -> None:
        radar = self._radars[radar_id]
        if self._read_atomic_frame(radar_id, radar):
            return
        presence_entity = radar.get("presence_entity")
        if presence_entity:
            presence = self.hass.states.get(str(presence_entity))
            if presence is None or presence.state in (STATE_UNAVAILABLE, STATE_UNKNOWN) or presence.state != STATE_ON:
                return

        signature: list[str] = []
        timestamp = time.time()
        observations: list[Observation] = []
        calibration = radar["calibration"]
        for slot, target in enumerate(radar["targets"]):
            x_state = self.hass.states.get(str(target["x_entity"]))
            y_state = self.hass.states.get(str(target["y_entity"]))
            if x_state is None or y_state is None:
                continue
            signature.extend((x_state.last_updated.isoformat(), y_state.last_updated.isoformat()))
            try:
                raw_x = float(x_state.state) * entity_coordinate_scale(radar, x_state)
                raw_y = float(y_state.state) * entity_coordinate_scale(radar, y_state)
            except (TypeError, ValueError):
                continue
            if raw_x == 0 and raw_y == 0:
                continue
            raw_z = 0.0
            if target.get("z_entity"):
                z_state = self.hass.states.get(str(target["z_entity"]))
                if z_state is not None:
                    signature.append(z_state.last_updated.isoformat())
                    try:
                        raw_z = float(z_state.state) * entity_coordinate_scale(radar, z_state)
                    except (TypeError, ValueError):
                        raw_z = 0.0
            speed: float | None = None
            if target.get("speed_entity"):
                speed_state = self.hass.states.get(str(target["speed_entity"]))
                if speed_state is not None:
                    signature.append(speed_state.last_updated.isoformat())
                    try:
                        speed = abs(float(speed_state.state)) * entity_coordinate_scale(radar, speed_state)
                    except (TypeError, ValueError):
                        speed = None
            timestamp = max(timestamp, x_state.last_updated.timestamp(), y_state.last_updated.timestamp())
            x, y, _ = transform_point(raw_x, raw_y, raw_z, calibration)
            observations.append(
                Observation(
                    radar_id=radar_id,
                    slot=slot,
                    timestamp=timestamp,
                    x=x,
                    y=y,
                    speed=speed,
                    weight=float(radar["measurement_weight"]),
                )
            )

        frame_signature = tuple(signature)
        if frame_signature and frame_signature == self._last_signatures.get(radar_id):
            return
        self._last_signatures[radar_id] = frame_signature
        self._pending.extend(observations)

    @callback
    def _read_atomic_frame(self, radar_id: str, radar: dict[str, Any]) -> bool:
        frame_entity = radar.get("frame_entity")
        if not frame_entity:
            return False
        state = self.hass.states.get(str(frame_entity))
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False
        if time.time() - state.last_updated.timestamp() > float(radar["frame_stale_after_s"]):
            return False
        frame = parse_target_frame(state.state)
        if frame is None:
            return False
        signature = (frame.frame_id, str(frame.source_timestamp))
        if signature == self._last_signatures.get(radar_id):
            return True
        self._last_signatures[radar_id] = signature

        scale = float(radar["frame_coordinate_scale"])
        timestamp = state.last_updated.timestamp()
        calibration = radar["calibration"]
        for slot, target in enumerate(frame.targets):
            x, y, _ = transform_point(target.x * scale, target.y * scale, target.z * scale, calibration)
            self._pending.append(
                Observation(
                    radar_id=radar_id,
                    slot=slot,
                    timestamp=timestamp,
                    x=x,
                    y=y,
                    speed=target.speed * scale if target.speed is not None else None,
                    weight=float(radar["measurement_weight"]),
                    frame_id=frame.frame_id,
                    source_timestamp=frame.source_timestamp,
                )
            )
        return True

    async def _tick_loop(self) -> None:
        interval = 1.0 / float(self.config["fusion"]["rate_hz"])
        while True:
            started_at = time.monotonic()
            try:
                await self._step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the long-running fusion task alive
                _LOGGER.exception("Fusion step failed for %s", self.fusion_id)
            elapsed = time.monotonic() - started_at
            await asyncio.sleep(max(interval - elapsed, 0.01))

    async def _step(self) -> None:
        now = time.time()
        observations, self._pending = self._pending, []
        result = self.engine.step(observations, now)
        self._latest_tracks = result.tracks
        for track in result.started:
            await self.hass.async_add_executor_job(self.storage.start_track, self.fusion_id, track)
        if result.ended_track_ids:
            await self.hass.async_add_executor_job(self.storage.end_tracks, result.ended_track_ids, now)

        for track in result.tracks:
            self._point_buffer.append((self.fusion_id, now, track))
        if self._point_buffer and now - self._last_point_flush >= DEFAULT_POINT_FLUSH_S:
            points, self._point_buffer = self._point_buffer, []
            self._last_point_flush = now
            await self.hass.async_add_executor_job(self.storage.append_points, points)

        zone_events = self.events.evaluate(result.tracks, now)
        for zone_event in zone_events:
            await self.hass.async_add_executor_job(self.storage.insert_event, zone_event)
            self.hass.bus.async_fire(EVENT_TYPE, zone_event)
            await self._record_event(zone_event)

        payload = {
            "fusion_id": self.fusion_id,
            "timestamp": now,
            "tracks": [track.as_dict() for track in result.tracks],
            "events": zone_events,
            "radars": self._radar_health(),
        }
        async_dispatcher_send(self.hass, f"{SIGNAL_UPDATE}_{self.fusion_id}", payload)
        self._publish_summary(payload)

    async def _record_event(self, event: dict[str, object]) -> None:
        for camera in self.config["cameras"]:
            if camera["zones"] and event["zone_id"] not in camera["zones"]:
                continue
            if event["event_type"] not in camera["event_types"]:
                continue
            recording_key = (str(camera["entity_id"]), str(event["zone_id"]), str(event["event_type"]))
            event_timestamp = float(event["timestamp"])
            last_recording = self._last_camera_recordings.get(recording_key)
            if last_recording is not None and event_timestamp - last_recording < int(camera["cooldown_s"]):
                continue
            self._last_camera_recordings[recording_key] = event_timestamp
            lookback = int(camera["lookback"])
            duration = int(camera["duration"])
            entity_id = str(camera["entity_id"])
            safe_camera = re.sub(r"[^a-zA-Z0-9_-]+", "_", entity_id)
            date_path = datetime.fromtimestamp(float(event["timestamp"])).strftime("%Y-%m-%d")
            clip_id = uuid4().hex
            relative_path = f"mmwave_fusion/{self.fusion_id}/{date_path}/{event['event_id']}_{safe_camera}.mp4"
            filename = f"/media/{relative_path}"
            provider = str(camera["recording_source"])
            now = time.time()
            clip = {
                "clip_id": clip_id,
                "event_id": event["event_id"],
                "camera_entity_id": entity_id,
                "path": relative_path,
                "requested_at": now,
                "start_ts": float(event["timestamp"]) - lookback,
                "end_ts": float(event["timestamp"]) + duration,
                "status": "waiting" if provider in {"hikvision_sd", "hikvision_nvr"} else "requested",
                "provider": provider,
                "updated_at": now,
                "completed_at": None,
                "file_size": None,
                "error": None,
            }
            try:
                await self.hass.async_add_executor_job(Path(filename).parent.mkdir, 0o755, True, True)
                await self.hass.async_add_executor_job(self.storage.insert_clip, clip)
                if provider in {"hikvision_sd", "hikvision_nvr"}:
                    task = self.hass.async_create_background_task(
                        self._extract_hikvision_archive_clip(camera, clip, Path(filename)),
                        f"mmwave_fusion_archive_{clip_id}",
                    )
                    self._clip_tasks.add(task)
                    task.add_done_callback(self._clip_tasks.discard)
                    continue
                await self.hass.services.async_call(
                    "camera",
                    "record",
                    {
                        "entity_id": entity_id,
                        "filename": filename,
                        "lookback": lookback,
                        "duration": duration,
                    },
                    blocking=False,
                )
            except Exception:  # noqa: BLE001 - one camera must not stop fusion
                clip["status"] = "failed"
                clip["updated_at"] = time.time()
                clip["error"] = "Unable to request recording"
                await self.hass.async_add_executor_job(self.storage.insert_clip, clip)
                _LOGGER.exception("Unable to request recording from %s for event %s", entity_id, event["event_id"])

    async def _extract_hikvision_archive_clip(
        self,
        camera: dict[str, Any],
        clip: dict[str, object],
        filename: Path,
    ) -> None:
        """Wait for the camera archive, then extract an event interval without transcoding."""

        try:
            source = await async_resolve_hikvision_source(self.hass, camera)
            settle_s = int(camera["archive_settle_s"])
            initial_delay = max(float(clip["end_ts"]) + settle_s - time.time(), 0.0)
            if initial_delay:
                await asyncio.sleep(initial_delay)

            attempts = int(camera["archive_retries"]) + 1
            retry_interval = int(camera["archive_retry_interval_s"])
            last_error: ArchiveError | None = None
            for attempt in range(attempts):
                clip["status"] = "extracting"
                clip["updated_at"] = time.time()
                clip["error"] = None
                await self.hass.async_add_executor_job(self.storage.insert_clip, clip)
                try:
                    await self.hass.async_add_executor_job(
                        search_recordings,
                        source,
                        float(clip["start_ts"]),
                        float(clip["end_ts"]),
                    )
                    size = await async_extract_hikvision_clip(
                        source,
                        float(clip["start_ts"]),
                        float(clip["end_ts"]),
                        filename,
                    )
                except ArchiveError as error:
                    last_error = error
                    if attempt + 1 >= attempts:
                        break
                    clip["status"] = "waiting"
                    clip["updated_at"] = time.time()
                    clip["error"] = (
                        "Recording has not been indexed yet"
                        if isinstance(error, RecordingNotReadyError)
                        else redact_credentials(str(error))[-500:]
                    )
                    await self.hass.async_add_executor_job(self.storage.insert_clip, clip)
                    await asyncio.sleep(retry_interval)
                    continue

                clip["status"] = "ready"
                clip["updated_at"] = time.time()
                clip["completed_at"] = clip["updated_at"]
                clip["file_size"] = size
                clip["error"] = None
                await self.hass.async_add_executor_job(self.storage.insert_clip, clip)
                return

            if last_error is not None:
                raise last_error
            raise ArchiveError("Historical recording extraction exhausted all attempts")
        except asyncio.CancelledError:
            clip["status"] = "failed"
            clip["updated_at"] = time.time()
            clip["error"] = "Extraction cancelled before completion"
            await self.hass.async_add_executor_job(self.storage.insert_clip, clip)
            raise
        except Exception as error:  # noqa: BLE001 - archive failure must not stop fusion
            clip["status"] = "failed"
            clip["updated_at"] = time.time()
            clip["error"] = redact_credentials(str(error))[-500:]
            await self.hass.async_add_executor_job(self.storage.insert_clip, clip)
            _LOGGER.error(
                "Unable to extract historical recording from %s for event %s: %s",
                camera["entity_id"],
                clip["event_id"],
                clip["error"],
            )

    @callback
    def _publish_summary(self, payload: dict[str, object]) -> None:
        tracks = payload["tracks"]
        assert isinstance(tracks, list)
        radars = payload["radars"]
        assert isinstance(radars, list)
        stable_health = tuple((radar["id"], radar["available"]) for radar in radars)
        signature = (len(tracks), stable_health)
        if signature == self._last_summary_signature:
            return
        self._last_summary_signature = signature
        attributes = {
            "friendly_name": f"MMWave Fusion {self.fusion_id} target count",
            "fusion_id": self.fusion_id,
            "online_radars": sum(1 for _, available in stable_health if available),
            "radar_count": len(stable_health),
        }
        self.hass.states.async_set(
            ENTITY_TARGET_COUNT.format(fusion_id=slugify(self.fusion_id)),
            len(tracks),
            attributes,
        )
        self.hass.states.async_set(
            ENTITY_OCCUPIED.format(fusion_id=slugify(self.fusion_id)),
            STATE_ON if tracks else "off",
            {
                "friendly_name": f"MMWave Fusion {self.fusion_id} occupied",
                "fusion_id": self.fusion_id,
                "device_class": "occupancy",
            },
        )

    @callback
    def remove_summary_states(self) -> None:
        self.hass.states.async_remove(ENTITY_TARGET_COUNT.format(fusion_id=slugify(self.fusion_id)))
        self.hass.states.async_remove(ENTITY_OCCUPIED.format(fusion_id=slugify(self.fusion_id)))

    def _radar_health(self) -> list[dict[str, object]]:
        health: list[dict[str, object]] = []
        now = time.time()
        for radar_id, radar in self._radars.items():
            entity_id = radar.get("frame_entity") or radar.get("presence_entity") or radar["targets"][0]["x_entity"]
            state = self.hass.states.get(str(entity_id))
            age_s = now - state.last_updated.timestamp() if state is not None else None
            stale = bool(
                radar.get("frame_entity")
                and (age_s is None or age_s > float(radar["frame_stale_after_s"]))
            )
            health.append(
                {
                    "id": radar_id,
                    "available": (
                        state is not None
                        and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                        and not stale
                    ),
                    "last_updated": state.last_updated.timestamp() if state is not None else None,
                    "age_s": round(age_s, 3) if age_s is not None else None,
                    "stale": stale,
                }
            )
        return health

    def status(self) -> dict[str, object]:
        return {
            "fusion_id": self.fusion_id,
            "configured": True,
            "radar_count": len(self._radars),
            "zone_count": len(self.config["zones"]),
            "camera_count": len(self.config["cameras"]),
            "target_count": len(self._latest_tracks),
        }


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    fusion_id = str(config.get("fusion_id") or DEFAULT_FUSION_ID).strip()
    if not fusion_id:
        raise ValueError("fusion_id cannot be empty")
    radars_input = config.get("radars")
    if not isinstance(radars_input, list) or not radars_input:
        raise ValueError("At least one radar is required")

    radar_ids: set[str] = set()
    radars: list[dict[str, Any]] = []
    for index, raw_radar in enumerate(radars_input):
        if not isinstance(raw_radar, dict):
            raise ValueError(f"radars[{index}] must be an object")
        radar = dict(raw_radar)
        radar_id = str(radar.get("id") or f"radar_{index + 1}")
        if radar_id in radar_ids:
            raise ValueError(f"Duplicate radar id: {radar_id}")
        radar_ids.add(radar_id)
        model = str(radar.get("radar_model") or radar.get("model") or "")
        if model not in SPATIAL_MODELS:
            raise ValueError(f"Radar {radar_id} model {model!r} does not provide supported spatial coordinates")
        targets = normalize_targets(radar)
        frame_entity = str(radar.get("frame_entity") or "").strip() or None
        if not targets and not frame_entity:
            raise ValueError(f"Radar {radar_id} needs frame_entity or X/Y target entities")
        calibration = dict(radar.get("calibration") or {})
        calibration.setdefault("radar_x", 0.0)
        calibration.setdefault("radar_y", 0.0)
        calibration.setdefault("radar_z", 220.0)
        calibration.setdefault("yaw", 0.0)
        calibration.setdefault("pitch", 0.0)
        calibration.setdefault("roll", 0.0)
        radars.append(
            {
                **radar,
                "id": radar_id,
                "radar_model": model,
                "targets": targets,
                "frame_entity": frame_entity,
                "calibration": calibration,
                "coordinate_scale": float(radar.get("coordinate_scale", MODEL_COORDINATE_SCALE[model])),
                "_coordinate_scale_explicit": "coordinate_scale" in radar,
                "frame_coordinate_scale": float(radar.get("frame_coordinate_scale", 1.0)),
                "frame_stale_after_s": max(float(radar.get("frame_stale_after_s", 3.0)), 0.5),
                "measurement_weight": max(float(radar.get("measurement_weight", 1.0)), 0.01),
            }
        )

    zones: list[dict[str, Any]] = []
    zone_ids: set[str] = set()
    for index, raw_zone in enumerate(config.get("zones") or []):
        if not isinstance(raw_zone, dict):
            raise ValueError(f"zones[{index}] must be an object")
        zone_id = str(raw_zone.get("id") or f"zone_{index + 1}")
        polygon = raw_zone.get("polygon")
        if zone_id in zone_ids or not isinstance(polygon, list) or len(polygon) < 3:
            raise ValueError(f"Zone {zone_id} needs a unique id and at least three polygon points")
        if any(
            not isinstance(point, dict)
            or not isinstance(point.get("x"), (int, float))
            or not isinstance(point.get("y"), (int, float))
            for point in polygon
        ):
            raise ValueError(f"Zone {zone_id} polygon points must contain numeric x/y values")
        zone_ids.add(zone_id)
        zones.append({**raw_zone, "id": zone_id, "polygon": polygon, "dwell_s": float(raw_zone.get("dwell_s", 0.0))})

    cameras: list[dict[str, Any]] = []
    for index, raw_camera in enumerate(config.get("cameras") or []):
        if not isinstance(raw_camera, dict) or not raw_camera.get("entity_id"):
            raise ValueError(f"cameras[{index}] must define entity_id")
        camera_zones = list(raw_camera.get("zones") or [])
        unknown_zones = set(camera_zones) - zone_ids
        if unknown_zones:
            raise ValueError(
                f"Camera {raw_camera['entity_id']} references unknown zones: {sorted(unknown_zones)}"
            )
        recording_source = str(raw_camera.get("recording_source") or "ha_live").lower()
        if recording_source not in {"ha_live", "hikvision_sd", "hikvision_nvr"}:
            raise ValueError(
                f"Camera {raw_camera['entity_id']} recording_source must be ha_live, hikvision_sd, or hikvision_nvr"
            )
        track_id = int(raw_camera.get("track_id", 101))
        if track_id < 1 or track_id > 9999:
            raise ValueError(f"Camera {raw_camera['entity_id']} track_id is out of range")
        cameras.append(
            {
                **raw_camera,
                "zones": camera_zones,
                "event_types": list(raw_camera.get("event_types") or ["enter", "dwell"]),
                "lookback": max(int(raw_camera.get("lookback", 5)), 0),
                "duration": max(int(raw_camera.get("duration", 20)), 1),
                "cooldown_s": max(int(raw_camera.get("cooldown_s", 30)), 0),
                "recording_source": recording_source,
                "track_id": track_id,
                "http_port": int(raw_camera.get("http_port", 80)),
                "rtsp_port": int(raw_camera.get("rtsp_port", 554)),
                "archive_settle_s": max(int(raw_camera.get("archive_settle_s", 15)), 0),
                "archive_retry_interval_s": max(int(raw_camera.get("archive_retry_interval_s", 30)), 5),
                "archive_retries": min(max(int(raw_camera.get("archive_retries", 24)), 0), 120),
            }
        )

    raw_fusion = dict(config.get("fusion") or {})
    default_track_ttl = (
        3.0
        if any(radar["radar_model"] == "r60abd1" for radar in radars)
        else DEFAULT_TRACK_TTL_S
    )
    fusion = {
        "rate_hz": min(max(float(raw_fusion.get("rate_hz", DEFAULT_RATE_HZ)), 1.0), 30.0),
        "association_gate_cm": float(raw_fusion.get("association_gate_cm", DEFAULT_ASSOCIATION_GATE_CM)),
        "merge_gate_cm": float(raw_fusion.get("merge_gate_cm", DEFAULT_MERGE_GATE_CM)),
        "track_ttl_s": float(raw_fusion.get("track_ttl_s", default_track_ttl)),
        "confirm_hits": max(int(raw_fusion.get("confirm_hits", 2)), 1),
    }
    return {
        **config,
        "fusion_id": fusion_id,
        "room_w": max(float(config.get("room_w", 400.0)), 50.0),
        "room_d": max(float(config.get("room_d", 600.0)), 50.0),
        "radars": radars,
        "zones": zones,
        "cameras": cameras,
        "fusion": fusion,
    }


def normalize_targets(radar: dict[str, Any]) -> list[dict[str, str]]:
    if isinstance(radar.get("targets"), list):
        return [dict(target) for target in radar["targets"] if target.get("x_entity") and target.get("y_entity")]
    targets: list[dict[str, str]] = []
    for index in range(1, 4):
        x_entity = radar.get(f"target_{index}_x_entity")
        y_entity = radar.get(f"target_{index}_y_entity")
        if x_entity and y_entity:
            target = {"x_entity": str(x_entity), "y_entity": str(y_entity)}
            if radar.get(f"target_{index}_speed_entity"):
                target["speed_entity"] = str(radar[f"target_{index}_speed_entity"])
            targets.append(target)
    if not targets and radar.get("x_entity") and radar.get("y_entity"):
        target = {"x_entity": str(radar["x_entity"]), "y_entity": str(radar["y_entity"])}
        if radar.get("z_entity"):
            target["z_entity"] = str(radar["z_entity"])
        targets.append(target)
    return targets


def radar_entity_ids(radar: dict[str, Any]) -> set[str]:
    result = {str(radar["presence_entity"])} if radar.get("presence_entity") else set()
    if radar.get("frame_entity"):
        result.add(str(radar["frame_entity"]))
    for target in radar["targets"]:
        result.update(str(value) for key, value in target.items() if key.endswith("_entity") and value)
    return result


def entity_coordinate_scale(radar: dict[str, Any], state: Any) -> float:
    """Convert a numeric HA entity state to room centimetres."""

    if radar.get("_coordinate_scale_explicit"):
        return float(radar["coordinate_scale"])
    unit = str(state.attributes.get("unit_of_measurement") or "").strip().lower()
    if unit in {"cm", "cm/s"}:
        return 1.0
    if unit in {"mm", "mm/s"}:
        return 0.1
    if unit in {"m", "m/s"}:
        return 100.0
    return float(radar["coordinate_scale"])


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    return slug or "home"
