# Agent instructions — MMWave Fusion

Home Assistant custom integration that fuses several mmWave radars into one
room-frame picture. Python, no third-party requirements, `local_push`.

This integration ships **no frontend**. The
[card](https://github.com/zomco/mmwave-card) is the only user interface and the
only thing that configures this backend — which is why `config_flow.py` asks
for nothing. Do not add UI here.

---

## The coordinate transform is a cross-repo invariant

`fusion.py::transform_point` implements the room-frame convention:

**R = Rz(yaw) · Rx(pitch) · Ry(roll)**, `yaw = 0` aims along room **+Y**,
positive yaw turns toward **+X**, `room_z = radar_z − world_z`. All lengths in
**centimetres**.

**The same convention is implemented three times, in three repositories:**

| Repository | File |
| --- | --- |
| **mmwave-fusion** (here) | `custom_components/mmwave_fusion/fusion.py` |
| [mmwave-component](https://github.com/zomco/mmwave-component) | `components/{model}/{model}_transform.h` (one per model) |
| [mmwave-card](https://github.com/zomco/mmwave-card) | `src/utils/transform.ts` |

Changing one alone silently mirrors or rotates every user's coordinates, and
**this repository's own tests will still pass**. The cross-check lives in the
[workspace](https://github.com/zomco/mmwave-workspace):

```bash
python -m unittest tests.unit.test_rotation_convention -v
```

It treats `fusion.py` as the reference and evaluates the card's TypeScript
expressions against it. Any change to the convention must land in all three
repositories together.

---

## Layout

```
custom_components/mmwave_fusion/
├── __init__.py        # Setup, YAML import shim
├── config_flow.py     # Config entry creation — asks nothing on purpose
├── const.py           # ← API_VERSION lives here
├── coordinator.py     # Per-system lifecycle and push loop
├── fusion.py          # ← transform_point, clustering, association, tracking
├── frames.py          # Atomic target frame decode
├── quality.py         # Trajectory scoring, recording admission
├── events.py          # Zone events, camera recording orchestration
├── storage.py         # SQLite schema, writes, retention
├── profiles.py        # Shared calibration profiles keyed by HA device_id
├── websocket_api.py   # Card-facing commands
├── entity.py / sensor.py / binary_sensor.py
└── translations/
```

---

## Rules

### API versioning

`API_VERSION` in `const.py` is stamped onto every push. The card refuses a
backend older than it needs and reports that in the UI. **Bump it whenever the
pushed payload shape or a websocket command's contract changes**, and say so in
the commit — the card is released independently and has no other way to tell.

### Entity availability

Entities report `unavailable` until the first frame arrives. Do not default
them to `0` / `off`: that asserts an empty room for a system that has produced
nothing, which silently drives automations.

### Websocket permissions

Configuration commands are admin-only. `subscribe`, `query_events` and
`query_track` are deliberately **not**, so non-administrator household members
can see their own home. This trade-off is documented in the README under
"WebSocket API and who can call it" — do not tighten or loosen it silently, and
keep the README in step if it changes.

### Storage

`track_points` dominates the database — roughly 51 MB/day on the development
instance. Retention runs every 6 hours. Clips are never pruned because the row
is the only pointer to the file on disk.

Only two summary entities go to the HA Recorder. Never route trajectory data
through it.

### Testing

Tests live in the workspace, not here, and stub Home Assistant rather than
importing it:

```bash
python -m unittest discover -s tests/unit
```

Keep it that way — importing `homeassistant` would make the suite need a full
HA install.

### Documentation

The README is bilingual: `README.md` and `README_CN.md`. HACS renders
`README.md` on the integration page (`render_readme: true` in `hacs.json`).
Update both in the same change when behaviour changes.
