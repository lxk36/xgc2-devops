# xgc2_b2_link (G3 + G4 aligned)

**XGC2 独立子产品** — 产品 ID `xgc2-b2-link`（见 [docs/PRODUCT.md](./docs/PRODUCT.md)、[`.xgc2/product.yml`](./.xgc2/product.yml)）。

| 部署 | 方式 |
|------|------|
| **Thor 机载** | 目标：`sudo apt install ros-jazzy-xgc2-b2-link` 后 `ros2 run …` |
| **地面开发** | **源码** `PYTHONPATH` + Noetic rospy（最快） |
| **两头都要快** | 机载 APT + 地面源码并行；契约同一文件 `contract/zenoh_v1.yaml` |

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

**Data path is not “open 8080 and a dog appears”.** Lichtblick only shows a robot after:

1. Backend is live (odom / joints / TF / `robot_description` / meshes), and  
2. UI has a **3D panel + URDF layer** (empty default layout shows no dog).

### Backend verify (must pass before blaming UI)

```bash
source /opt/ros/noetic/setup.bash
export ROS_PACKAGE_PATH=$PWD/../../robot/b2arx_description:$ROS_PACKAGE_PATH
export PYTHONPATH=/opt/ros/noetic/lib/python3/dist-packages:$PWD
# topics
rostopic echo -n 1 /remote/b2/odom
rostopic echo -n 1 /joint_states
# TF
rosrun tf tf_echo odom b2_description
# foxglove must advertise those topics (script checks this)
```

### Start stack (one shot)

```bash
cd products/ros2/driver/xgc2_b2_link
bash scripts/one_shot_viz.sh
```

Checks TF + foxglove advertise + URDF param + **CORS layout server**, then prints one URL.

### Open URL

**Recommended** (HTML trampoline — always expands the full data-source id):

```text
http://127.0.0.1:8091/open_b2_sim.html
```

Direct (must keep `ds` **exactly** `foxglove-websocket`, never truncated `foxglove-`):

```text
http://127.0.0.1:8080/?ds=foxglove-websocket&ds.url=ws%3A%2F%2F127.0.0.1%3A8080%2Fws&layoutUrl=http%3A%2F%2F127.0.0.1%3A8091%2Fb2_sim_3d.json
```

Short form (Lichtblick injects `ds` via auto-connect when `ds` is omitted):

```text
http://127.0.0.1:8080/?layoutUrl=http%3A%2F%2F127.0.0.1%3A8091%2Fb2_sim_3d.json
```

| Console message | Meaning |
|-----------------|---------|
| `[followTf] No coordinate frames found` | 3D panel has no TF because the WebSocket data source is not connected (wrong/truncated `ds`). Backend `/tf` is usually fine. |
| `called render done function twice` | Benign panel remount race in Lichtblick; ignore. |
| `Unknown data source: foxglove-` | `ds` was truncated — use the trampoline URL above. |

- Layout server uses **CORS** (`scripts/cors_static_server.py`) so `layoutUrl` is not blocked.
- Layout ships a **3D panel with URDF layer** (`/robot_description`). Hard-refresh after restart.
- Confirm connection: Topics sidebar should list `/tf`, `/remote/b2/odom`, etc., and 3D Fixed/Follow frame shows `b2_description` / `odom`.

### What the stack runs

1. sim_publisher (walk + circle odom + arm + battery)  
2. ground_peer ROS1 recovery  
3. robot_state_publisher  
4. foxglove_bridge `:8765` (assets + package:// meshes)  
5. xgc2-lichtblick-web `:8080`

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
