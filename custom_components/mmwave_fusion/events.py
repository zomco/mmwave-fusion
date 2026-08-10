"""Zone transition and dwell event detection."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from .fusion import FusedTrack, point_in_polygon


@dataclass(slots=True)
class _ZoneState:
    entered_at: float
    dwell_fired: bool = False


class ZoneEventEngine:
    def __init__(self, fusion_id: str, zones: list[dict[str, object]]) -> None:
        self.fusion_id = fusion_id
        self.zones = zones
        self._states: dict[tuple[str, str], _ZoneState] = {}

    def evaluate(self, tracks: tuple[FusedTrack, ...], now: float) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        active_tracks = {track.track_id for track in tracks}
        for track in tracks:
            for zone in self.zones:
                zone_id = str(zone["id"])
                key = (track.track_id, zone_id)
                inside = point_in_polygon(track.x, track.y, list(zone.get("polygon", [])))
                state = self._states.get(key)
                if inside and state is None:
                    self._states[key] = _ZoneState(now)
                    events.append(self._event(track, zone_id, "enter", now))
                    continue
                if not inside and state is not None:
                    del self._states[key]
                    events.append(self._event(track, zone_id, "exit", now))
                    continue
                dwell_s = float(zone.get("dwell_s", 0.0))
                if (
                    inside
                    and state is not None
                    and dwell_s > 0
                    and not state.dwell_fired
                    and now - state.entered_at >= dwell_s
                ):
                    state.dwell_fired = True
                    events.append(self._event(track, zone_id, "dwell", now, {"dwell_s": dwell_s}))

        for key in tuple(self._states):
            if key[0] not in active_tracks:
                del self._states[key]
        return events

    def occupancy(self) -> dict[str, int]:
        """How many tracks are currently inside each zone.

        Read straight off the state this engine already keeps to decide enter
        and exit, so it cannot disagree with the events it emits — recomputing
        point-in-polygon somewhere else would be a second implementation free to
        drift. Zones with nobody in them are reported as 0 rather than omitted,
        so an entity can tell "empty" from "this zone no longer exists".
        """

        counts = {str(zone["id"]): 0 for zone in self.zones}
        for _track_id, zone_id in self._states:
            if zone_id in counts:
                counts[zone_id] += 1
        return counts

    def _event(
        self,
        track: FusedTrack,
        zone_id: str,
        event_type: str,
        now: float,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "event_id": uuid4().hex,
            "fusion_id": self.fusion_id,
            "track_id": track.track_id,
            "event_type": event_type,
            "zone_id": zone_id,
            "timestamp": now,
            "x": round(track.x, 2),
            "y": round(track.y, 2),
            "metadata": metadata or {},
        }
