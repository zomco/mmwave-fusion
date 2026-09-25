"""Video sink the engine asks for. Shells implement it; this file does not."""

from __future__ import annotations

from typing import Protocol


class VideoSink(Protocol):
    """Grab a still or a short clip from a live camera. Not an NVR timeline."""

    async def snapshot(self, entity_id: str, filename: str) -> None:
        """Write a JPEG at filename."""

    async def record(self, entity_id: str, filename: str, lookback: int, duration: int) -> None:
        """Write an MP4 covering lookback seconds before now, plus duration."""
