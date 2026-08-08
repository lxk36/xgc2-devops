# xgc2_b2_link (G3 + G4 aligned)

Shared **Zenoh key contract** and first implementation slice for:

| Role | Component | Side |
|------|-----------|------|
| **G3** | `b2_forwarder_d0` | Thor / onboard — ROS2 → transport |
| **G4** | `b2_ground_peer` | Ground — transport → snapshot (+ optional local ROS) |

Full **B2 Adapter Runtime / Go telemetry** still plugs into the same keys; this package freezes the wire so both sides cannot drift.

## Contract

- File: [`contract/zenoh_v1.yaml`](./contract/zenoh_v1.yaml)
- Keys: `xgc2/{robot_id}/up/*`, `xgc2/{robot_id}/down/cmd`
- Encodings: ROS2 CDR envelope (`XGC2` magic) for odom/joint; JSON for `power_summary` and commands

## Transport

1. **`zenoh`** — when `eclipse-zenoh` is installed (`pip install eclipse-zenoh`)
2. **`tcp`** — framed fallback for lab loopback without Zenoh (default in tests)
3. **`auto`** — zenoh if importable, else tcp

## Quick loopback (no ROS)

Terminal A (G4 ground — TCP server):

```bash
cd products/ros2/driver/xgc2_b2_link
PYTHONPATH=. python3 -m xgc2_b2_link.ground_peer \
  --robot-id b2-01 --transport tcp --tcp-role server --tcp-port 7448 --send-echo
```

Terminal B (G3 onboard dry-run — TCP client):

```bash
cd products/ros2/driver/xgc2_b2_link
PYTHONPATH=. python3 -m xgc2_b2_link.forwarder_node \
  --robot-id b2-01 --transport tcp --tcp-role client --tcp-port 7448 --dry-run-no-ros
```

## Onboard with ROS 2 (domain 0)

```bash
# source Jazzy + workspace with b2_ros2_driver for LowState
ros2 run xgc2_b2_link b2_forwarder_d0 --ros-args \
  -p use_sim_time:=false -- \
  --robot-id b2-01 --transport auto --tcp-role client
```

## Tests

```bash
cd products/ros2/driver/xgc2_b2_link
PYTHONPATH=. python3 -m pytest test/ -q
```

## Visualization closed loop (sim → ROS1 recovery → foxglove → Lichtblick)

Host: ROS Noetic + `foxglove_bridge` + `robot_state_publisher` + URDF `b2arx_description`.

```bash
cd products/ros2/driver/xgc2_b2_link
export PYTHONPATH=$PWD
# one-shot stack (Ctrl-C stops):
bash scripts/viz_closed_loop.sh
```

What it starts:

1. `roscore` (if needed)
2. `/robot_description` from `b2arx_visual.urdf` (mesh paths rewritten to `file://`)
3. **G4** `ground_peer --publish-ros` (TCP server) — recovers:
   - `/remote/b2/odom`, `/remote/b2/joint_states`, `/remote/b2/path`, `/remote/b2/power_summary`
   - `/joint_states` (dog URDF joints + R5a arm joints)
   - `/remote/arm/slave_joint_states`
   - TF `odom` → `b2_description`
4. **Sim** `sim_publisher` (TCP client) — walk gait + circle odom + arm motion + battery
5. `robot_state_publisher` — full TF tree from URDF + joints
6. `foxglove_bridge` on `ws://127.0.0.1:8765`
7. `xgc2-lichtblick-web` if installed — open UI and connect Foxglove WebSocket to `8765`

Manual verify:

```bash
source /opt/ros/noetic/setup.bash
rostopic echo -n 1 /remote/b2/odom
rostopic echo -n 1 /joint_states
# path history:
rostopic hz /remote/b2/path
```

In Lichtblick / Foxglove Studio: add **3D** panel, fixed frame `odom`, enable TF, Robot (from `robot_description` / URDF), topics `/remote/b2/path`, `/remote/b2/odom`.

## MVP topics (G3→G4)

| Onboard topic | Key | Rate |
|---------------|-----|------|
| `/b2/odom` | `up/odom` | 15 Hz |
| `/b2/joint_states` | `up/joint_states` | 15 Hz |
| `/b2/low_state` → summary | `up/power_summary` | 2 Hz |
| `/b2/driver_status` | `up/driver_status` | 1 Hz |
| cmd reverse | `down/cmd` → `/remote/high_level_cmd` | event |

Domain 17 arm forwarder: next slice (`forwarder_d17`).

## Owners

- G3/G4: this package + panel §19 / §15
- Do not split a separate “ROS restore microservice” on ground — G4 peer is the transport half of the **one** B2 Adapter.
