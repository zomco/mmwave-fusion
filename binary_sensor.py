"""Occupancy binary sensor for each fusion system."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import DOMAIN, SIGNAL_SYSTEM_ADDED
from .entity import FusionEntity


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    if discovery_info is None:
        return

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

    async_dispatcher_connect(hass, SIGNAL_SYSTEM_ADDED, _add)


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
