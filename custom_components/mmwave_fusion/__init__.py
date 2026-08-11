"""MMWave Fusion Home Assistant integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import (
    DEFAULT_EVENT_RETENTION_DAYS,
    DEFAULT_POINT_RETENTION_DAYS,
    DOMAIN,
    OPTION_EVENT_RETENTION_DAYS,
    OPTION_POINT_RETENTION_DAYS,
)
from .coordinator import FusionCoordinator
from .services import async_register_services, async_unregister_services
from .websocket_api import async_register_websocket_api

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN): vol.Schema({}, extra=vol.ALLOW_EXTRA),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Adopt a legacy `mmwave_fusion:` block into a config entry.

    The integration used to be YAML-only. Existing installs keep working: the
    block triggers a one-shot import flow, and the flow's unique id makes that
    a no-op once the entry exists.
    """
    if DOMAIN in config:
        hass.async_create_task(
            hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT}, data={})
        )
    return True


def _apply_options(coordinator: FusionCoordinator, entry: ConfigEntry) -> None:
    coordinator.set_retention(
        float(entry.options.get(OPTION_POINT_RETENTION_DAYS, DEFAULT_POINT_RETENTION_DAYS)),
        float(entry.options.get(OPTION_EVENT_RETENTION_DAYS, DEFAULT_EVENT_RETENTION_DAYS)),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = FusionCoordinator(hass)
    # Before initialize, so the first sweep already uses the configured windows
    # rather than pruning to the defaults once and correcting itself later.
    _apply_options(coordinator, entry)
    await coordinator.async_initialize()
    hass.data.setdefault(DOMAIN, coordinator)
    async_register_websocket_api(hass, coordinator)
    async_register_services(hass, coordinator)

    # Forwarded after async_initialize so the platforms see every system
    # restored from storage; systems created later over the WebSocket API
    # arrive through SIGNAL_SYSTEM_ADDED.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Config entries are not reliably unloaded when Home Assistant stops, and
    # the trajectory store holds an open SQLite connection, so close it on the
    # stop event as well as on unload.
    async def _async_stop(_: object) -> None:
        await coordinator.async_shutdown()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop))

    # Applied in place rather than by reloading the entry: a reload would tear
    # down every fusion system and restart the tracking loops, dropping tracks
    # in flight, to change a number the prune loop reads on its next pass.
    async def _async_options_updated(_: HomeAssistant, updated: ConfigEntry) -> None:
        _apply_options(coordinator, updated)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        async_unregister_services(hass)
        coordinator: FusionCoordinator = hass.data.pop(DOMAIN)
        await coordinator.async_shutdown()
    return unloaded
