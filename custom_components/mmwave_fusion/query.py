"""Compact, LLM-sized views of fusion state. No Home Assistant imports."""

from __future__ import annotations

from datetime import datetime
from datetime import tzinfo as TzInfo
from typing import Any

EVENT_TYPES = frozenset({"enter", "exit", "dwell", "traverse", "trajectory"})
DEFAULT_EVENT_LIMIT = 20
MAX_EVENT_LIMIT = 50
DEFAULT_HOURS = 24.0
MAX_HOURS = 168.0
HEATMAP_TOP_N = 8
REVIEW_VERDICTS = frozenset({"person", "pet", "false_positive", "uncertain"})


class QueryError(ValueError):
    """Malformed tool arguments or an unknown fusion/zone id."""


def resolve_fusion_id(fusion_ids: list[str], requested: str | None) -> str:
    ids = [str(item) for item in fusion_ids]
    if requested:
        if requested not in ids:
            raise QueryError(f"Unknown fusion_id {requested!r}. Known: {ids or 'none'}")
        return requested
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise QueryError("No fusion system is configured.")
    raise QueryError(f"Multiple fusion systems; pass fusion_id. Known: {ids}")


def zone_names(zones: list[dict[str, Any]]) -> dict[str, str]:
    return {str(zone["id"]): str(zone.get("name") or zone["id"]) for zone in zones}


def resolve_zone(zones: list[dict[str, Any]], query: str | None) -> str | None:
    if query is None or not str(query).strip():
        return None
    needle = str(query).strip().casefold()
    exact: list[str] = []
    partial: list[str] = []
    for zone in zones:
        zone_id = str(zone["id"])
        name = str(zone.get("name") or zone_id)
        folded_id = zone_id.casefold()
        folded_name = name.casefold()
        if needle in {folded_id, folded_name}:
            exact.append(zone_id)
        elif needle in folded_id or needle in folded_name:
            partial.append(zone_id)
    matches = exact or partial
    if len(matches) == 1:
        return matches[0]
    if not matches:
        known = [f"{zone_id} ({name})" for zone_id, name in zone_names(zones).items()]
        raise QueryError(f"No zone matching {query!r}. Known: {known or 'none'}")
    raise QueryError(f"Ambiguous zone {query!r}: {matches}")


