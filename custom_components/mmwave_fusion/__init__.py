"""MMWave Fusion Home Assistant integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = FusionCoordinator(hass)
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
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        async_unregister_services(hass)
        coordinator: FusionCoordinator = hass.data.pop(DOMAIN)
        await coordinator.async_shutdown()
    return unloaded
