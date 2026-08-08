#!/usr/bin/env bash
# Closed-loop sim: sim_publisher → TCP → ground_peer(ROS1) → RSP + foxglove_bridge [+ lichtblick]
# Host requirements: ROS Noetic (rospy, robot_state_publisher, foxglove_bridge), PyYAML
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$ROOT/../../../.." && pwd)"  # products/ros2/driver/xgc2_b2_link → products
# monorepo root: xgc2-devops/products/ros2/driver/xgc2_b2_link → up 5 = xgc2-devops? 
# ROOT = .../xgc2_b2_link
# urdf under monorepo
URDF_DEFAULT="$ROOT/../../robot/b2arx_description/urdf/b2arx_visual.urdf"
if [[ ! -f "$URDF_DEFAULT" ]]; then
  URDF_DEFAULT="/home/lxk/Dev/xgc2-vibe-coding/xgc2-devops/products/ros2/robot/b2arx_description/urdf/b2arx_visual.urdf"
fi

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
ROBOT_ID="${ROBOT_ID:-b2-sim}"
PORT="${PORT:-7448}"
URDF="${URDF_PATH:-$URDF_DEFAULT}"
RUN_LICHTBLICK="${RUN_LICHTBLICK:-1}"
FOXGLOVE_PORT="${FOXGLOVE_PORT:-8765}"
LICHTBLICK_PORT="${LICHTBLICK_PORT:-18081}"
LOG_DIR="${LOG_DIR:-/tmp/xgc2_b2_viz_$$}"
mkdir -p "$LOG_DIR"

PIDS=()
cleanup() {
  set +e
  for p in "${PIDS[@]:-}"; do
    kill "$p" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  echo "[viz] cleaned up (logs in $LOG_DIR)"
}
trap cleanup EXIT INT TERM

source /opt/ros/noetic/setup.bash

if ! rostopic list &>/dev/null; then
  echo "[viz] starting roscore..."
  roscore >"$LOG_DIR/roscore.log" 2>&1 &
  PIDS+=($!)
  for _ in $(seq 1 50); do
    rostopic list &>/dev/null && break
    sleep 0.1
  done
fi

if [[ ! -f "$URDF" ]]; then
  echo "[viz] URDF not found: $URDF" >&2
  exit 1
fi

# Fix package:// mesh paths for param-based load (point to share-like dir)
URDF_DIR="$(cd "$(dirname "$URDF")" && pwd)"
MESH_ROOT="$(cd "$URDF_DIR/.." && pwd)"
# b2arx uses package://b2arx_description/... — rewrite for file://
TMP_URDF="$LOG_DIR/b2arx_visual.resolved.urdf"
sed "s|package://b2arx_description/|file://${MESH_ROOT}/|g" "$URDF" >"$TMP_URDF"
# rosparam CLI YAML-parses the value and chokes on URDF ':' — set via rospy
python3 - "$TMP_URDF" <<'PY'
import sys
import rospy
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    xml = f.read()
rospy.init_node("xgc2_set_robot_description", anonymous=True)
rospy.set_param("/robot_description", xml)
print("[viz] robot_description loaded bytes=", len(xml))
PY

echo "[viz] robot_description loaded from $TMP_URDF"

# G4 ground peer (TCP server) with ROS1 recovery
python3 -m xgc2_b2_link.ground_peer \
  --robot-id "$ROBOT_ID" \
  --transport tcp --tcp-role server --tcp-port "$PORT" \
  --publish-ros --print-hz 0.5 \
  >"$LOG_DIR/ground_peer.log" 2>&1 &
PIDS+=($!)
sleep 0.5

# G3-side scene simulator (acts as forwarder data source)
python3 -m xgc2_b2_link.sim_publisher \
  --robot-id "$ROBOT_ID" \
  --transport tcp --tcp-role client --tcp-port "$PORT" \
  >"$LOG_DIR/sim_publisher.log" 2>&1 &
PIDS+=($!)
sleep 0.8

# robot_state_publisher for TF from joint_states + URDF
rosrun robot_state_publisher robot_state_publisher \
  >"$LOG_DIR/robot_state_publisher.log" 2>&1 &
PIDS+=($!)

# Wait for recovered topics
echo "[viz] waiting for /remote/b2/odom ..."
ok=0
for _ in $(seq 1 40); do
  if rostopic list 2>/dev/null | grep -q '/remote/b2/odom'; then
    ok=1
    break
  fi
  sleep 0.25
