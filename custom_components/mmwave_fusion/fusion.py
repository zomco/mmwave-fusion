"""Small, dependency-free multi-radar tracking engine.

The tracker combines an alpha-beta state filter with global minimum-cost
assignment. Same-radar slots that sit on one person are collapsed before
clustering; unmatched clusters next to an existing track do not mint a new
identity; confirmed tracks that stay inside the merge gate are fused into the
older track_id. This keeps Home Assistant free of NumPy/SciPy while avoiding
the identity swaps produced by greedy nearest-neighbour association.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import cos, hypot, isfinite, pi, sin
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class Observation:
    """One target observation expressed in the shared room coordinate system."""

    radar_id: str
    slot: int
    timestamp: float
    x: float
    y: float
    speed: float | None = None
    weight: float = 1.0
    frame_id: str | None = None
    source_timestamp: float | None = None
    range_cm: float | None = None


@dataclass(frozen=True, slots=True)
class FusedTrack:
    """Public immutable representation of a fused target track."""

    track_id: str
    x: float
    y: float
    vx: float
    vy: float
    confidence: float
    sources: tuple[str, ...]
    started_at: float
    last_seen: float
    source_count: int = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "track_id": self.track_id,
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "vx": round(self.vx, 2),
            "vy": round(self.vy, 2),
            "confidence": round(self.confidence, 3),
            "sources": list(self.sources),
            "source_count": self.source_count,
            "started_at": self.started_at,
            "last_seen": self.last_seen,
        }


@dataclass(frozen=True, slots=True)
class StepResult:
    tracks: tuple[FusedTrack, ...]
    started: tuple[FusedTrack, ...]
    ended_track_ids: tuple[str, ...]
    merged_track_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class _Cluster:
    observations: list[Observation] = field(default_factory=list)

    @property
    def radar_ids(self) -> set[str]:
        return {item.radar_id for item in self.observations}

    @property
    def x(self) -> float:
        total = sum(max(item.weight, 0.01) for item in self.observations)
        return sum(item.x * max(item.weight, 0.01) for item in self.observations) / total

    @property
    def y(self) -> float:
        total = sum(max(item.weight, 0.01) for item in self.observations)
        return sum(item.y * max(item.weight, 0.01) for item in self.observations) / total

    @property
    def timestamp(self) -> float:
        return max(item.timestamp for item in self.observations)


@dataclass(slots=True)
class _Track:
    track_id: str
    x: float
    y: float
    vx: float
    vy: float
    confidence: float
    sources: set[str]
    seen_sources: set[str]
    started_at: float
    last_seen: float
    updated_at: float
    hits: int
    confirmed: bool

    def predict(self, now: float) -> float:
        dt = min(max(now - self.updated_at, 0.0), 0.5)
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.updated_at = now
        return dt

    def public(self) -> FusedTrack:
        return FusedTrack(
            track_id=self.track_id,
            x=self.x,
            y=self.y,
            vx=self.vx,
            vy=self.vy,
            confidence=self.confidence,
            sources=tuple(sorted(self.sources)),
            started_at=self.started_at,
            last_seen=self.last_seen,
            source_count=len(self.seen_sources),
        )


class FusionEngine:
    """Associate room-space observations and maintain continuous target tracks."""

    def __init__(
        self,
        *,
        association_gate_cm: float = 90.0,
        merge_gate_cm: float = 70.0,
        track_ttl_s: float = 2.0,
        confirm_hits: int = 2,
        min_confirm_sources: int = 1,
        duplicate_gate_cm: float = 50.0,
        range_merge_factor: float = 0.08,
        merge_confirm_s: float = 0.6,
    ) -> None:
        self.association_gate_cm = max(association_gate_cm, 10.0)
        self.merge_gate_cm = max(merge_gate_cm, 10.0)
        self.track_ttl_s = max(track_ttl_s, 0.2)
        self.confirm_hits = max(confirm_hits, 1)
        self.min_confirm_sources = max(min_confirm_sources, 1)
        self.duplicate_gate_cm = max(duplicate_gate_cm, 0.0)
        self.range_merge_factor = max(range_merge_factor, 0.0)
        self.merge_confirm_s = max(merge_confirm_s, 0.0)
        self._tracks: dict[str, _Track] = {}
        self._merge_since: dict[frozenset[str], float] = {}

    def reset(self) -> None:
        self._tracks.clear()
        self._merge_since.clear()

    def step(self, observations: list[Observation], now: float) -> StepResult:
        # Keep only the latest observation for a physical radar slot, even if
        # a caller supplies a backlog. Expire tracks before allocating costs.
        latest: dict[tuple[str, int], Observation] = {}
        for observation in observations:
            if not all(isfinite(value) for value in (observation.x, observation.y, observation.timestamp, observation.weight)):
                continue
            if not 0 <= now - observation.timestamp <= self.track_ttl_s:
                continue
            key = (observation.radar_id, observation.slot)
            if key not in latest or observation.timestamp >= latest[key].timestamp:
                latest[key] = observation
        observations = self._dedupe_same_radar(list(latest.values()))
        ended = []
        for track_id, track in tuple(self._tracks.items()):
            if now - track.last_seen > self.track_ttl_s:
                if track.confirmed:
                    ended.append(track_id)
                del self._tracks[track_id]
        prediction_dt = {track_id: track.predict(now) for track_id, track in self._tracks.items()}
        clusters = self._cluster_observations(observations)
        assignments = self._associate(clusters, prediction_dt)
        assigned_tracks = {track_id for track_id, _ in assignments}
        assigned_clusters = {cluster_index for _, cluster_index in assignments}
        started: list[FusedTrack] = []

        for track_id, cluster_index in assignments:
            started.extend(
                self._update_track(
                    self._tracks[track_id],
                    clusters[cluster_index],
                    max(prediction_dt.get(track_id, 0.1), 0.05),
                )
            )

        for track_id, track in self._tracks.items():
            if track_id not in assigned_tracks:
                track.confidence = max(0.0, track.confidence - 0.08)
                track.sources = set()

        preexisting = set(self._tracks)
        for index, cluster in enumerate(clusters):
            if index in assigned_clusters:
                continue
            nearest_id, nearest_distance = self._nearest_track(
                cluster, prediction_dt, allowed=preexisting
            )
            if nearest_id is None:
                started.extend(self._birth_track(cluster, now))
                continue
            if nearest_id in assigned_tracks:
                if nearest_distance <= self.merge_gate_cm:
                    continue
                started.extend(self._birth_track(cluster, now))
                continue
            if nearest_distance <= self.association_gate_cm:
                assigned_tracks.add(nearest_id)
                started.extend(
                    self._update_track(
                        self._tracks[nearest_id],
                        cluster,
                        max(prediction_dt.get(nearest_id, 0.1), 0.05),
                    )
                )
                continue
            started.extend(self._birth_track(cluster, now))

        merged = self._merge_close_tracks(now)
        public_tracks = tuple(track.public() for track in self._tracks.values() if track.confirmed)
        return StepResult(public_tracks, tuple(started), tuple(ended), tuple(merged))

    def _dedupe_same_radar(self, observations: list[Observation]) -> list[Observation]:
        """Drop extra slots from one radar that sit on top of the same person."""

        if self.duplicate_gate_cm <= 0 or len(observations) < 2:
            return observations
        grouped: dict[str, list[Observation]] = {}
        for observation in observations:
            grouped.setdefault(observation.radar_id, []).append(observation)
        kept: list[Observation] = []
        for group in grouped.values():
            if len(group) == 1:
                kept.extend(group)
                continue

            def score(observation: Observation) -> float:
                if not self._tracks:
                    return observation.weight
                return -min(
                    hypot(observation.x - track.x, observation.y - track.y)
                    for track in self._tracks.values()
                )

            accepted: list[Observation] = []
            for observation in sorted(group, key=score, reverse=True):
                if any(
                    hypot(observation.x - other.x, observation.y - other.y) <= self.duplicate_gate_cm
                    for other in accepted
                ):
                    continue
                accepted.append(observation)
            kept.extend(accepted)
        return kept

    def _pair_merge_gate(self, observation: Observation, cluster: _Cluster) -> float:
        peak = observation.range_cm or 0.0
        for item in cluster.observations:
            if item.range_cm is not None:
                peak = max(peak, item.range_cm)
        return self.merge_gate_cm + self.range_merge_factor * peak

    def _update_track(self, track: _Track, cluster: _Cluster, dt: float) -> list[FusedTrack]:
        residual_x = cluster.x - track.x
        residual_y = cluster.y - track.y
        source_bonus = min(len(cluster.radar_ids) - 1, 3)
        alpha = 0.35 + 0.05 * source_bonus
        beta = 0.08 + 0.02 * source_bonus
        track.x += alpha * residual_x
        track.y += alpha * residual_y
        track.vx += beta * residual_x / dt
        track.vy += beta * residual_y / dt
        track.last_seen = cluster.timestamp
        track.sources = cluster.radar_ids
        track.seen_sources.update(cluster.radar_ids)
        track.hits += max(1, len(cluster.radar_ids))
        confidence_ceiling = (
            1.0 if len(track.seen_sources) >= self.min_confirm_sources else 0.74
        )
        track.confidence = min(
            confidence_ceiling,
            track.confidence + 0.1 + 0.08 * source_bonus,
        )
        was_confirmed = track.confirmed
        track.confirmed = (
            track.hits >= self.confirm_hits
            and len(track.seen_sources) >= self.min_confirm_sources
        )
        if track.confirmed and not was_confirmed:
            return [track.public()]
        return []

    def _birth_track(self, cluster: _Cluster, now: float) -> list[FusedTrack]:
        hits = max(1, len(cluster.radar_ids))
        track = _Track(
            track_id=uuid4().hex,
            x=cluster.x,
            y=cluster.y,
            vx=0.0,
            vy=0.0,
            confidence=min(0.9, 0.35 + 0.18 * len(cluster.radar_ids)),
            sources=cluster.radar_ids,
            seen_sources=set(cluster.radar_ids),
            started_at=cluster.timestamp,
            last_seen=cluster.timestamp,
            updated_at=now,
            hits=hits,
            confirmed=(
                hits >= self.confirm_hits and len(cluster.radar_ids) >= self.min_confirm_sources
            ),
        )
        self._tracks[track.track_id] = track
        if track.confirmed:
            return [track.public()]
        return []

    def _nearest_track(
        self,
        cluster: _Cluster,
        prediction_dt: dict[str, float],
        allowed: set[str] | None = None,
    ) -> tuple[str | None, float]:
        best_id: str | None = None
        best_distance = float("inf")
        for track_id, track in self._tracks.items():
            if allowed is not None and track_id not in allowed:
                continue
            distance = hypot(track.x - cluster.x, track.y - cluster.y)
            gate = self.association_gate_cm + hypot(track.vx, track.vy) * prediction_dt.get(
                track_id, 0.0
            )
            if distance <= gate and distance < best_distance:
                best_id = track_id
                best_distance = distance
        return best_id, best_distance

    def _merge_close_tracks(self, now: float) -> list[str]:
        if self.merge_confirm_s <= 0:
            self._merge_since.clear()
            return []
        confirmed = [track for track in self._tracks.values() if track.confirmed]
        close: set[frozenset[str]] = set()
        merged: list[str] = []
        consumed: set[str] = set()
        for index, left in enumerate(confirmed):
            if left.track_id in consumed:
                continue
            for right in confirmed[index + 1 :]:
                if right.track_id in consumed:
                    continue
                distance = hypot(left.x - right.x, left.y - right.y)
                if distance > self.merge_gate_cm:
                    continue
                pair = frozenset({left.track_id, right.track_id})
                close.add(pair)
                first_seen = self._merge_since.setdefault(pair, now)
                if now - first_seen < self.merge_confirm_s:
                    continue
                older, newer = (
                    (left, right) if left.started_at <= right.started_at else (right, left)
                )
                older.seen_sources.update(newer.seen_sources)
                older.sources.update(newer.sources)
                older.hits += newer.hits
                older.confidence = max(older.confidence, newer.confidence)
                del self._tracks[newer.track_id]
                merged.append(newer.track_id)
                consumed.add(newer.track_id)
                consumed.add(older.track_id)
                break
        self._merge_since = {
            pair: stamp
            for pair, stamp in self._merge_since.items()
            if pair in close and all(track_id in self._tracks for track_id in pair)
        }
        return merged

    def _cluster_observations(self, observations: list[Observation]) -> list[_Cluster]:
        clusters: list[_Cluster] = []
        for observation in sorted(observations, key=lambda item: item.weight, reverse=True):
            best: _Cluster | None = None
            best_distance = float("inf")
            for cluster in clusters:
                if observation.radar_id in cluster.radar_ids:
                    continue
                distance = hypot(observation.x - cluster.x, observation.y - cluster.y)
                if distance <= self._pair_merge_gate(observation, cluster) and distance < best_distance:
                    best = cluster
                    best_distance = distance
            if best is None:
                clusters.append(_Cluster([observation]))
            else:
                best.observations.append(observation)
        return clusters

    def _associate(
        self, clusters: list[_Cluster], prediction_dt: dict[str, float]
    ) -> list[tuple[str, int]]:
        track_ids = list(self._tracks)
        if not track_ids or not clusters:
            return []
        track_count = len(track_ids)
        cluster_count = len(clusters)
        size = track_count + cluster_count
        unmatched_cost = 1.05
        invalid_cost = 4.0
        costs = [[0.0] * size for _ in range(size)]
        for track_index, track_id in enumerate(track_ids):
            track = self._tracks[track_id]
            dynamic_gate = self.association_gate_cm + hypot(track.vx, track.vy) * prediction_dt.get(
                track_id, 0.0
            )
            for cluster_index, cluster in enumerate(clusters):
                distance = hypot(track.x - cluster.x, track.y - cluster.y)
                costs[track_index][cluster_index] = (
                    distance / dynamic_gate if distance <= dynamic_gate else invalid_cost
                )
            for dummy_track_column in range(cluster_count, size):
                costs[track_index][dummy_track_column] = unmatched_cost
        for dummy_cluster_row in range(track_count, size):
            for cluster_index in range(cluster_count):
                costs[dummy_cluster_row][cluster_index] = unmatched_cost

        return [
            (track_ids[track_index], cluster_index)
            for track_index, cluster_index in _minimum_cost_assignment(costs)
            if track_index < track_count
            and cluster_index < cluster_count
            and costs[track_index][cluster_index] <= 1.0
        ]


def observations_in_room(
    observations: list[Observation], room_w: float, room_d: float
) -> list[Observation]:
    """Discard out-of-floor-plan detections before they create ghost tracks."""

    return [
        observation
        for observation in observations
        if 0 <= observation.x <= room_w and 0 <= observation.y <= room_d
    ]


def observations_inside(
    observations: list[Observation],
    room_w: float,
    room_d: float,
    polygons: dict[str, list[dict[str, object]]] | None = None,
) -> list[Observation]:
    """Keep in-room observations, then apply each radar's installation polygon."""

    kept = observations_in_room(observations, room_w, room_d)
    if not polygons:
        return kept
    result: list[Observation] = []
    for observation in kept:
        polygon = polygons.get(observation.radar_id) or []
        if len(polygon) < 3 or point_in_polygon(observation.x, observation.y, polygon):
            result.append(observation)
    return result


