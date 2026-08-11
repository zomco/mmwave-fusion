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
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import (
    DEFAULT_EVENT_RETENTION_DAYS,
    DEFAULT_POINT_RETENTION_DAYS,
    DOMAIN,
    MAX_EVENT_RETENTION_DAYS,
    MAX_POINT_RETENTION_DAYS,
    MIN_EVENT_RETENTION_DAYS,
    MIN_POINT_RETENTION_DAYS,
    OPTION_EVENT_RETENTION_DAYS,
    OPTION_POINT_RETENTION_DAYS,
)


class MMWaveFusionConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> MMWaveFusionOptionsFlow:
        return MMWaveFusionOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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


class MMWaveFusionOptionsFlow(OptionsFlow):
    """How long stored history is kept.

    This is the one setting that cannot come from the card. The card configures
    fusion systems, and there are many of them; retention belongs to the single
    SQLite database they all share, so it lives with the entry that owns it.

    It is worth exposing because the cost is real and depends entirely on the
    install: track_points is written at the fusion rate, which on a two-radar
    system at 10 Hz is roughly 50 MB per day. A week suits a house. A week of a
    warehouse is a different number, and so is a week of a holiday cottage that
    nobody walks through.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        # What was just typed wins over what was stored, so a rejected pair
        # comes back as the user left it. Falling through to the saved values
        # means correcting one number requires retyping both.
        options = {**self.config_entry.options, **(user_input or {})}

        if user_input is not None:
            points = user_input[OPTION_POINT_RETENTION_DAYS]
            events = user_input[OPTION_EVENT_RETENTION_DAYS]
            if events < points:
                # Not a matter of taste. prune() deletes tracks on the event
                # window and their points on the point window, so the shorter
                # event window would strand points whose track is gone. They
                # keep their disk space and disappear from the heatmap, which
                # joins tracks to scope points to a fusion system — the worst
                # of both, and invisible until someone wonders where the
                # history went.
                errors[OPTION_EVENT_RETENTION_DAYS] = "events_shorter_than_points"
            else:
                return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        OPTION_POINT_RETENTION_DAYS,
                        default=options.get(
                            OPTION_POINT_RETENTION_DAYS, DEFAULT_POINT_RETENTION_DAYS
                        ),
                    ): vol.All(
                        cv.positive_int,
                        vol.Range(min=MIN_POINT_RETENTION_DAYS, max=MAX_POINT_RETENTION_DAYS),
                    ),
                    vol.Required(
                        OPTION_EVENT_RETENTION_DAYS,
                        default=options.get(
                            OPTION_EVENT_RETENTION_DAYS, DEFAULT_EVENT_RETENTION_DAYS
                        ),
                    ): vol.All(
                        cv.positive_int,
                        vol.Range(min=MIN_EVENT_RETENTION_DAYS, max=MAX_EVENT_RETENTION_DAYS),
                    ),
                }
            ),
        )
