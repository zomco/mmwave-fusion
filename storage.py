"""Dedicated trajectory/event storage for MMWave Fusion."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from threading import Lock
import time
from typing import Iterable

from .fusion import FusedTrack


class TrajectoryStore:
    """Thread-safe SQLite store kept separate from Home Assistant Recorder."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = Lock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tracks (
                    track_id TEXT PRIMARY KEY,
                    fusion_id TEXT NOT NULL,
                    start_ts REAL NOT NULL,
                    end_ts REAL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_tracks_fusion_time
                    ON tracks(fusion_id, start_ts DESC);

                CREATE TABLE IF NOT EXISTS track_points (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_id TEXT NOT NULL,
                    ts REAL NOT NULL,
                    x REAL NOT NULL,
                    y REAL NOT NULL,
                    vx REAL NOT NULL,
                    vy REAL NOT NULL,
                    confidence REAL NOT NULL,
                    sources TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_track_points_track_time
                    ON track_points(track_id, ts);

                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    fusion_id TEXT NOT NULL,
                    track_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    zone_id TEXT NOT NULL,
                    ts REAL NOT NULL,
                    x REAL NOT NULL,
                    y REAL NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_events_fusion_time
                    ON events(fusion_id, ts DESC);

                CREATE TABLE IF NOT EXISTS clips (
                    clip_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    camera_entity_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    requested_at REAL NOT NULL,
                    start_ts REAL NOT NULL,
                    end_ts REAL NOT NULL,
                    status TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'ha_live',
                    updated_at REAL,
                    completed_at REAL,
                    file_size INTEGER,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_clips_event ON clips(event_id);
                """
            )
            self._ensure_column(self._connection, "clips", "provider", "TEXT NOT NULL DEFAULT 'ha_live'")
            self._ensure_column(self._connection, "clips", "updated_at", "REAL")
            self._ensure_column(self._connection, "clips", "completed_at", "REAL")
            self._ensure_column(self._connection, "clips", "file_size", "INTEGER")
            self._ensure_column(self._connection, "clips", "error", "TEXT")
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def start_track(self, fusion_id: str, track: FusedTrack) -> None:
        with self._lock, self._require_connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO tracks(track_id, fusion_id, start_ts, metadata) VALUES (?, ?, ?, ?)",
                (track.track_id, fusion_id, track.started_at, json.dumps({"sources": track.sources})),
            )

    def end_tracks(self, track_ids: Iterable[str], end_ts: float) -> None:
        rows = [(end_ts, track_id) for track_id in track_ids]
        if not rows:
            return
        with self._lock, self._require_connection() as connection:
            connection.executemany("UPDATE tracks SET end_ts = ? WHERE track_id = ?", rows)

    def append_points(self, points: Iterable[tuple[str, float, FusedTrack]]) -> None:
        rows = [
            (
                track.track_id,
                timestamp,
                track.x,
                track.y,
                track.vx,
                track.vy,
                track.confidence,
                json.dumps(track.sources),
            )
            for _, timestamp, track in points
        ]
        if not rows:
            return
        with self._lock, self._require_connection() as connection:
            connection.executemany(
                """
                INSERT INTO track_points(track_id, ts, x, y, vx, vy, confidence, sources)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def insert_event(self, event: dict[str, object]) -> None:
        with self._lock, self._require_connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO events(
                    event_id, fusion_id, track_id, event_type, zone_id, ts, x, y, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event["fusion_id"],
                    event["track_id"],
                    event["event_type"],
                    event["zone_id"],
                    event["timestamp"],
                    event["x"],
                    event["y"],
                    json.dumps(event.get("metadata", {})),
                ),
            )

    def insert_clip(self, clip: dict[str, object]) -> None:
        with self._lock, self._require_connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO clips(
                    clip_id, event_id, camera_entity_id, path, requested_at, start_ts, end_ts, status,
                    provider, updated_at, completed_at, file_size, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clip["clip_id"],
                    clip["event_id"],
                    clip["camera_entity_id"],
                    clip["path"],
                    clip["requested_at"],
                    clip["start_ts"],
                    clip["end_ts"],
                    clip["status"],
                    clip.get("provider", "ha_live"),
                    clip.get("updated_at"),
                    clip.get("completed_at"),
                    clip.get("file_size"),
                    clip.get("error"),
                ),
            )

    def fail_incomplete_clips(self, reason: str) -> int:
        """Make interrupted requests explicit instead of leaving them pending forever."""

        with self._lock, self._require_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE clips SET status = 'failed', updated_at = ?, error = ?
                WHERE status IN ('requested', 'waiting', 'extracting')
                """,
                (time.time(), reason),
            )
            return cursor.rowcount

    def query_events(self, fusion_id: str, limit: int = 100, before: float | None = None) -> list[dict[str, object]]:
        sql = """
            SELECT e.*, c.clip_id, c.camera_entity_id,
                   CASE WHEN c.status = 'ready' THEN c.path END AS clip_path,
                   c.start_ts AS clip_start_ts, c.end_ts AS clip_end_ts, c.status AS clip_status,
                   c.provider AS clip_provider, c.file_size AS clip_file_size, c.error AS clip_error
            FROM events e
            LEFT JOIN clips c ON c.event_id = e.event_id
            WHERE e.fusion_id = ?
        """
        parameters: list[object] = [fusion_id]
        if before is not None:
            sql += " AND e.ts < ?"
            parameters.append(before)
        sql += " ORDER BY e.ts DESC LIMIT ?"
        parameters.append(min(max(limit, 1), 500))
        with self._lock:
            rows = self._require_connection().execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]

    def query_track(self, track_id: str, limit: int = 5000) -> list[dict[str, object]]:
        with self._lock:
            rows = self._require_connection().execute(
                """
                SELECT ts, x, y, vx, vy, confidence, sources
                FROM track_points WHERE track_id = ? ORDER BY ts LIMIT ?
                """,
                (track_id, min(max(limit, 1), 20000)),
            ).fetchall()
        return [dict(row) for row in rows]

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("TrajectoryStore is not initialized")
        return self._connection

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
