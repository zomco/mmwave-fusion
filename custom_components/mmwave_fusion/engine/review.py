"""Parse AI Task clip-review replies. No Home Assistant imports."""

from __future__ import annotations

from typing import Any

from .query import REVIEW_VERDICTS, QueryError


def review_instructions(event: dict[str, Any]) -> str:
    zone = event.get("zone_id") or "unknown"
    event_type = event.get("event_type") or "unknown"
    score = event.get("quality_score")
    score_text = f"{score}" if score is not None else "unknown"
    return (
        "A mmWave radar scored a trajectory and recorded this camera still. "
        f"Zone id: {zone}. Event type: {event_type}. Quality score: {score_text}. "
        "Decide whether the still shows a person, a pet, nothing / a false positive "
        "(curtains, glare, empty room), or is too unclear to say. "
        "Do not name or identify anyone. One short sentence in summary."
    )


def parse_review(payload: object) -> dict[str, str]:
    data = _unwrap(payload)
    if not isinstance(data, dict):
        raise QueryError("Clip review returned no structured data")
    verdict = str(data.get("verdict") or "").strip()
    if verdict not in REVIEW_VERDICTS:
        raise QueryError(
            f"Clip review verdict {verdict!r} is not one of {sorted(REVIEW_VERDICTS)}"
        )
    summary = str(data.get("summary") or "").strip()
    if not summary:
        raise QueryError("Clip review returned an empty summary")
    return {"verdict": verdict, "summary": summary}


def _unwrap(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    if "verdict" in payload:
        return payload
    nested = payload.get("data")
    if isinstance(nested, dict):
        return _unwrap(nested)
    for value in payload.values():
        if isinstance(value, dict) and ("verdict" in value or "data" in value):
            return _unwrap(value)
    return payload
