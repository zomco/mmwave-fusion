"""Home Assistant implementation of the engine video sink."""

from __future__ import annotations

from homeassistant.core import HomeAssistant


class HAVideoSink:
    """camera.snapshot / camera.record against the live HA stream."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def snapshot(self, entity_id: str, filename: str) -> None:
        await self.hass.services.async_call(
            "camera",
            "snapshot",
            {"entity_id": entity_id, "filename": filename},
            blocking=True,
        )

    async def record(self, entity_id: str, filename: str, lookback: int, duration: int) -> None:
        await self.hass.services.async_call(
            "camera",
            "record",
            {
                "entity_id": entity_id,
                "filename": filename,
                "lookback": lookback,
                "duration": duration,
            },
            blocking=True,
        )