def parse_time(value: str | float | int | None, tzinfo: TzInfo) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise QueryError(f"Cannot parse time {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tzinfo)
    return parsed.timestamp()


def format_local_time(timestamp: float, tzinfo: TzInfo) -> str:
    return datetime.fromtimestamp(timestamp, tz=tzinfo).replace(microsecond=0).isoformat(sep=" ")


def window_from_args(
    now: float,
    *,
    hours: float | None = None,
    since: str | float | None = None,
    until: str | float | None = None,
    tzinfo: TzInfo,
) -> tuple[float, float]:
    if hours is not None and since is not None:
        raise QueryError("Pass hours or since, not both.")
    until_ts = parse_time(until, tzinfo)
    if until_ts is None:
        until_ts = now
    since_ts = parse_time(since, tzinfo)
    if since_ts is None:
        lookback = DEFAULT_HOURS if hours is None else float(hours)
        if lookback < 0.1 or lookback > MAX_HOURS:
            raise QueryError(f"hours must be between 0.1 and {MAX_HOURS:g}")
        since_ts = until_ts - lookback * 3600.0
    if until_ts <= since_ts:
        raise QueryError("until must be after since")
    if until_ts - since_ts > MAX_HOURS * 3600.0 + 1.0:
        raise QueryError(f"Window cannot exceed {MAX_HOURS:g} hours")
    return since_ts, until_ts


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_EVENT_LIMIT
    return min(max(int(limit), 1), MAX_EVENT_LIMIT)


def normalize_event_type(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if value not in EVENT_TYPES:
        raise QueryError(f"Unknown event_type {value!r}. Use one of: {sorted(EVENT_TYPES)}")
    return value


def compact_event(
    event: dict[str, Any], names: dict[str, str], tzinfo: TzInfo
) -> dict[str, Any]:
    zone_id = str(event.get("zone_id") or "")
    timestamp = event.get("ts", event.get("timestamp"))
    clip_status = event.get("clip_status")
    review = None
    verdict = event.get("review_verdict")
    if verdict:
        review = {"verdict": verdict, "summary": event.get("review_summary")}
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    return {
        "time": format_local_time(float(timestamp), tzinfo),
        "event": event.get("event_type"),
        "zone": names.get(zone_id, zone_id),
        "zone_id": zone_id,
        "track_id": event.get("track_id"),
        "x_cm": round(float(event.get("x") or 0)),
        "y_cm": round(float(event.get("y") or 0)),
        "quality_score": event.get("quality_score") or metadata.get("quality_score"),
        "quality_reason": event.get("quality_reason") or metadata.get("quality_reason"),
        "clip": str(clip_status) if clip_status else "none",
        "review": review,
    }


def summarize_heatmap(grid: dict[str, Any], top_n: int = HEATMAP_TOP_N) -> dict[str, Any]:
    cells = sorted(grid.get("cells") or [], key=lambda cell: int(cell["visits"]), reverse=True)
    hottest = cells[: max(int(top_n), 0)]
    return {
        "fusion_id": grid.get("fusion_id"),
        "bin_cm": grid.get("bin_cm"),
        "total_points": int(grid.get("total_points") or 0),
        "hottest": [
            {
                "x_cm": int(cell["x"]),
                "y_cm": int(cell["y"]),
                "visits": int(cell["visits"]),
            }
            for cell in hottest
        ],
    }


def tally_event_counts(
    rows: list[dict[str, Any]], names: dict[str, str]
) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_zone: dict[str, int] = {}
    for row in rows:
        event_type = str(row["event_type"])
        zone_id = str(row["zone_id"])
        count = int(row["count"])
        by_type[event_type] = by_type.get(event_type, 0) + count
        label = names.get(zone_id, zone_id)
        by_zone[label] = by_zone.get(label, 0) + count
    return {"by_type": by_type, "by_zone": by_zone}


def live_occupancy(
    *,
    fusion_id: str,
    ready: bool,
    target_count: int,
    zone_occupancy: dict[str, int],
    zones: list[dict[str, Any]],
    radar_health: list[dict[str, Any]],
) -> dict[str, Any]:
    names = zone_names(zones)
    zone_rows = []
    for zone_id, name in names.items():
        occupied = zone_occupancy.get(zone_id, 0)
        zone_rows.append(
            {
                "id": zone_id,
                "name": name,
                "occupied": None if not ready else int(occupied),
            }
        )
    return {
        "fusion_id": fusion_id,
        "ready": ready,
        "note": None
        if ready
        else "No radar frames yet. That is not the same as an empty room.",
        "target_count": None if not ready else int(target_count),
        "zones": zone_rows,
        "radars_online": sum(1 for radar in radar_health if radar.get("available")),
        "radar_count": len(radar_health),
    }


def diagnose_radar(health: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    hints: list[str] = []
    observations = int(health.get("observations") or 0)
    ratio = health.get("in_room_ratio")
    if not health.get("available"):
        issues.append("not_reporting")
        hints.append(
            "The radar entity is unavailable or its frames are stale. "
            "Check power, Wi-Fi and the UART wiring first."
        )
    elif health.get("stale"):
        issues.append("stale_frames")
        hints.append(
            "The ESP is reachable but the radar module has stopped sending frames. "
            "A full power cycle usually fixes this; restarting the ESP does not."
        )
    if health.get("calibration_warning"):
        issues.append("outside_room")
        ratio_text = f"{float(ratio):.0%}" if isinstance(ratio, (int, float)) else "unknown"
        hints.append(
            f"Fewer than 20% of detections land inside the room after {observations} "
            f"observations (in-room ratio {ratio_text}). This is almost always yaw off "
            "by 90° or 180°, or the origin corner is wrong. Mirrored motion → add 180° "
            "to yaw. Motion at right angles → add 90°. Do not chase pitch/roll on a 2D "
            "radar; those stay as measured. Recalibrate from the mmWave card, do not "
            "edit numbers by hand."
        )
    elif observations and observations < 100:
        hints.append(
            f"Only {observations} observations so far; the 100-sample in-room check "
            "has not run yet. Walk a loop inside the room outline."
        )
    elif observations >= 100:
        hints.append(
            "Detections are landing inside the room. Confirm in the live view that "
            "the point moves with you, not mirrored or rotated."
        )
    return {
        "id": health.get("id"),
        "available": bool(health.get("available")),
        "stale": bool(health.get("stale")),
        "observations": observations,
        "in_room_ratio": ratio,
        "issues": issues,
        "hints": hints,
    }
