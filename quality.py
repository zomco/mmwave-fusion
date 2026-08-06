"""Trajectory quality scoring for recording decisions."""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot
from typing import Any
from uuid import uuid4

from .fusion import FusedTrack


@dataclass(frozen=True, slots=True)
class TrajectorySample:
    """One fused-track sample, including whether a radar observed it."""

    timestamp: float
    x: float
    y: float
    vx: float
    vy: float
    confidence: float
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrajectoryAssessment:
    """Final trajectory score and recording decision."""

    score: int
    eligible: bool
    reason: str
    breakdown: dict[str, int]
    metrics: dict[str, float | int]


class TrajectoryQualityEngine:
    """Keep short in-memory histories and score them when tracks end."""

    def __init__(
        self,
        fusion_id: str,
        room_w: float,
        room_d: float,
        settings: dict[str, Any],
    ) -> None:
        self.fusion_id = fusion_id
        self.room_w = room_w
        self.room_d = room_d
        self.settings = settings
        self._samples: dict[str, list[TrajectorySample]] = {}
        self._events: dict[str, list[dict[str, object]]] = {}

    def observe(self, tracks: tuple[FusedTrack, ...], now: float) -> None:
        """Append current track states, retaining only a bounded time window."""

        cutoff = now - float(self.settings["history_s"])
        for track in tracks:
            history = self._samples.setdefault(track.track_id, [])
            history.append(
                TrajectorySample(
                    timestamp=now,
                    x=track.x,
                    y=track.y,
                    vx=track.vx,
                    vy=track.vy,
                    confidence=track.confidence,
                    sources=track.sources,
                )
            )
            if history[0].timestamp < cutoff:
                self._samples[track.track_id] = [sample for sample in history if sample.timestamp >= cutoff]

    def add_zone_events(self, events: list[dict[str, object]]) -> None:
        """Associate raw zone transitions with their track histories."""

        for event in events:
            self._events.setdefault(str(event["track_id"]), []).append(event)

    def finish(self, track_id: str, now: float) -> tuple[dict[str, object], TrajectoryAssessment] | None:
        """Finalize one track and return a trajectory event plus assessment."""

        samples = self._samples.pop(track_id, [])
        events = self._events.pop(track_id, [])
        if not samples:
            return None
        assessment = assess_trajectory(samples, events, self.room_w, self.room_d, self.settings)
        observed = [sample for sample in samples if sample.sources]
        endpoint = observed[-1] if observed else samples[-1]
        zone_id = next((str(event["zone_id"]) for event in events if event["event_type"] == "enter"), "room")
        start_ts = float(assessment.metrics.get("start_ts", samples[0].timestamp))
        end_ts = float(assessment.metrics.get("end_ts", now))
        event_type = "traverse" if assessment.eligible else "trajectory"
        event = {
            "event_id": uuid4().hex,
            "fusion_id": self.fusion_id,
            "track_id": track_id,
            "event_type": event_type,
            "zone_id": zone_id,
            "timestamp": end_ts,
            "x": round(endpoint.x, 2),
            "y": round(endpoint.y, 2),
            "quality_score": assessment.score,
            "quality_reason": assessment.reason,
            "recording_decision": "eligible" if assessment.eligible else "rejected_quality",
            "metadata": {
                "quality_score": assessment.score,
                "quality_breakdown": assessment.breakdown,
                "quality_metrics": assessment.metrics,
                "quality_reason": assessment.reason,
                "recording_decision": "eligible" if assessment.eligible else "rejected_quality",
                "start_ts": start_ts,
                "end_ts": end_ts,
            },
        }
        return event, assessment


