"""Constants for the MMWave Fusion integration."""

from __future__ import annotations

DOMAIN = "mmwave_fusion"

# WebSocket contract version between this integration and mmwave-card.
#
# The two ship as separate HACS packages and are versioned independently, so a
# user can end up with any combination. Bump this when a command is added, or
# when a request or reply field changes meaning; the card compares it against
# the minimum it needs and says so plainly rather than half-working.
API_VERSION = 1
STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1

DEFAULT_FUSION_ID = "home"
DEFAULT_RATE_HZ = 10.0
DEFAULT_ASSOCIATION_GATE_CM = 90.0
DEFAULT_MERGE_GATE_CM = 70.0
DEFAULT_TRACK_TTL_S = 1.2
DEFAULT_FRAME_DEBOUNCE_S = 0.05
DEFAULT_POINT_FLUSH_S = 1.0

# History retention. track_points is written at the fusion rate and dominates
# the database; tracks and events are metadata and are what the event list
# reads, so they are kept far longer.
DEFAULT_POINT_RETENTION_DAYS = 7
DEFAULT_EVENT_RETENTION_DAYS = 90
PRUNE_INTERVAL_S = 6 * 3600

# How often radar health is checked against the Repairs page. The snapshot
# itself is rebuilt every tick; this only governs how often issues are
# raised or cleared, and none of the conditions matter on a shorter scale.
ISSUE_CHECK_INTERVAL_S = 30.0

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
