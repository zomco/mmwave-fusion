"""Constants for the MMWave Fusion integration."""

from __future__ import annotations

DOMAIN = "mmwave_fusion"
STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1

DEFAULT_FUSION_ID = "home"
DEFAULT_RATE_HZ = 10.0
DEFAULT_ASSOCIATION_GATE_CM = 90.0
DEFAULT_MERGE_GATE_CM = 70.0
DEFAULT_TRACK_TTL_S = 1.2
DEFAULT_FRAME_DEBOUNCE_S = 0.05
DEFAULT_POINT_FLUSH_S = 1.0

EVENT_TYPE = "mmwave_fusion_event"
SIGNAL_UPDATE = "mmwave_fusion_update"

# Fusion systems are created and destroyed at runtime through the WebSocket
# API, so the entity platforms cannot enumerate them once at setup. These
# signals let the platforms add and drop entities as systems come and go.
SIGNAL_SYSTEM_ADDED = "mmwave_fusion_system_added"
SIGNAL_SYSTEM_REMOVED = "mmwave_fusion_system_removed"

MANUFACTURER = "mmwave"
MODEL = "Multi-radar fusion"


# Values exposed by the current mmwave-card model adapters. Models that only
# provide a range are intentionally excluded from spatial fusion.
MODEL_COORDINATE_SCALE: dict[str, float] = {
    "ld2450": 0.1,
    "ld2451": 1.0,
    "ld2452": 1.0,
    "ld2453": 1.0,
    "ld2454": 1.0,
    "r60abd1": 1.0,
}

SPATIAL_MODELS = frozenset(MODEL_COORDINATE_SCALE)
