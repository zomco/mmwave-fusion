"""Shared base for the entities a fusion system exposes."""

from __future__ import annotations

from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, MANUFACTURER, MODEL, SIGNAL_SYSTEM_REMOVED, SIGNAL_UPDATE


class FusionEntity(Entity):
    """One entity belonging to a single fusion system.

    State arrives over the SIGNAL_UPDATE dispatcher rather than from polling,
    so the entity never drives I/O itself.
    """

    _attr_should_poll = False

    def __init__(self, fusion_id: str, key: str, name: str) -> None:
        self._fusion_id = fusion_id
        self._key = key
        # Not using _attr_has_entity_name: the object id is derived from this
        # name, and it has to keep matching the ids these entities had before
        # they were registry entities, or every automation referencing them
        # breaks on upgrade.
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{fusion_id}_{key}"
        # One device per fusion system, so a room's entities group together and
        # can be assigned to an area in one move. This only takes effect
        # because the integration now owns a config entry - the device registry
        # refuses entries that are not tied to one.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, fusion_id)},
            name=f"MMWave Fusion {fusion_id}",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )
        self._payload: dict[str, Any] | None = None
        self._removed = False

    @property
    def available(self) -> bool:
        # Unavailable until the first frame, rather than reporting a
        # confident zero for a system that has not produced anything yet.
        return not self._removed and self._payload is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"fusion_id": self._fusion_id}

    @property
    def _zone_occupancy(self) -> dict[str, int]:
        """Per-zone head count from the last push, empty before the first one."""

        if self._payload is None:
            return {}
        occupancy = self._payload.get("zone_occupancy")
        return occupancy if isinstance(occupancy, dict) else {}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_UPDATE}_{self._fusion_id}",
                self._handle_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_SYSTEM_REMOVED,
                self._handle_system_removed,
            )
        )

    @callback
    def _handle_update(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.async_write_ha_state()

    @callback
    def _handle_system_removed(self, fusion_id: str) -> None:
        if fusion_id != self._fusion_id:
            return
        self._removed = True
        self.async_write_ha_state()

    # ── Helpers shared by the concrete entities ─────────────────────────────

    @property
    def _tracks(self) -> list[dict[str, Any]]:
        payload = self._payload or {}
        tracks = payload.get("tracks")
        return tracks if isinstance(tracks, list) else []

    @property
    def _radars(self) -> list[dict[str, Any]]:
        payload = self._payload or {}
        radars = payload.get("radars")
        return radars if isinstance(radars, list) else []
