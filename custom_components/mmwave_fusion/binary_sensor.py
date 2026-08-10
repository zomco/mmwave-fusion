"""Occupancy binary sensors: one per fusion system, plus one per zone."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_SYSTEM_ADDED
from .entity import FusionEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN]
    known: set[str] = set()

    @callback
    def _add(fusion_id: str) -> None:
        if fusion_id in known:
            return
        known.add(fusion_id)
        entities: list[BinarySensorEntity] = [FusionOccupiedBinarySensor(fusion_id)]
        entities += [
            FusionZoneOccupiedBinarySensor(
                fusion_id, str(zone["id"]), str(zone.get("name") or zone["id"])
            )
            for zone in coordinator.configs.get(fusion_id, {}).get("zones", [])
        ]
        async_add_entities(entities)

    for fusion_id in coordinator.systems:
        _add(fusion_id)

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_SYSTEM_ADDED, _add))


class FusionOccupiedBinarySensor(FusionEntity, BinarySensorEntity):
    """Whether the fused room currently holds anyone."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, fusion_id: str) -> None:
        super().__init__(
            fusion_id,
            "occupied",
            f"MMWave Fusion {fusion_id} occupied",
        )

    @property
    def is_on(self) -> bool | None:
        if self._payload is None:
            return None
        return bool(self._tracks)


class FusionZoneOccupiedBinarySensor(FusionEntity, BinarySensorEntity):
    """Whether anyone is currently inside one zone.

    This is the entity an automation actually wants — "turn the desk lamp on
    when someone is at the desk" — and until now the integration exposed only
    room-level occupancy, so that automation had to be written against raw
    mmwave_fusion_event bus events and keep its own state.
    """

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, fusion_id: str, zone_id: str, zone_name: str) -> None:
        super().__init__(
            fusion_id,
            f"zone_{zone_id}_occupied",
            f"MMWave Fusion {fusion_id} {zone_name} occupied",
        )
        self._zone_id = zone_id
        self._zone_name = zone_name

    @property
    def is_on(self) -> bool | None:
        if self._payload is None:
            return None
        return self._zone_occupancy.get(self._zone_id, 0) > 0

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            **super().extra_state_attributes,
            "zone_id": self._zone_id,
            "zone_name": self._zone_name,
        }