done
if [[ "$ok" != 1 ]]; then
  echo "[viz] FAIL: odom not recovered. ground log:" >&2
  tail -30 "$LOG_DIR/ground_peer.log" >&2 || true
  exit 2
fi

echo "[viz] ROS1 topics (sample):"
rostopic list | grep -E 'remote/b2|joint_states|remote/arm' || true
echo "[viz] odom sample:"
timeout 2 rostopic echo -n 1 /remote/b2/odom 2>/dev/null | head -25 || true

# foxglove_bridge whitelist via env / private ~ not all versions support CLI
# Use parameters if available
rosparam set /foxglove_bridge/port "$FOXGLOVE_PORT" 2>/dev/null || true
rosparam set /foxglove_bridge/address "0.0.0.0" 2>/dev/null || true
# topic_whitelist as list of regex
python3 - <<'PY'
import rospy
rospy.init_node("xgc2_foxglove_cfg", anonymous=True)
topics = [
    r"^/remote/b2/odom$",
    r"^/remote/b2/joint_states$",
    r"^/remote/b2/path$",
    r"^/remote/b2/power_summary$",
    r"^/remote/arm/slave_joint_states$",
    r"^/joint_states$",
    r"^/tf$",
    r"^/tf_static$",
]
rospy.set_param("/foxglove_bridge/topic_whitelist", topics)
rospy.set_param("/foxglove_bridge/service_whitelist", [])
rospy.set_param("/foxglove_bridge/param_whitelist", [r"^/robot_description$"])
rospy.set_param("/foxglove_bridge/capabilities", ["clientPublish", "parameters", "parametersSubscribe", "assets", "connectionGraph", "services"])
print("foxglove params set", topics)
PY

rosrun foxglove_bridge foxglove_bridge \
  _port:="$FOXGLOVE_PORT" \
  _address:="127.0.0.1" \
  >"$LOG_DIR/foxglove_bridge.log" 2>&1 &
PIDS+=($!)
sleep 1

if ss -ltn 2>/dev/null | grep -q ":${FOXGLOVE_PORT}"; then
  echo "[viz] foxglove_bridge listening on ws://127.0.0.1:${FOXGLOVE_PORT}"
else
  echo "[viz] WARN: foxglove port not detected; see $LOG_DIR/foxglove_bridge.log"
  tail -20 "$LOG_DIR/foxglove_bridge.log" || true
fi

if [[ "$RUN_LICHTBLICK" == "1" ]] && [[ -x /usr/bin/xgc2-lichtblick-web ]]; then
  # Lichtblick web typically serves UI; connect to foxglove ws in layout
  XGC2_LICHTBLICK_PORT="$LICHTBLICK_PORT" \
    /usr/bin/xgc2-lichtblick-web \
    >"$LOG_DIR/lichtblick.log" 2>&1 &
  PIDS+=($!)
  sleep 1
  echo "[viz] lichtblick-web started (log $LOG_DIR/lichtblick.log)"
  echo "[viz] Open UI (if served): http://127.0.0.1:${LICHTBLICK_PORT}/"
  echo "[viz] In layout: Foxglove WebSocket → ws://127.0.0.1:${FOXGLOVE_PORT}"
else
  echo "[viz] skip lichtblick (RUN_LICHTBLICK=0 or binary missing)"
  echo "[viz] Connect Foxglove Studio / Lichtblick to ws://127.0.0.1:${FOXGLOVE_PORT}"
fi

echo ""
echo "=== closed loop running ==="
echo "  robot_id=$ROBOT_ID  logs=$LOG_DIR"
echo "  recovered: /remote/b2/{odom,joint_states,path,power_summary}"
echo "             /joint_states  /remote/arm/slave_joint_states  tf"
echo "  foxglove:  ws://127.0.0.1:${FOXGLOVE_PORT}"
echo "  Ctrl-C to stop"
echo ""

# keep alive
while true; do
  sleep 5
  # health: ensure sim still up
  if ! kill -0 "${PIDS[1]:-0}" 2>/dev/null; then
    echo "[viz] a child exited early; check $LOG_DIR" >&2
    tail -40 "$LOG_DIR"/*.log >&2 || true
    exit 3
  fi
done
