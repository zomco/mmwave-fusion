"""Occupancy binary sensor for each fusion system."""

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
        async_add_entities([FusionOccupiedBinarySensor(fusion_id)])

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
