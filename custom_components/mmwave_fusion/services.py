"""Home Assistant actions for maintenance that previously had no handle.

Everything this integration could do was reachable only from the card, over the
WebSocket API. That leaves the operational jobs — reclaim the disk the history
is sitting on, sweep it now rather than in six hours, drop stale tracks after
moving the furniture — with no way to script, schedule, or put on a dashboard
button.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import FusionCoordinator

SERVICE_VACUUM_DATABASE = "vacuum_database"
SERVICE_PRUNE_HISTORY = "prune_history"
SERVICE_RESET_TRACKS = "reset_tracks"

RESET_TRACKS_SCHEMA = vol.Schema({vol.Optional("fusion_id"): cv.string})


def async_register_services(hass: HomeAssistant, coordinator: FusionCoordinator) -> None:
    async def _vacuum(_: ServiceCall) -> dict[str, int]:
        return await coordinator.async_vacuum()

    async def _prune(_: ServiceCall) -> dict[str, int]:
        return await coordinator.async_prune_now()

    async def _reset_tracks(call: ServiceCall) -> dict[str, list[str]]:
        return {"reset": coordinator.reset_tracks(call.data.get("fusion_id"))}

    # All three answer with what they did — bytes reclaimed, rows removed,
    # systems reset — so a script can log or branch on the result instead of
    # firing blind.
    hass.services.async_register(
        DOMAIN, SERVICE_VACUUM_DATABASE, _vacuum, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PRUNE_HISTORY, _prune, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_TRACKS,
        _reset_tracks,
        schema=RESET_TRACKS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def async_unregister_services(hass: HomeAssistant) -> None:
    for service in (SERVICE_VACUUM_DATABASE, SERVICE_PRUNE_HISTORY, SERVICE_RESET_TRACKS):
        hass.services.async_remove(DOMAIN, service)
