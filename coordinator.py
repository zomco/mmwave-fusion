"""Runtime coordinator for radar ingestion, fusion, persistence and recording."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
import logging
from math import ceil
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
from .fusion import (
    FusedTrack,
    FusionEngine,
    Observation,
    observations_in_room,
    transform_point,
)
from .quality import TrajectoryQualityEngine
from .profiles import normalize_calibration_profile
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
        self.calibration_profiles: dict[str, dict[str, Any]] = {}

    async def async_initialize(self) -> None:
        await self.hass.async_add_executor_job(self.trajectory_store.initialize)
        interrupted = await self.hass.async_add_executor_job(
            self.trajectory_store.fail_incomplete_clips,
            "Home Assistant restarted before extraction completed",
        )
        if interrupted:
            _LOGGER.warning("Marked %s interrupted recording request(s) as failed", interrupted)
        stored = await self.config_store.async_load() or {}
        profiles = stored.get("calibration_profiles", {})
        if isinstance(profiles, dict):
            self.calibration_profiles = {
                str(profile_id): profile
                for profile_id, profile in profiles.items()
                if isinstance(profile, dict)
            }
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
            await self._async_save()
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
        await self._async_save()
        return True

    async def async_upsert_calibration_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        """Create or update one device-level calibration profile."""

        profile_id = str(profile.get("profile_id") or "").strip()
        normalized = normalize_calibration_profile(
            profile,
            self.calibration_profiles.get(profile_id),
        )
        self.calibration_profiles[profile_id] = normalized
        await self._async_save()
        return normalized

    async def async_remove_calibration_profile(self, profile_id: str) -> bool:
        """Remove a reusable profile without changing existing config snapshots."""

        removed = self.calibration_profiles.pop(profile_id, None) is not None
        if removed:
            await self._async_save()
        return removed

    def list_calibration_profiles(self) -> list[dict[str, Any]]:
        """Return profiles newest first."""

        return sorted(
            self.calibration_profiles.values(),
            key=lambda profile: float(profile.get("updated_at", 0)),
            reverse=True,
        )

    async def _async_save(self) -> None:
        await self.config_store.async_save(
            {
                "systems": self.configs,
                "calibration_profiles": self.calibration_profiles,
            }
        )

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
            min_confirm_sources=int(settings["min_confirm_sources"]),
        )
        self.events = ZoneEventEngine(self.fusion_id, config["zones"])
        self.quality = TrajectoryQualityEngine(
            self.fusion_id,
            float(config["room_w"]),
            float(config["room_d"]),
            config["quality"],
        )
        self._pending: list[Observation] = []
        self._radar_by_entity: dict[str, str] = {}
        self._radars = {str(radar["id"]): radar for radar in config["radars"]}
        self._last_signatures: dict[str, tuple[str, ...]] = {}
        self._last_camera_recordings: dict[tuple[str, str, str], float] = {}
        self._flush_tasks: dict[str, asyncio.Task[None]] = {}
        self._point_buffer: list[tuple[str, float, FusedTrack]] = []
        self._last_point_samples: dict[str, float] = {}
        self._last_point_flush = time.time()
        self._latest_tracks: tuple[FusedTrack, ...] = ()
        self._remove_listener: Callable[[], None] | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._clip_tasks: set[asyncio.Task[None]] = set()
        self._camera_recording_locks: dict[str, asyncio.Lock] = {}
        self._camera_buffers: dict[str, tuple[Any, Any, bool, bool]] = {}
        self._camera_buffer_task: asyncio.Task[None] | None = None
        self._radar_stats: dict[str, dict[str, int]] = {
            radar_id: {"observations": 0, "in_room": 0} for radar_id in self._radars
        }
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
        self._camera_buffer_task = self.hass.async_create_background_task(
            self._async_ensure_camera_buffers(),
            f"mmwave_fusion_camera_buffer_{self.fusion_id}",
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
        if self._camera_buffer_task is not None:
            self._camera_buffer_task.cancel()
            await asyncio.gather(self._camera_buffer_task, return_exceptions=True)
            self._camera_buffer_task = None
        await self._async_stop_camera_buffers()
        if self._point_buffer:
            points, self._point_buffer = self._point_buffer, []
            await self.hass.async_add_executor_job(self.storage.append_points, points)

    async def _async_ensure_camera_buffers(self) -> None:
        """Wait for camera entities that may finish setup after this integration."""

        expected = {str(camera["entity_id"]) for camera in self.config["cameras"]}
        for attempt in range(12):
            await self._async_start_camera_buffers(log_errors=attempt == 11)
            if expected.issubset(self._camera_buffers):
                return
            await asyncio.sleep(5)

    async def _async_start_camera_buffers(self, *, log_errors: bool) -> None:
        """Keep one HA HLS stream active so camera.record has an in-memory lookback."""

        from homeassistant.components.camera.helper import get_camera_from_entity_id

        for camera_config in self.config["cameras"]:
            entity_id = str(camera_config["entity_id"])
            if entity_id in self._camera_buffers:
                continue
            stream = None
            provider = None
            previous_preload = False
            created_provider = False
            try:
                camera = get_camera_from_entity_id(self.hass, entity_id)
                stream = await camera.async_create_stream()
                if stream is None:
                    raise RuntimeError("camera does not expose an HLS-compatible stream")
                previous_preload = bool(stream.dynamic_stream_settings.preload_stream)
                had_hls_provider = "hls" in stream.outputs()
                stream.dynamic_stream_settings.preload_stream = True
                provider = stream.add_provider("hls")
                created_provider = not had_hls_provider
                await stream.start()
                self._camera_buffers[entity_id] = (
                    stream,
                    provider,
                    previous_preload,
                    created_provider,
                )
                _LOGGER.info("Started HA memory lookback buffer for %s", entity_id)
            except Exception as error:  # noqa: BLE001 - one camera must not stop radar fusion
                if stream is not None:
                    stream.dynamic_stream_settings.preload_stream = previous_preload
                    if provider is not None and created_provider:
                        await stream.remove_provider(provider)
                if log_errors:
                    _LOGGER.error(
                        "Unable to start HA memory lookback buffer for %s: %s",
                        entity_id,
                        redact_url_credentials(str(error)),
                    )
                else:
                    _LOGGER.debug("Waiting for camera entity %s", entity_id)

    async def _async_stop_camera_buffers(self) -> None:
        """Release providers created by this fusion system and restore preferences."""

        for entity_id, (stream, provider, previous_preload, created_provider) in tuple(
            self._camera_buffers.items()
        ):
            try:
                stream.dynamic_stream_settings.preload_stream = previous_preload
                if created_provider and not previous_preload:
                    await stream.remove_provider(provider)
            except Exception:  # noqa: BLE001 - shutdown must continue
                _LOGGER.exception("Unable to stop HA memory lookback buffer for %s", entity_id)
        self._camera_buffers.clear()

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
        room_w = float(self.config["room_w"])
        room_d = float(self.config["room_d"])
        for observation in observations:
            stats = self._radar_stats.setdefault(
                observation.radar_id,
                {"observations": 0, "in_room": 0},
            )
            stats["observations"] += 1
            if 0 <= observation.x <= room_w and 0 <= observation.y <= room_d:
                stats["in_room"] += 1

        result = self.engine.step(observations_in_room(observations, room_w, room_d), now)
        self._latest_tracks = result.tracks
        self.quality.observe(result.tracks, now)
        for track in result.started:
            await self.hass.async_add_executor_job(self.storage.start_track, self.fusion_id, track)

        persist_interval = float(self.config["quality"]["persist_interval_s"])
        for track in result.tracks:
            last_sample = self._last_point_samples.get(track.track_id)
            if track.sources and (last_sample is None or now - last_sample >= persist_interval):
                self._point_buffer.append((self.fusion_id, now, track))
                self._last_point_samples[track.track_id] = now
        if self._point_buffer and now - self._last_point_flush >= DEFAULT_POINT_FLUSH_S:
            points, self._point_buffer = self._point_buffer, []
            self._last_point_flush = now
            await self.hass.async_add_executor_job(self.storage.append_points, points)

        zone_events = self.events.evaluate(result.tracks, now)
        self.quality.add_zone_events(zone_events)
        trajectory_events: list[dict[str, object]] = []
        for track_id in result.ended_track_ids:
            finished = self.quality.finish(track_id, now)
            if finished is None:
                await self.hass.async_add_executor_job(self.storage.end_tracks, [track_id], now)
            else:
                trajectory_event, assessment = finished
                trajectory_events.append(trajectory_event)
                await self.hass.async_add_executor_job(
                    self.storage.finish_track,
                    track_id,
                    now,
                    {
                        "quality_score": assessment.score,
                        "quality_reason": assessment.reason,
                        "quality_breakdown": assessment.breakdown,
                        "quality_metrics": assessment.metrics,
                        "recording_decision": (
                            "eligible" if assessment.eligible else "rejected_quality"
                        ),
                    },
                )
            self._last_point_samples.pop(track_id, None)

        emitted_events = [*zone_events, *trajectory_events]
        for fusion_event in emitted_events:
            await self._record_event(fusion_event)
            await self.hass.async_add_executor_job(self.storage.insert_event, fusion_event)
            self.hass.bus.async_fire(EVENT_TYPE, fusion_event)

        payload = {
            "fusion_id": self.fusion_id,
            "timestamp": now,
            "tracks": [track.as_dict() for track in result.tracks],
            "events": emitted_events,
            "radars": self._radar_health(),
        }
        async_dispatcher_send(self.hass, f"{SIGNAL_UPDATE}_{self.fusion_id}", payload)
        self._publish_summary(payload)

    async def _record_event(self, event: dict[str, object]) -> None:
        metadata = event.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
            event["metadata"] = metadata
        decisions: list[dict[str, object]] = []
        metadata["recording_decisions"] = decisions
        event["recording_decisions"] = decisions

        for camera in self.config["cameras"]:
            entity_id = str(camera["entity_id"])
            decision: dict[str, object] = {
                "camera_entity_id": entity_id,
                "status": "not_applicable",
            }
            decisions.append(decision)
            if camera["zones"] and event["zone_id"] not in camera["zones"]:
                decision["status"] = "zone_filtered"
                continue
            if event["event_type"] not in camera["event_types"]:
                decision["status"] = "event_type_filtered"
                continue
            recording_key = (str(camera["entity_id"]), str(event["zone_id"]), str(event["event_type"]))
            event_timestamp = float(event["timestamp"])
            last_recording = self._last_camera_recordings.get(recording_key)
            if last_recording is not None and event_timestamp - last_recording < int(camera["cooldown_s"]):
                decision["status"] = "cooldown"
                decision["retry_after_s"] = round(
                    int(camera["cooldown_s"]) - (event_timestamp - last_recording),
                    1,
                )
                continue
            base_lookback = int(camera["lookback"])
            trajectory_start = float(metadata.get("start_ts", event_timestamp))
            requested_lookback = max(0, ceil(event_timestamp - trajectory_start) + base_lookback)
            lookback = min(requested_lookback, int(camera["buffer_seconds"]))
            duration = int(camera["duration"])
            safe_camera = re.sub(r"[^a-zA-Z0-9_-]+", "_", entity_id)
            date_path = datetime.fromtimestamp(float(event["timestamp"])).strftime("%Y-%m-%d")
            clip_id = uuid4().hex
            relative_path = f"mmwave_fusion/{self.fusion_id}/{date_path}/{event['event_id']}_{safe_camera}.mp4"
            filename = f"/media/{relative_path}"
            now = time.time()
            clip = {
                "clip_id": clip_id,
                "event_id": event["event_id"],
                "camera_entity_id": entity_id,
                "path": relative_path,
                "requested_at": now,
                "start_ts": float(event["timestamp"]) - lookback,
                "end_ts": float(event["timestamp"]) + duration,
                "status": "waiting",
                "provider": "ha_live",
                "updated_at": now,
                "completed_at": None,
                "file_size": None,
                "error": None,
            }
            try:
                await self.hass.async_add_executor_job(Path(filename).parent.mkdir, 0o755, True, True)
                await self.hass.async_add_executor_job(self.storage.insert_clip, clip)
                self._last_camera_recordings[recording_key] = event_timestamp
                decision.update(
                    {
                        "status": "scheduled",
                        "clip_id": clip_id,
                        "lookback_s": lookback,
                        "buffer_truncated": requested_lookback > lookback,
                    }
                )
                task = self.hass.async_create_background_task(
                    self._record_live_clip(camera, clip, Path(filename), lookback, duration),
                    f"mmwave_fusion_record_{clip_id}",
                )
                self._clip_tasks.add(task)
                task.add_done_callback(self._clip_tasks.discard)
            except Exception as error:  # noqa: BLE001 - one camera must not stop fusion
                clip["status"] = "failed"
                clip["updated_at"] = time.time()
                clip["error"] = redact_url_credentials(str(error))[-500:]
                decision["status"] = "failed"
                decision["error"] = clip["error"]
                await self.hass.async_add_executor_job(self.storage.insert_clip, clip)
                _LOGGER.exception("Unable to request recording from %s for event %s", entity_id, event["event_id"])

    async def _record_live_clip(
        self,
        camera: dict[str, Any],
        clip: dict[str, object],
        filename: Path,
        lookback: int,
        duration: int,
    ) -> None:
        """Record from HA's preloaded live stream and verify that a clip exists."""

        entity_id = str(camera["entity_id"])
        lock = self._camera_recording_locks.setdefault(entity_id, asyncio.Lock())
        try:
            async with lock:
                clip["status"] = "extracting"
                clip["updated_at"] = time.time()
                clip["error"] = None
                await self.hass.async_add_executor_job(self.storage.insert_clip, clip)
                await self.hass.services.async_call(
                    "camera",
                    "record",
                    {
                        "entity_id": entity_id,
                        "filename": str(filename),
                        "lookback": lookback,
                        "duration": duration,
                    },
                    blocking=True,
                )
                size = await self.hass.async_add_executor_job(
                    lambda: filename.stat().st_size if filename.is_file() else 0
                )
                if size <= 0:
                    raise RuntimeError("camera.record completed without creating a playable file")
                clip["status"] = "ready"
                clip["updated_at"] = time.time()
                clip["completed_at"] = clip["updated_at"]
                clip["file_size"] = size
                clip["error"] = None
                await self.hass.async_add_executor_job(self.storage.insert_clip, clip)
        except asyncio.CancelledError:
            clip["status"] = "failed"
            clip["updated_at"] = time.time()
            clip["error"] = "Recording cancelled before completion"
            await self.hass.async_add_executor_job(self.storage.insert_clip, clip)
            raise
        except Exception as error:  # noqa: BLE001 - recording failure must not stop fusion
            clip["status"] = "failed"
            clip["updated_at"] = time.time()
            clip["error"] = redact_url_credentials(str(error))[-500:]
            await self.hass.async_add_executor_job(self.storage.insert_clip, clip)
            _LOGGER.exception(
                "Unable to record HA memory-buffered video from %s for event %s: %s",
                entity_id,
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
        calibration_warnings = tuple(
            str(radar["id"]) for radar in radars if radar.get("calibration_warning")
        )
        multi_source_targets = sum(len(track.get("sources", [])) >= 2 for track in tracks)
        signature = (len(tracks), multi_source_targets, stable_health, calibration_warnings)
        if signature == self._last_summary_signature:
            return
        self._last_summary_signature = signature
        attributes = {
            "friendly_name": f"MMWave Fusion {self.fusion_id} target count",
            "fusion_id": self.fusion_id,
            "online_radars": sum(1 for _, available in stable_health if available),
            "radar_count": len(stable_health),
            "multi_source_targets": multi_source_targets,
            "calibration_warnings": list(calibration_warnings),
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
            stats = self._radar_stats.get(radar_id, {"observations": 0, "in_room": 0})
            observation_count = int(stats["observations"])
            in_room_count = int(stats["in_room"])
            in_room_ratio = in_room_count / observation_count if observation_count else None
            calibration_warning = bool(
                observation_count >= 100
                and in_room_ratio is not None
                and in_room_ratio < 0.2
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
                    "observations": observation_count,
                    "in_room_observations": in_room_count,
                    "in_room_ratio": round(in_room_ratio, 4) if in_room_ratio is not None else None,
                    "calibration_warning": calibration_warning,
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
            "multi_source_target_count": sum(
                len(track.sources) >= 2 for track in self._latest_tracks
            ),
            "calibration_warnings": [
                radar["id"]
                for radar in self._radar_health()
                if radar.get("calibration_warning")
            ],
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
    legacy_camera_keys = {
        "archive_host",
        "archive_retries",
        "archive_retry_interval_s",
        "archive_settle_s",
        "http_port",
        "rtsp_port",
        "track_id",
        "username",
        "password",
    }
    allowed_event_types = {"enter", "exit", "dwell", "trajectory", "traverse"}
    for index, raw_camera in enumerate(config.get("cameras") or []):
        if not isinstance(raw_camera, dict) or not raw_camera.get("entity_id"):
            raise ValueError(f"cameras[{index}] must define entity_id")
        camera_zones = list(raw_camera.get("zones") or [])
        unknown_zones = set(camera_zones) - zone_ids
        if unknown_zones:
            raise ValueError(
                f"Camera {raw_camera['entity_id']} references unknown zones: {sorted(unknown_zones)}"
            )
        requested_source = str(raw_camera.get("recording_source") or "ha_live").lower()
        if requested_source not in {"ha_live", "hikvision_sd", "hikvision_nvr"}:
            raise ValueError(
                f"Camera {raw_camera['entity_id']} recording_source must be ha_live"
            )
        legacy_source = requested_source in {"hikvision_sd", "hikvision_nvr"}
        if legacy_source:
            _LOGGER.warning(
                "Migrating %s from %s archive reads to the HA live memory buffer",
                raw_camera["entity_id"],
                requested_source,
            )
        raw_event_types = ["traverse"] if legacy_source else list(
            raw_camera.get("event_types") or ["traverse"]
        )
        unknown_event_types = set(raw_event_types) - allowed_event_types
        if unknown_event_types:
            raise ValueError(
                f"Camera {raw_camera['entity_id']} has unsupported event types: "
                f"{sorted(unknown_event_types)}"
            )
        camera_options = {
            key: value for key, value in raw_camera.items() if key not in legacy_camera_keys
        }
        cameras.append(
            {
                **camera_options,
                "zones": camera_zones,
                "event_types": raw_event_types,
                "lookback": max(int(raw_camera.get("lookback", 5)), 0),
                "duration": min(max(int(raw_camera.get("duration", 10)), 1), 60),
                "cooldown_s": max(int(raw_camera.get("cooldown_s", 60)), 0),
                "buffer_seconds": min(
                    max(int(raw_camera.get("buffer_seconds", 30)), 5),
                    30,
                ),
                "recording_source": "ha_live",
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
        "min_confirm_sources": min(
            max(
                int(raw_fusion.get("min_confirm_sources", 2 if len(radars) > 1 else 1)),
                1,
            ),
            len(radars),
        ),
    }
    raw_quality = dict(config.get("quality") or {})
    quality = {
        "min_score": min(max(int(raw_quality.get("min_score", 70)), 0), 100),
        "min_duration_s": max(float(raw_quality.get("min_duration_s", 3.0)), 0.0),
        "min_observed_points": max(int(raw_quality.get("min_observed_points", 20)), 2),
        "min_displacement_cm": max(
            float(raw_quality.get("min_displacement_cm", 120.0)),
            0.0,
        ),
        "min_observed_ratio": min(
            max(float(raw_quality.get("min_observed_ratio", 0.6)), 0.0),
            1.0,
        ),
        "min_inside_ratio": min(
            max(float(raw_quality.get("min_inside_ratio", 0.6)), 0.0),
            1.0,
        ),
        "max_gap_s": max(float(raw_quality.get("max_gap_s", 0.8)), 0.05),
        "max_jump_cm": max(float(raw_quality.get("max_jump_cm", 100.0)), 1.0),
        "require_enter_exit": bool(raw_quality.get("require_enter_exit", True)),
        "smoothing_s": min(
            max(float(raw_quality.get("smoothing_s", 0.5)), 0.05),
            5.0,
        ),
        "history_s": min(max(float(raw_quality.get("history_s", 60.0)), 10.0), 300.0),
        "persist_interval_s": min(
            max(float(raw_quality.get("persist_interval_s", 0.5)), 0.1),
            10.0,
        ),
        "boundary_margin_cm": min(
            max(float(raw_quality.get("boundary_margin_cm", 60.0)), 10.0),
            250.0,
        ),
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
        "quality": quality,
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


def redact_url_credentials(value: str) -> str:
    """Remove URL user-info before an exception is persisted or sent to the UI."""

    return re.sub(r"(?<=://)[^/@\s]+@", "***@", value)
