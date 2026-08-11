# MMWave Fusion

[中文文档](./README_CN.md)

Home Assistant integration that fuses several mmWave radars into one room-frame
picture: it ingests each radar's targets, transforms them into a shared floor
plan, associates and tracks them across radars, scores the resulting
trajectories, and persists events and clips.

> **Experimental.** Single-radar setups do not need this.

---

## Do you need this?

Probably not, unless you are running more than one radar in one space.

| You have | You need |
| --- | --- |
| One radar in a room | Just the [card](https://github.com/zomco/mmwave-card). Stop here. |
| Several radars, and you want one merged view | The card alone will do it — **in the browser**, with nothing stored. |
| Several radars, and you want stored trajectories, zone events, camera clips | This integration. |

The card is the only user interface; this integration ships no frontend of its
own. It is configured entirely from the card, which is why its config flow asks
for nothing.

Fusion needs radars that report **2-D position**. Range-only models report
distance without direction, so there is nothing to fuse and the card's editor
does not offer them.

### The three pieces

| Repository | What it is | Needed? |
| --- | --- | --- |
| [mmwave-component](https://github.com/zomco/mmwave-component) | ESPHome firmware | Yes — the device side. |
| [mmwave-card](https://github.com/zomco/mmwave-card) | Lovelace card (HACS: **plugin**) | Yes — the only UI. |
| **mmwave-fusion** (this) | HA integration (HACS: **integration**) | Multi-radar only. |

The card and this integration are released independently, so the integration
stamps `api_version` (currently **1**) onto every push, and the card refuses to
run against a backend older than it needs — saying so in the UI rather than
half-working.

---

## Installation

### Via HACS

Add this repository as a custom repository under the **Integration** category,
then install and restart Home Assistant.

### Then add the integration

The integration ships a config flow, so once the files are in
`config/custom_components/mmwave_fusion/` it is added from
**Settings → Devices & services → Add integration → MMWave Fusion**.

There is nothing to fill in — radars, zones and cameras are all configured from
the card.

An existing YAML setup keeps working. A leftover

```yaml
mmwave_fusion:
```

block triggers a one-shot import that creates the config entry; the block can
be removed afterwards.

### Then configure it from the card

Open the card's fusion panel, add your radars, and run the joint calibration.
Nothing here is configured in YAML.

---

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

Only these two summary entities go to the HA Recorder. Trajectory data goes to
its own database (below) to keep the recorder from being flooded.

---

## How it works

1. The backend subscribes to each radar's atomic target frames; older devices
   without atomic frames still work via their split X/Y entities.
2. Each radar's coordinates are transformed into one shared floor-plan frame
   using its installation position and yaw/pitch/roll.
3. Nearby observations from different radars are clustered, then a global
   minimum-cost assignment plus an alpha-beta tracker maintains `track_id`.
4. `track_ttl_s` lets a track survive a brief dropout; `confirm_hits` rejects
   one-off false positives.
5. Tracks, events and radar calibration health are pushed to the card over
   `mmwave_fusion/subscribe`.

### Coordinate convention

`yaw = 0` aims along room **+Y**, positive turns toward **+X**. Floor-plan FOV,
the zone editor, the 3-D installation view and this backend all share it.

> **This convention is implemented three times, in three repositories** — here
> in `fusion.py::transform_point`, in the ESPHome components, and in the card's
> `src/utils/transform.ts`. Changing one alone silently mirrors everyone's
> coordinates while this repository's own tests stay green. See
> [AGENTS.md](./AGENTS.md).

### Calibration diagnostics

The backend tracks what fraction of each radar's transformed observations land
inside the floor-plan rectangle. After at least 100 observations, a radar with
under 20% inside triggers a calibration warning in the card. This catches a
flipped yaw sign, a wrong installation point, or a unit mismatch.

---

## Trajectory quality and recording admission

Zones still produce raw `enter`, `exit` and `dwell` events. When a track ends,
the quality engine additionally produces:

- **`traverse`** — a track that meets the hard conditions and scores above
  threshold. These can trigger recording.
- **`trajectory`** — a track that did not qualify. Diagnostics are stored; no
  recording.

Scoring is out of 100 and combines: whether a complete enter/exit topology
formed, the valid-observation ratio and largest gap, displacement and path
efficiency, largest position jump, the fraction of observations inside the
floor plan, how many radars contributed, and track duration.

Brief false positives, insufficient displacement, intermittent observation,
large jumps, or tracks mostly outside the floor plan are rejected. The
rejection reason, sub-scores, metrics and per-camera recording decision are all
written into event metadata and shown in the card.

### Recording

Only `recording_source: ha_live` is supported. Camera SD cards and NVR archives
are not read.

1. An HA HLS stream is pre-warmed for each camera entity at startup.
2. HA reuses that camera's single decode worker and keeps roughly 30 s of
   segments in memory.
3. Only key events allowed by the camera's `event_types` call `camera.record`.
4. Dynamic lookback covers as much of the track as possible, capped at
   `buffer_seconds` (max 30 s).
5. The call uses blocking completion, then verifies the file exists and is
   non-empty before the status becomes `ready`.

This means no ISAPI queries, no historical RTSP playback, no repeated indexing
against the camera. The steady-state load is one live stream per camera, and HA
writes a clip to `/media/mmwave_fusion/<fusion_id>/...` only for key tracks.

`cooldown_s` limits the minimum interval between recordings for the same
camera, zone and event type. Event queries return `waiting`, `extracting`,
`ready` or `failed` explicitly, and failures surface in the card.

---

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

Write frequency is limited by `quality.persist_interval_s`, which drops the
default 10 Hz fusion rate to at most 2 Hz on disk, and only points backed by an
actual radar observation are stored.

SQLite reuses freed pages but does not shrink the file, so pruning stops growth
without reclaiming space already allocated. To reclaim it, stop Home Assistant
once and run `VACUUM` against the database.

---

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

---

## Blueprints

Two automation blueprints ship in
[`blueprints/automation/mmwave_fusion/`](blueprints/automation/mmwave_fusion).
Import either by URL from **Settings → Automations & scenes → Blueprints →
Import blueprint**.

### Light follows zone presence

[`zone_presence_light.yaml`](blueprints/automation/mmwave_fusion/zone_presence_light.yaml)

Turns a light on while a zone is occupied and off once it is empty.

This is not the stock motion-light blueprint with a different sensor. A PIR
reports *movement*, so every motion-light automation needs a timeout, and every
timeout is a compromise between dying on someone who is reading and burning for
ten minutes after they leave. A fused zone reports *presence*, so there is
nothing to guess. The grace period exists to ride out a single dropped frame,
not to estimate how long a person might sit still — needing more than a minute
of it means there is a calibration problem being worked around.

### Notify on a scored crossing

[`traverse_notification.yaml`](blueprints/automation/mmwave_fusion/traverse_notification.yaml)

Fires on a `traverse` event: a complete, well-observed path through a zone, as
judged by the quality engine described above. Trajectories that fail those
checks are emitted as `trajectory` with a reason and are not matched — which is
the point, because a notification that fires on every reflection off a curtain
is one people turn off. The minimum score is a second filter for rooms where
even a clean crossing is not always worth a phone buzzing.

---

## Development

| Module | Responsibility |
| --- | --- |
| `fusion.py` | Coordinate transform, clustering, association, tracking |
| `frames.py` | Atomic target frame decode |
| `coordinator.py` | Per-system lifecycle and push loop |
| `quality.py` | Trajectory scoring and recording admission |
| `events.py` | Zone events, camera recording orchestration |
| `storage.py` | SQLite schema, writes, retention |
| `profiles.py` | Shared calibration profiles keyed by HA `device_id` |
| `websocket_api.py` | The commands above |
| `config_flow.py` | Config entry creation (asks nothing) |

Unit tests live in the development workspace
([mmwave-workspace](https://github.com/zomco/mmwave-workspace)), which carries
this repository as a submodule alongside the card and the ESPHome components:

```bash
python -m unittest discover -s tests/unit
```

They stub Home Assistant rather than importing it, so they run without a Home
Assistant installation.

---

## License

MIT