def assess_trajectory(
    samples: list[TrajectorySample],
    events: list[dict[str, object]],
    room_w: float,
    room_d: float,
    settings: dict[str, Any],
) -> TrajectoryAssessment:
    """Score a trajectory using topology, continuity and physical plausibility."""

    observed = [sample for sample in samples if sample.sources]
    if len(observed) < 2:
        return _rejected("insufficient_observations", samples, observed)

    smoothed = _bucket_samples(observed, float(settings["smoothing_s"]))
    first, last = observed[0], observed[-1]
    duration = max(last.timestamp - first.timestamp, 0.0)
    displacement = hypot(last.x - first.x, last.y - first.y)
    raw_steps = [
        hypot(right.x - left.x, right.y - left.y)
        for left, right in zip(observed, observed[1:])
    ]
    steps = [hypot(right.x - left.x, right.y - left.y) for left, right in zip(smoothed, smoothed[1:])]
    path_length = sum(steps)
    path_efficiency = displacement / path_length if path_length > 0 else 0.0
    gaps = [right.timestamp - left.timestamp for left, right in zip(observed, observed[1:])]
    max_gap = max(gaps, default=0.0)
    max_jump = max(raw_steps, default=0.0)
    observed_ratio = len(observed) / max(len(samples), 1)
    inside_ratio = sum(0 <= sample.x <= room_w and 0 <= sample.y <= room_d for sample in observed) / len(observed)
    dual_ratio = sum(len(sample.sources) >= 2 for sample in observed) / len(observed)
    source_count = len({source for sample in observed for source in sample.sources})
    enter_count = sum(event["event_type"] == "enter" for event in events)
    exit_count = sum(event["event_type"] == "exit" for event in events)
    transition_count = enter_count + exit_count

    topology = 30 if enter_count and exit_count else 12 if transition_count else 0
    continuity = round(
        15 * _clamp(observed_ratio / 0.8)
        + 10 * _clamp(1.0 - max_gap / max(float(settings["max_gap_s"]), 0.01))
    )
    geometry = round(
        10 * _clamp(displacement / max(float(settings["min_displacement_cm"]) * 1.5, 1.0))
        + 10 * _clamp(path_efficiency / 0.7)
    )
    kinematics = round(10 * _clamp(1.0 - max_jump / max(float(settings["max_jump_cm"]), 1.0)))
    sensor_agreement = round(5 * _clamp((source_count - 1)) + 5 * _clamp(dual_ratio / 0.25))
    support = round(5 * _clamp(duration / 10.0))
    bounce_penalty = min(max(transition_count - 2, 0) * 3, 15)
    outside_penalty = round(10 * _clamp((float(settings["min_inside_ratio"]) - inside_ratio) / 0.5))
    breakdown = {
        "topology": topology,
        "continuity": continuity,
        "geometry": geometry,
        "kinematics": kinematics,
        "sensor_agreement": sensor_agreement,
        "support": support,
        "bounce_penalty": -bounce_penalty,
        "outside_penalty": -outside_penalty,
    }
    score = max(0, min(100, sum(breakdown.values())))
    metrics: dict[str, float | int] = {
        "start_ts": round(first.timestamp, 3),
        "end_ts": round(last.timestamp, 3),
        "duration_s": round(duration, 3),
        "observed_points": len(observed),
        "observed_ratio": round(observed_ratio, 3),
        "inside_ratio": round(inside_ratio, 3),
        "displacement_cm": round(displacement, 2),
        "path_length_cm": round(path_length, 2),
        "path_efficiency": round(path_efficiency, 3),
        "max_gap_s": round(max_gap, 3),
        "max_jump_cm": round(max_jump, 2),
        "source_count": source_count,
        "dual_source_ratio": round(dual_ratio, 3),
        "enter_count": enter_count,
        "exit_count": exit_count,
    }

    hard_failures: list[str] = []
    if duration < float(settings["min_duration_s"]):
        hard_failures.append("too_short")
    if len(observed) < int(settings["min_observed_points"]):
        hard_failures.append("too_few_observations")
    if displacement < float(settings["min_displacement_cm"]):
        hard_failures.append("insufficient_displacement")
    if observed_ratio < float(settings["min_observed_ratio"]):
        hard_failures.append("discontinuous_observations")
    if inside_ratio < float(settings["min_inside_ratio"]):
        hard_failures.append("mostly_outside_room")
    if max_gap > float(settings["max_gap_s"]):
        hard_failures.append("observation_gap")
    if max_jump > float(settings["max_jump_cm"]):
        hard_failures.append("trajectory_jump")
    if bool(settings["require_enter_exit"]) and not (enter_count and exit_count):
        hard_failures.append("incomplete_crossing")
    elif bool(settings["require_enter_exit"]) and transition_count != 2:
        hard_failures.append("unstable_boundary_crossing")

    eligible = not hard_failures and score >= int(settings["min_score"])
    reason = hard_failures[0] if hard_failures else "eligible" if eligible else "below_score_threshold"
    return TrajectoryAssessment(score, eligible, reason, breakdown, metrics)


def _bucket_samples(samples: list[TrajectorySample], interval_s: float) -> list[TrajectorySample]:
    """Average samples in fixed time buckets to avoid counting 10 Hz jitter as travel."""

    interval_s = max(interval_s, 0.05)
    buckets: list[list[TrajectorySample]] = []
    current_key: int | None = None
    for sample in samples:
        key = int(sample.timestamp / interval_s)
        if key != current_key:
            buckets.append([])
            current_key = key
        buckets[-1].append(sample)
    result: list[TrajectorySample] = []
    for bucket in buckets:
        count = len(bucket)
        result.append(
            TrajectorySample(
                timestamp=sum(sample.timestamp for sample in bucket) / count,
                x=sum(sample.x for sample in bucket) / count,
                y=sum(sample.y for sample in bucket) / count,
                vx=sum(sample.vx for sample in bucket) / count,
                vy=sum(sample.vy for sample in bucket) / count,
                confidence=sum(sample.confidence for sample in bucket) / count,
                sources=tuple(sorted({source for sample in bucket for source in sample.sources})),
            )
        )
    return result


def _rejected(
    reason: str,
    samples: list[TrajectorySample],
    observed: list[TrajectorySample],
) -> TrajectoryAssessment:
    metrics: dict[str, float | int] = {
        "observed_points": len(observed),
        "observed_ratio": round(len(observed) / max(len(samples), 1), 3),
    }
    return TrajectoryAssessment(0, False, reason, {}, metrics)


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)
