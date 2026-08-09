# MMWave Fusion

Home Assistant integration that fuses several mmWave radars into one room-frame
picture: it ingests each radar's targets, transforms them into a shared floor
plan, associates and tracks them across radars, scores the resulting
trajectories, and persists events and clips.

It is the backend half of [mmwave-card](https://github.com/zomco/mmwave-card);
the card is what configures it.

## Installation

The integration ships a config flow, so after the files are in
`config/custom_components/mmwave_fusion/` it is added from
**Settings → Devices & services → Add integration → MMWave Fusion**. There is
nothing to fill in — radars, zones and cameras are configured from the card.

An existing YAML setup keeps working. A leftover

```yaml
mmwave_fusion:
```

block triggers a one-shot import that creates the config entry; the block can
be removed afterwards.

### HACS

`hacs.json` is present and the integration is HACS-shaped, but HACS resolves an
integration at `<repo-root>/custom_components/<domain>/`. In this workspace the
integration lives under `ha-config/`, which is the Home Assistant configuration
directory for development, so **the workspace repository is not itself
installable through HACS**. Publishing it means copying
`ha-config/custom_components/mmwave_fusion/` to the root of a dedicated
repository as `custom_components/mmwave_fusion/`, along with `hacs.json` and
this README.

## What it exposes

Per fusion system, grouped under one device:

| Entity | Type | Notes |
| --- | --- | --- |
| `sensor.mmwave_fusion_<id>_target_count` | sensor | Fused track count. Attributes: `online_radars`, `radar_count`, `multi_source_targets`, `calibration_warnings` |
| `binary_sensor.mmwave_fusion_<id>_occupied` | binary_sensor | `occupancy` device class |

Both report `unavailable` until the first frame arrives, rather than asserting
an empty room for a system that has not produced anything yet.

`multi_source_targets` counts tracks seen by two or more radars — the tracks
that fusion actually contributed something to, as opposed to a single radar's
own detection.

## History and retention

Fused tracks, their points, zone events and recorded clips go to
`config/.storage/mmwave_fusion.sqlite`.

`track_points` is written at the fusion rate and dominates the database. On the
development instance it reached 1.66 million rows and 276 MB in 5.4 days, about
51 MB a day. Retention therefore runs every 6 hours:

| Data | Kept |
| --- | --- |
| `track_points` | 7 days |
| `tracks`, `events` | 90 days |
| `clips` and the events that own them | never pruned |

Clips are exempt because the row is the only pointer to the recording on disk;
dropping it would orphan the file rather than reclaim anything.

SQLite reuses freed pages but does not shrink the file, so pruning stops growth
without reclaiming space already allocated. To reclaim it, stop Home Assistant
once and run `VACUUM` against the database.

## WebSocket API and who can call it

The card talks to the integration over these commands:

| Command | Requires admin |
| --- | --- |
| `mmwave_fusion/configure` | yes |
| `mmwave_fusion/get_config` | yes |
| `mmwave_fusion/remove_config` | yes |
| `mmwave_fusion/list_calibration_profiles` | yes |
| `mmwave_fusion/upsert_calibration_profile` | yes |
| `mmwave_fusion/remove_calibration_profile` | yes |
| `mmwave_fusion/subscribe` | **no** |
| `mmwave_fusion/query_events` | **no** |
| `mmwave_fusion/query_track` | **no** |

Everything that changes configuration is admin-only. The three read commands
are deliberately not, so non-administrator household members can open a
dashboard and see their own home.

**Be aware of what that means.** Any Home Assistant user — including a
restricted or guest account — can subscribe to live occupant positions and
query the full stored trajectory history for any fusion system. If that is not
appropriate for your household, add `@websocket_api.require_admin` above those
three commands in `websocket_api.py`; the cost is that non-administrators lose
the live view and the event list.

This is a deliberate default, not an oversight, and it is called out here
rather than changed silently because tightening it degrades the dashboard for
exactly the people it is meant to serve.

## Development

Unit tests live in the workspace, not here:

```bash
python -m unittest discover -s tests/unit
```

They stub Home Assistant rather than importing it, so they run without a Home
Assistant installation. See `tests/unit/ha_stubs.py`.
