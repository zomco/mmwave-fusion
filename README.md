# MMWave Fusion

[中文文档](./README_CN.md)

Home Assistant integration that turns one or more **2D** mmWave radars into one room-frame picture: shared coordinates, tracks, zone events, and optional stills and short clips from a live camera stream. The [card](https://github.com/zomco/mmwave-card) is the only UI. This integration asks for nothing in its config flow.

Experimental. A single radar that only needs an Occupied bit does not need this.

## Quick start

1. Flash the radar with [mmwave-component](https://github.com/zomco/mmwave-component) and install [mmwave-card](https://github.com/zomco/mmwave-card). Calibrate there first.
2. In HACS, add this repository as a custom repository, category **Integration**. Install it and restart Home Assistant.
3. **Settings → Devices & services → Add integration → MMWave Fusion**. Leave the form empty.
4. Open the card's fusion panel, add 2D radars, and run joint calibration. Do not configure this in YAML.

1D radars are not offered. They report distance without direction, so there is nothing to fuse.

| You have | You need |
| --- | --- |
| One radar, Occupied only (bathroom, desk, bed) | The card. Stop here. |
| One 2D radar, and you want crossings, replay, or a heatmap | This integration, with one radar in `radars[]`. |
| Several 2D radars, a merged view, nothing stored | The card alone. Fusion stays in the browser. |
| Stored tracks, zone events, or camera clips | This integration. |

This is not an NVR search. Clips come from the live Home Assistant stream. A presence bit cannot produce a track.

## Documentation map

| If you want | Read |
| --- | --- |
| Flash and first calibration | [Getting Started](https://github.com/zomco/mmwave-component/blob/main/GETTING-STARTED.md) |
| Card setup and YAML | [mmwave-card DIY](https://github.com/zomco/mmwave-card/blob/main/DIY.md) |
| Tracker, zones, scoring (no Home Assistant) | [mmwave-engine](https://github.com/zomco/mmwave-engine) |
| Light, occupancy, crossing, and dwell automations | [`blueprints/automation/mmwave_fusion/`](blueprints/automation/mmwave_fusion) |
| Coordinate convention and what not to change | [AGENTS.md](./AGENTS.md) |
| Tests | [mmwave-workspace](https://github.com/zomco/mmwave-workspace) `tests/unit` |

Import a blueprint from **Settings → Automations & scenes → Blueprints**. Indoor bathroom, desk, and bed presence stays on the device's Area Occupied entity, not these blueprints.

## What it exposes

One device per fusion system:

| Entity | Type | Notes |
| --- | --- | --- |
| `sensor.mmwave_fusion_<id>_target_count` | sensor | Fused track count |
| `binary_sensor.mmwave_fusion_<id>_occupied` | binary_sensor | Occupancy |

Both stay `unavailable` until the first frame. Only these two go to the Recorder. Tracks go to `config/.storage/mmwave_fusion.sqlite`.

| Data | Kept |
| --- | --- |
| `track_points` | 7 days |
| `tracks`, `events` | 90 days |
| clips under `/media/mmwave_fusion` | 30 days |

SQLite does not shrink the file until you stop Home Assistant and run `VACUUM`.

Configuration WebSocket commands are admin-only. `subscribe`, `query_events`, and `query_track` are not, so any Home Assistant user can see live positions and stored tracks. To close that, add `@websocket_api.require_admin` on those three commands in `websocket_api.py`. Non-administrators then lose the live view.

## Radars

**1D radars cannot use this integration.** **2D radars can use every feature in the project**, including this one. R60ABD1 also reports height; it is listed with the 2D models because it can be fused.

Figures and progress match [mmwave-component](https://github.com/zomco/mmwave-component/blob/main/README.md). `—` means the manual does not state it. A cell marked *accuracy* is not a range-gate width. LD2410 at 0.20 m resolution reaches 1.6 m. LD2451 reports whole metres. LD2450 and LD2454 publish resolution in each target frame. LD2454's performance table says 8 m; the install section says 6 m tracking.

### 1D radars

| Model | Detection distance | Distance resolution | Progress |
| --- | --- | --- | --- |
| LD2410 | 6 m | 0.75 m / 0.20 m | Developing |
| LD2410B | 6 m | 0.75 m (protocol can also select 0.20 m) | Testing |
| LD2410C | 6 m | 0.75 m (protocol can also select 0.20 m) | Testing |
| LD2411 | 2 m (gesture 1 m) | — | Developing |
| LD2411S | 6 m (micro-motion 3.5 m) | ±5 cm / ±5% (accuracy) | Developing |
| LD2412 | 9 m | 0.75 m | Developing |
| LD2420 | 8 m (micro-motion 6 m) | 0.70 m | Developing |
| LD2450A | 2 m | 0.2 m (sensing accuracy) | Developing |
| LD6002 | 1.5 m (breath / heart) | — | Developing |
| RD-03E | 6 m (micro-motion 3.5 m) | ±5 cm / ±5% (accuracy) | Developing |

### 2D radars

| Model | Detection distance | Angle | Distance resolution | Progress |
| --- | --- | --- | --- | --- |
| LD2450 | 6 m | azimuth ±60°, elevation ±35° | per frame | Developing |
| LD2451 | 100 m | horizontal ±15°, vertical ±7° | 1 m | Testing |
| LD2452 | 6 m | azimuth ±60°, elevation ±30° | 0.72 m | Testing |
| LD2453 | 6 m | azimuth ±40°, elevation ±30° | 0.72 m | Developing |
| LD2454 | 8 m | azimuth ±60°, elevation ±30° | per frame | Testing |
| R60ABD1 | 2.5 m (breath / heart 1.5 m) | ±20° | — | Completed |

## License

MIT
