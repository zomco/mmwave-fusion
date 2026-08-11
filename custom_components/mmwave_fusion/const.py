"""Constants for the MMWave Fusion integration."""

from __future__ import annotations

DOMAIN = "mmwave_fusion"

# WebSocket contract version between this integration and mmwave-card.
#
# The two ship as separate HACS packages and are versioned independently, so a
# user can end up with any combination. Bump this when a command is added, or
# when a request or reply field changes meaning; the card compares it against
# the minimum it needs and says so plainly rather than half-working.
# 2 adds mmwave_fusion/query_heatmap, and the zone_occupancy field on the
# update push that the per-zone entities read. 3 adds
# mmwave_fusion/query_replay. All are additions, so an older card still works —
# it simply does not ask for them.
API_VERSION = 3
STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1

DEFAULT_FUSION_ID = "home"
DEFAULT_RATE_HZ = 10.0
DEFAULT_ASSOCIATION_GATE_CM = 90.0
DEFAULT_MERGE_GATE_CM = 70.0
DEFAULT_TRACK_TTL_S = 1.2
DEFAULT_FRAME_DEBOUNCE_S = 0.05
DEFAULT_POINT_FLUSH_S = 1.0

# History retention. track_points dominates the database: one row per track
# per quality.persist_interval_s, which is twice a second by default and is
# *not* the fusion rate — raising rate_hz makes tracking smoother without
# storing more. tracks and events are metadata and are what the event list
# reads, so they are kept far longer.
DEFAULT_POINT_RETENTION_DAYS = 7
DEFAULT_EVENT_RETENTION_DAYS = 90
PRUNE_INTERVAL_S = 6 * 3600

# Both windows are settable from the config entry's options. The defaults suit
# a house; the cost measured on the development bench was roughly 50 MB per day
# of points, which is the number worth knowing before raising the first one.
OPTION_POINT_RETENTION_DAYS = "point_retention_days"
OPTION_EVENT_RETENTION_DAYS = "event_retention_days"

# A day is the shortest useful window — the heatmap's default view is a day,
# and anything less would leave it empty. The upper bounds are there to stop a
# typo turning into a database nobody can vacuum.
MIN_POINT_RETENTION_DAYS = 1
MAX_POINT_RETENTION_DAYS = 90
MIN_EVENT_RETENTION_DAYS = 1
MAX_EVENT_RETENTION_DAYS = 3650

# How often radar health is checked against the Repairs page. The snapshot
# itself is rebuilt every tick; this only governs how often issues are
# raised or cleared, and none of the conditions matter on a shorter scale.
ISSUE_CHECK_INTERVAL_S = 30.0

# The widest replay a viewer may ask for. Six hours of positions is already a
# large answer even after thinning, and nobody scrubs through a week — the
# heatmap is the tool for that span.
MAX_REPLAY_WINDOW_S = 6 * 3600

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
# Fallback conversion to centimetres, per model, used *only* when a coordinate
# entity carries no unit_of_measurement that entity_coordinate_scale can read.
#
# These describe what the ESPHome entity publishes, not what the radar protocol
# carries on the wire. Every component in mmwave-component converts to
# centimetres before publishing and declares `cm`, so every value here is 1.0.
# ld2450 sat at 0.1 — the unit of its raw protocol frame — which was a ten-fold
# error waiting for the first entity that did not declare a unit, and looked
# authoritative enough that nobody would question it.
MODEL_COORDINATE_SCALE: dict[str, float] = {
    "ld2450": 1.0,
    "ld2451": 1.0,
    "ld2452": 1.0,
    "ld2453": 1.0,
    "ld2454": 1.0,
    "r60abd1": 1.0,
}

SPATIAL_MODELS = frozenset(MODEL_COORDINATE_SCALE)
