"""Small, dependency-free multi-radar tracking engine.

The tracker combines an alpha-beta state filter with global minimum-cost
assignment. This keeps Home Assistant free of NumPy/SciPy while avoiding the
identity swaps produced by greedy nearest-neighbour association.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import cos, hypot, pi, sin
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

    def as_dict(self) -> dict[str, object]:
        return {
            "track_id": self.track_id,
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "vx": round(self.vx, 2),
            "vy": round(self.vy, 2),
            "confidence": round(self.confidence, 3),
            "sources": list(self.sources),
            "started_at": self.started_at,
            "last_seen": self.last_seen,
        }


@dataclass(frozen=True, slots=True)
class StepResult:
    tracks: tuple[FusedTrack, ...]
    started: tuple[FusedTrack, ...]
    ended_track_ids: tuple[str, ...]


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
        )


class FusionEngine:
    """Associate room-space observations and maintain continuous target tracks."""

    def __init__(
        self,
        *,
        association_gate_cm: float = 90.0,
        merge_gate_cm: float = 70.0,
        track_ttl_s: float = 1.2,
        confirm_hits: int = 2,
    ) -> None:
        self.association_gate_cm = max(association_gate_cm, 10.0)
        self.merge_gate_cm = max(merge_gate_cm, 10.0)
        self.track_ttl_s = max(track_ttl_s, 0.2)
        self.confirm_hits = max(confirm_hits, 1)
        self._tracks: dict[str, _Track] = {}

    def reset(self) -> None:
        self._tracks.clear()

    def step(self, observations: list[Observation], now: float) -> StepResult:
        prediction_dt = {track_id: track.predict(now) for track_id, track in self._tracks.items()}
        clusters = self._cluster_observations(observations)
        assignments = self._associate(clusters, prediction_dt)
        assigned_tracks = {track_id for track_id, _ in assignments}
        assigned_clusters = {cluster_index for _, cluster_index in assignments}
        started: list[FusedTrack] = []

        for track_id, cluster_index in assignments:
            track = self._tracks[track_id]
            cluster = clusters[cluster_index]
            dt = max(prediction_dt.get(track_id, 0.1), 0.05)
            residual_x = cluster.x - track.x
            residual_y = cluster.y - track.y
            source_bonus = min(len(cluster.radar_ids) - 1, 3)
            alpha = 0.56 + 0.06 * source_bonus
            beta = 0.10 + 0.02 * source_bonus
            track.x += alpha * residual_x
            track.y += alpha * residual_y
            track.vx += beta * residual_x / dt
            track.vy += beta * residual_y / dt
            track.last_seen = cluster.timestamp
            track.sources = cluster.radar_ids
            track.hits += max(1, len(cluster.radar_ids))
            track.confidence = min(1.0, track.confidence + 0.1 + 0.08 * source_bonus)
            was_confirmed = track.confirmed
            track.confirmed = track.hits >= self.confirm_hits
            if track.confirmed and not was_confirmed:
                started.append(track.public())

        for track_id, track in self._tracks.items():
            if track_id not in assigned_tracks:
                track.confidence = max(0.0, track.confidence - 0.08)
                track.sources = set()

        for index, cluster in enumerate(clusters):
            if index in assigned_clusters:
                continue
            hits = max(1, len(cluster.radar_ids))
            track = _Track(
                track_id=uuid4().hex,
                x=cluster.x,
                y=cluster.y,
                vx=0.0,
                vy=0.0,
                confidence=min(0.9, 0.35 + 0.18 * len(cluster.radar_ids)),
                sources=cluster.radar_ids,
                started_at=cluster.timestamp,
                last_seen=cluster.timestamp,
                updated_at=now,
                hits=hits,
                confirmed=hits >= self.confirm_hits,
            )
            self._tracks[track.track_id] = track
            if track.confirmed:
                started.append(track.public())

        ended: list[str] = []
        for track_id, track in tuple(self._tracks.items()):
            if now - track.last_seen > self.track_ttl_s:
                if track.confirmed:
                    ended.append(track_id)
                del self._tracks[track_id]

        public_tracks = tuple(track.public() for track in self._tracks.values() if track.confirmed)
        return StepResult(public_tracks, tuple(started), tuple(ended))

    def _cluster_observations(self, observations: list[Observation]) -> list[_Cluster]:
        clusters: list[_Cluster] = []
        for observation in sorted(observations, key=lambda item: item.weight, reverse=True):
            best: _Cluster | None = None
            best_distance = self.merge_gate_cm
            for cluster in clusters:
                if observation.radar_id in cluster.radar_ids:
                    continue
                distance = hypot(observation.x - cluster.x, observation.y - cluster.y)
                if distance <= best_distance:
                    best = cluster
                    best_distance = distance
            if best is None:
                clusters.append(_Cluster([observation]))
            else:
                best.observations.append(observation)
        return clusters

    def _associate(self, clusters: list[_Cluster], prediction_dt: dict[str, float]) -> list[tuple[str, int]]:
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
            dynamic_gate = self.association_gate_cm + hypot(track.vx, track.vy) * prediction_dt.get(track_id, 0.0)
            for cluster_index, cluster in enumerate(clusters):
                distance = hypot(track.x - cluster.x, track.y - cluster.y)
                costs[track_index][cluster_index] = distance / dynamic_gate if distance <= dynamic_gate else invalid_cost
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
                reduced = matrix[current_row - 1][column - 1] - row_potential[current_row] - column_potential[column]
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

    assignment = [(column_match[column] - 1, column - 1) for column in range(1, column_count + 1) if column_match[column]]
    if transposed:
        return [(column, row) for row, column in assignment]
    return assignment


def transform_point(raw_x: float, raw_y: float, raw_z: float, calibration: dict[str, object]) -> tuple[float, float, float]:
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
