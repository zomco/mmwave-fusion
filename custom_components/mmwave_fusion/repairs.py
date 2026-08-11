"""Surface radar problems in Home Assistant's Repairs page.

The health of every radar is already computed on each tick — whether its entity
is available, whether its frames have gone stale, whether almost nothing it
reports lands inside the room. None of it was shown anywhere. A radar that
stopped talking stayed silent in the UI too: the fused count simply got smaller,
and you found out when an automation did not fire, or when a test went red.

These conditions are worth a repair issue rather than a log line because they
are all actionable by a person — reseat a connector, power-cycle a device,
recalibrate — and because Repairs is where someone looks when the house feels
wrong.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

# A radar has to look broken for this long before it is worth telling anyone.
# Frames go stale for a second whenever WiFi hiccups, and an issue that appears
# and clears on its own teaches people to ignore the page.
PERSIST_S = 120.0


class RadarIssueReporter:
    """Raise and clear repair issues from the health snapshot the loop builds."""

    def __init__(self, hass: HomeAssistant, fusion_id: str) -> None:
        self._hass = hass
        self._fusion_id = fusion_id
        # (radar_id, kind) -> when the condition was first seen continuously
        self._since: dict[tuple[str, str], float] = {}
        self._raised: set[tuple[str, str]] = set()

    def sync(self, health: list[dict[str, object]], now: float) -> None:
        seen: set[tuple[str, str]] = set()

        for radar in health:
            radar_id = str(radar["id"])
            for kind, failing in (
                ("unavailable", not radar.get("available")),
                # Only worth reporting separately while the entity is there:
                # an unavailable radar is stale by definition, and two issues
                # for one cable is noise.
                ("stale", bool(radar.get("available") and radar.get("stale"))),
                ("calibration", bool(radar.get("calibration_warning"))),
            ):
                key = (radar_id, kind)
                if not failing:
                    self._since.pop(key, None)
                    self._clear(key)
                    continue
                seen.add(key)
                first = self._since.setdefault(key, now)
                if now - first >= PERSIST_S:
                    self._raise(key, radar_id, kind)

        # A radar dropped from the config should not leave its issue behind.
        for key in tuple(self._raised):
            if key not in seen:
                self._since.pop(key, None)
                self._clear(key)

    def clear_all(self) -> None:
        for key in tuple(self._raised):
            self._clear(key)
        self._since.clear()

    def _issue_id(self, key: tuple[str, str]) -> str:
        radar_id, kind = key
        return f"radar_{kind}_{self._fusion_id}_{radar_id}"

    def _raise(self, key: tuple[str, str], radar_id: str, kind: str) -> None:
        if key in self._raised:
            return
        self._raised.add(key)
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            self._issue_id(key),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=f"radar_{kind}",
            translation_placeholders={
                "radar_id": radar_id,
                "fusion_id": self._fusion_id,
            },
        )

    def _clear(self, key: tuple[str, str]) -> None:
        if key not in self._raised:
            return
        self._raised.discard(key)
        ir.async_delete_issue(self._hass, DOMAIN, self._issue_id(key))
