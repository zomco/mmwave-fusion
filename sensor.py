"""Target-count sensor for each fusion system."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
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
        async_add_entities([FusionTargetCountSensor(fusion_id)])

    for fusion_id in coordinator.systems:
        _add(fusion_id)

    async_dispatcher_connect(hass, SIGNAL_SYSTEM_ADDED, _add)


class FusionTargetCountSensor(FusionEntity, SensorEntity):
    """How many fused tracks the system currently holds."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:account-group"

    def __init__(self, fusion_id: str) -> None:
        super().__init__(
            fusion_id,
            "target_count",
            f"MMWave Fusion {fusion_id} target count",
        )

    @property
    def native_value(self) -> int | None:
        if self._payload is None:
            return None
        return len(self._tracks)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        radars = self._radars
        return {
            **super().extra_state_attributes,
            "online_radars": sum(1 for radar in radars if radar.get("available")),
            "radar_count": len(radars),
            # A track seen by two or more radars is the payoff of fusion, so
            # it is worth surfacing separately from the raw count.
            "multi_source_targets": sum(
                1 for track in self._tracks if len(track.get("sources", [])) >= 2
            ),
            "calibration_warnings": [
                str(radar["id"]) for radar in radars if radar.get("calibration_warning")
            ],
        }