def _minimum_cost_assignment(costs: list[list[float]]) -> list[tuple[int, int]]:
    """Return a globally minimal row/column assignment using Hungarian O(n^3)."""

    if not costs or not costs[0]:
        return []
    row_count = len(costs)
    column_count = len(costs[0])
    if any(len(row) != column_count for row in costs):
        raise ValueError("Assignment cost matrix must be rectangular")
    transposed = row_count > column_count
    matrix = [list(row) for row in costs]
    if transposed:
        matrix = [list(row) for row in zip(*matrix, strict=True)]
        row_count, column_count = column_count, row_count

    row_potential = [0.0] * (row_count + 1)
    column_potential = [0.0] * (column_count + 1)
    column_match = [0] * (column_count + 1)
    previous_column = [0] * (column_count + 1)
    for row in range(1, row_count + 1):
        column_match[0] = row
        current_column = 0
        minimum = [float("inf")] * (column_count + 1)
        used = [False] * (column_count + 1)
        while True:
            used[current_column] = True
            current_row = column_match[current_column]
            delta = float("inf")
            next_column = 0
            for column in range(1, column_count + 1):
                if used[column]:
                    continue
                reduced = (
                    matrix[current_row - 1][column - 1]
                    - row_potential[current_row]
                    - column_potential[column]
                )
                if reduced < minimum[column]:
                    minimum[column] = reduced
                    previous_column[column] = current_column
                if minimum[column] < delta:
                    delta = minimum[column]
                    next_column = column
            for column in range(column_count + 1):
                if used[column]:
                    row_potential[column_match[column]] += delta
                    column_potential[column] -= delta
                else:
                    minimum[column] -= delta
            current_column = next_column
            if column_match[current_column] == 0:
                break
        while True:
            next_column = previous_column[current_column]
            column_match[current_column] = column_match[next_column]
            current_column = next_column
            if current_column == 0:
                break

    assignment = [
        (column_match[column] - 1, column - 1)
        for column in range(1, column_count + 1)
        if column_match[column]
    ]
    if transposed:
        return [(column, row) for row, column in assignment]
    return assignment


