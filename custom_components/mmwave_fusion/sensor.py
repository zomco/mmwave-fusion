"""Target-count sensors: one per fusion system, plus one per zone."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_SYSTEM_ADDED
from .entity import FusionEntity

# Only the database-size sensor polls; everything else is pushed. Five
# minutes is far more often than a file that moves at a few MB a day needs.
SCAN_INTERVAL = timedelta(minutes=5)


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
        entities: list[SensorEntity] = [
            FusionTargetCountSensor(fusion_id),
            FusionDatabaseSizeSensor(fusion_id, coordinator),
        ]
        entities += [
            FusionZoneCountSensor(fusion_id, str(zone["id"]), str(zone.get("name") or zone["id"]))
            for zone in coordinator.configs.get(fusion_id, {}).get("zones", [])
        ]
        # update_before_add so the polled database-size sensor has a value
        # immediately; without it the entity reads unknown for a full scan
        # interval after every restart. The pushed entities ignore it.
        async_add_entities(entities, update_before_add=True)

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


class FusionDatabaseSizeSensor(FusionEntity, SensorEntity):
    """How much disk the trajectory store is using.

    Worth an entity because the number gets away from you quietly: track_points
    is written at the fusion rate, and the development instance reached 328 MB
    before anyone looked. Retention stops the growth, but SQLite only reuses
    freed pages — the file itself shrinks when the vacuum_database action runs,
    and this is how you see whether that was worth doing.

    Reported per fusion system for placement on that system's device, though the
    store is shared, so every one of them reads the same number.
    """

    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.MEBIBYTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:database"

    def __init__(self, fusion_id: str, coordinator: Any) -> None:
        super().__init__(
            fusion_id,
            "database_size",
            f"MMWave Fusion {fusion_id} database size",
        )
        self._coordinator = coordinator
        self._size_mib: float | None = None

    # Polled, not pushed, and that is deliberate: reading the size means
    # stat()ing the file, which is I/O and must not happen on the event loop.
    # Doing it in native_value earned a "took 0.414 seconds" warning from Home
    # Assistant the first time this ran.
    _attr_should_poll = True

    @property
    def available(self) -> bool:
        # Unlike the others this does not need a frame to have arrived — the
        # file has a size from the moment the store is initialised, and its
        # size is most interesting precisely when nothing is being tracked.
        return not self._removed

    @property
    def native_value(self) -> float | None:
        return self._size_mib

    async def async_update(self) -> None:
        def _size() -> float | None:
            try:
                return self._coordinator.trajectory_store.size_bytes() / 1048576
            except OSError:
                # The store can be mid-vacuum, which briefly replaces the file.
                return None

        self._size_mib = await self.hass.async_add_executor_job(_size)
