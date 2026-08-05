"""MMWave Fusion Home Assistant integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import FusionCoordinator
from .websocket_api import async_register_websocket_api

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN): vol.Schema({}, extra=vol.ALLOW_EXTRA),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: cv.ConfigType) -> bool:
    coordinator = FusionCoordinator(hass)
    await coordinator.async_initialize()
    async_register_websocket_api(hass, coordinator)

    async def async_shutdown(_: object) -> None:
        await coordinator.async_shutdown()

    hass.bus.async_listen_once("homeassistant_stop", async_shutdown)
    return True
