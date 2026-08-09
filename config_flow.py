"""Config flow for MMWave Fusion.

The entry is a single hub rather than one entry per fusion system. Fusion
systems are created, reconfigured and removed at runtime by the Lovelace card
over the WebSocket API, so asking the user to declare them here would just be a
second place to keep in sync. The entry exists so the integration has a config
entry at all, which is what lets its entities be grouped under a device and
what makes it installable as a HACS integration.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class MMWaveFusionConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # One hub is enough; everything else is configured from the card.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

        return self.async_create_entry(title="MMWave Fusion", data={})

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Adopt an existing `mmwave_fusion:` block in configuration.yaml.

        Runs without a form so upgrading does not silently drop a working
        setup; the fusion systems themselves live in .storage and are picked up
        by the coordinator either way.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="MMWave Fusion", data={})
