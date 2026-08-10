"""Target-count sensors: one per fusion system, plus one per zone."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
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
        entities: list[SensorEntity] = [FusionTargetCountSensor(fusion_id)]
        entities += [
            FusionZoneCountSensor(fusion_id, str(zone["id"]), str(zone.get("name") or zone["id"]))
            for zone in coordinator.configs.get(fusion_id, {}).get("zones", [])
        ]
        async_add_entities(entities)

    for fusion_id in coordinator.systems:
        _add(fusion_id)

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_SYSTEM_ADDED, _add))


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


class FusionZoneCountSensor(FusionEntity, SensorEntity):
    """How many fused tracks are inside one zone.

    A zone is where fusion stops being a picture and starts being useful: it is
    what an automation asks about. Until these existed the only counts on offer
    were for the whole room, so anyone wanting "is someone at the desk" had to
    listen for mmwave_fusion_event on the bus and keep their own tally.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:account-group"

    def __init__(self, fusion_id: str, zone_id: str, zone_name: str) -> None:
        super().__init__(
            fusion_id,
            f"zone_{zone_id}_count",
            f"MMWave Fusion {fusion_id} {zone_name} count",
        )
        self._zone_id = zone_id
        self._zone_name = zone_name

    @property
    def native_value(self) -> int | None:
        if self._payload is None:
            return None
        return self._zone_occupancy.get(self._zone_id, 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **super().extra_state_attributes,
            "zone_id": self._zone_id,
            "zone_name": self._zone_name,
        }