def transform_point(
    raw_x: float, raw_y: float, raw_z: float, calibration: dict[str, object]
) -> tuple[float, float, float]:
    """Apply the same yaw/pitch/roll + translation convention as mmwave-card."""

    yaw = float(calibration.get("yaw", 0.0)) * pi / 180.0
    pitch = float(calibration.get("pitch", 0.0)) * pi / 180.0
    roll = float(calibration.get("roll", 0.0)) * pi / 180.0
    sy, cy = sin(yaw), cos(yaw)
    sp, cp = sin(pitch), cos(pitch)
    sr, cr = sin(roll), cos(roll)
    matrix = (
        (cy * cr + sy * sp * sr, sy * cp, -cy * sr + sy * sp * cr),
        (-sy * cr + cy * sp * sr, cy * cp, sy * sr + cy * sp * cr),
        (cp * sr, -sp, cp * cr),
    )
    world_x = matrix[0][0] * raw_x + matrix[0][1] * raw_y + matrix[0][2] * raw_z
    world_y = matrix[1][0] * raw_x + matrix[1][1] * raw_y + matrix[1][2] * raw_z
    world_z = matrix[2][0] * raw_x + matrix[2][1] * raw_y + matrix[2][2] * raw_z
    return (
        float(calibration.get("radar_x", 0.0)) + world_x,
        float(calibration.get("radar_y", 0.0)) + world_y,
        float(calibration.get("radar_z", 0.0)) - world_z,
    )


def point_in_polygon(x: float, y: float, polygon: list[dict[str, object]]) -> bool:
    if len(polygon) < 3:
        return False
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = float(current["x"]), float(current["y"])
        x2, y2 = float(previous["x"]), float(previous["y"])
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
        previous = current
    return inside
