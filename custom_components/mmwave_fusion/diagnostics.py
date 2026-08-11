"""Diagnostics download for a fusion config entry.

Almost every question about this integration is "why is it not seeing anyone",
and answering it needs the same handful of facts: how each radar is calibrated,
whether its frames are arriving, what fraction of its targets land inside the
room, and how big the history has grown. Collecting that by hand means walking
someone through half a dozen entity pages. This is the button that does it.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import FusionCoordinator, redact_url_credentials


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: FusionCoordinator = hass.data[DOMAIN]

    systems: dict[str, Any] = {}
    for fusion_id, system in coordinator.systems.items():
        config = coordinator.configs.get(fusion_id, {})
        systems[fusion_id] = {
            "status": system.status(),
            "radars": system.radar_health(),
            "zones": [
                {
                    "id": zone.get("id"),
                    "name": zone.get("name"),
                    "polygon_points": len(zone.get("polygon") or []),
                    "dwell_s": zone.get("dwell_s"),
                }
                for zone in config.get("zones", [])
            ],
            "room": {"w": config.get("room_w"), "d": config.get("room_d")},
            "fusion": config.get("fusion"),
            "quality": config.get("quality"),
            # Calibration is the first thing to look at when targets are landing
            # in the wrong place, and it is not sensitive.
            "calibration": {
                str(radar.get("id")): radar.get("calibration") for radar in config.get("radars", [])
            },
            # Cameras carry stream URLs, which carry credentials often enough
            # that redacting is the only safe default for a file people paste
            # into issue trackers.
            "cameras": [
                {
                    "entity_id": camera.get("entity_id"),
                    "buffer_seconds": camera.get("buffer_seconds"),
                    "duration": camera.get("duration"),
                    "source": redact_url_credentials(str(camera.get("source", ""))),
                }
                for camera in config.get("cameras", [])
            ],
        }

    return {
        "systems": systems,
        "calibration_profiles": len(coordinator.calibration_profiles),
        "storage": {
            "path": str(coordinator.trajectory_store.path),
            "size_bytes": await hass.async_add_executor_job(
                coordinator.trajectory_store.size_bytes
            ),
        },
    }
