#!/usr/bin/env bash
# Verified B2+R5a visualization stack.
# Prerequisite checks: ROS topics + foxglove channel advertise + print browser URL.
# Does NOT claim success until advertise lists odom/joints/tf and robot_description is set.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
B2ARX="${B2ARX_ROOT:-$ROOT/../../robot/b2arx_description}"
if [[ ! -f "$B2ARX/urdf/b2arx_visual.urdf" ]]; then
  B2ARX="/home/lxk/Dev/xgc2-vibe-coding/xgc2-devops/products/ros2/robot/b2arx_description"
fi
B2ARX_PARENT="$(cd "$(dirname "$B2ARX")" && pwd)"

ROBOT_ID="${ROBOT_ID:-b2-sim}"
PORT="${PORT:-7448}"
FOXGLOVE_PORT="${FOXGLOVE_PORT:-8765}"
LOG_DIR="${LOG_DIR:-/tmp/xgc2_b2_viz_verified}"
mkdir -p "$LOG_DIR"
echo "$$" >"$LOG_DIR/parent.pid"

source /opt/ros/noetic/setup.bash
# After setup.bash (it resets ROS_PACKAGE_PATH)
export ROS_PACKAGE_PATH="${B2ARX_PARENT}:${ROS_PACKAGE_PATH:-}"
export PYTHONPATH="/opt/ros/noetic/lib/python3/dist-packages:${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

stop_pidfile() {
  local f="$1"
  if [[ -f "$f" ]]; then
    kill "$(cat "$f")" 2>/dev/null || true
    rm -f "$f"
  fi
}

cleanup() {
  set +e
  stop_pidfile "$LOG_DIR/sim.pid"
  stop_pidfile "$LOG_DIR/ground.pid"
  stop_pidfile "$LOG_DIR/rsp.pid"
  stop_pidfile "$LOG_DIR/fox.pid"
  # do not kill shared roscore or lichtblick unless we started them
  if [[ -f "$LOG_DIR/started_roscore" ]]; then
    stop_pidfile "$LOG_DIR/roscore.pid"
  fi
  if [[ -f "$LOG_DIR/started_lichtblick" ]]; then
    stop_pidfile "$LOG_DIR/lb.pid"
  fi
  echo "[verified-viz] stopped (logs: $LOG_DIR)"
}
trap cleanup EXIT INT TERM

# ---- master ----
if ! python3 -c "import rosgraph; rosgraph.Master('/probe').getPid()" 2>/dev/null; then
  nohup roscore >"$LOG_DIR/roscore.log" 2>&1 &
  echo $! >"$LOG_DIR/roscore.pid"
  touch "$LOG_DIR/started_roscore"
  for _ in $(seq 1 50); do
    python3 -c "import rosgraph; rosgraph.Master('/probe').getPid()" 2>/dev/null && break
    sleep 0.1
  done
fi

# ---- robot_description with package:// (foxglove asset allowlist) ----
python3 - <<PY
import rospy
rospy.init_node("xgc2_set_rd", anonymous=True)
path = "${B2ARX}/urdf/b2arx_visual.urdf"
xml = open(path, "r", encoding="utf-8").read()
assert "package://b2arx_description/" in xml
rospy.set_param("/robot_description", xml)
print("[verified-viz] robot_description package:// bytes=", len(xml))
PY

# ---- kill previous stack pieces we own (by pid files only if re-run) ----
stop_pidfile "$LOG_DIR/sim.pid" || true
stop_pidfile "$LOG_DIR/ground.pid" || true
stop_pidfile "$LOG_DIR/rsp.pid" || true
stop_pidfile "$LOG_DIR/fox.pid" || true

# ---- G4 ground + sim ----
python3 -m xgc2_b2_link.ground_peer \
  --robot-id "$ROBOT_ID" --transport tcp --tcp-role server --tcp-port "$PORT" \
  --publish-ros --print-hz 0 \
  >"$LOG_DIR/ground.log" 2>&1 &
echo $! >"$LOG_DIR/ground.pid"
sleep 0.6
python3 -m xgc2_b2_link.sim_publisher \
  --robot-id "$ROBOT_ID" --transport tcp --tcp-role client --tcp-port "$PORT" \
  >"$LOG_DIR/sim.log" 2>&1 &
echo $! >"$LOG_DIR/sim.pid"

# ---- RSP ----
rosrun robot_state_publisher robot_state_publisher \
  >"$LOG_DIR/rsp.log" 2>&1 &
echo $! >"$LOG_DIR/rsp.pid"

# wait for odom
echo "[verified-viz] waiting for recovered odom..."
ok=0
for _ in $(seq 1 50); do
  if timeout 1 rostopic echo -n 1 /remote/b2/odom >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 0.2
done
if [[ "$ok" != 1 ]]; then
  echo "[verified-viz] FAIL: no /remote/b2/odom" >&2
  tail -40 "$LOG_DIR/ground.log" "$LOG_DIR/sim.log" >&2 || true
  exit 2
fi

# TF check
python3 - <<'PY'
import rospy, tf2_ros
rospy.init_node("tf_check", anonymous=True)
buf = tf2_ros.Buffer(cache_time=rospy.Duration(3.0))
_ = tf2_ros.TransformListener(buf)
rospy.sleep(1.0)
for parent, child in [("odom", "b2_description"), ("b2_description", "b2_description_FL_calf"), ("b2_description", "R5a_link3")]:
    t = buf.lookup_transform(parent, child, rospy.Time(0), rospy.Duration(2.0))
    print(f"[verified-viz] TF OK {parent} -> {child}")
print("[verified-viz] TF tree healthy")
PY

# ---- foxglove via launch (package:// assets + capabilities) ----
# stop any stray bridge on port without matching our pidfile
if ss -ltn | grep -q ":${FOXGLOVE_PORT}"; then
  echo "[verified-viz] port ${FOXGLOVE_PORT} busy; attempting to free via existing pidfile only"
  stop_pidfile "$LOG_DIR/fox.pid" || true
  sleep 0.5
fi

roslaunch foxglove_bridge foxglove_bridge.launch \
  port:="${FOXGLOVE_PORT}" \
  address:="127.0.0.1" \
  topic_whitelist:="['.*']" \
  param_whitelist:="['.*']" \
  service_whitelist:="['']" \
  capabilities:="[clientPublish,parameters,parametersSubscribe,assets,connectionGraph,services]" \
  asset_uri_allowlist:="['^package://(?:\\w+/)*\\w+\\.(?:dae|fbx|glb|gltf|jpeg|jpg|mtl|obj|png|stl|tif|tiff|urdf|webp|xacro)$']" \
  >"$LOG_DIR/fox.log" 2>&1 &
echo $! >"$LOG_DIR/fox.pid"
sleep 1.5

# Advertise check
python3 - <<PY
import json, time
try:
    import websocket
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "websocket-client", "-q"])
    import websocket

ws = websocket.create_connection(
    "ws://127.0.0.1:${FOXGLOVE_PORT}",
    timeout=5,
    subprotocols=["foxglove.websocket.v1"],
)
topics = set()
for _ in range(15):
    ws.settimeout(2)
    msg = ws.recv()
    if isinstance(msg, bytes):
        continue
    data = json.loads(msg)
    if data.get("op") == "advertise":
        for c in data.get("channels") or []:
            topics.add(c.get("topic"))
        break
ws.close()
need = {
    "/remote/b2/odom",
    "/joint_states",
    "/tf",
    "/remote/b2/path",
    "/remote/arm/slave_joint_states",
}
missing = sorted(need - topics)
print("[verified-viz] foxglove topics:", sorted(topics))
if missing:
    raise SystemExit("FAIL missing foxglove topics: " + ",".join(missing))
print("[verified-viz] foxglove advertise OK")
PY

# ---- Lichtblick ----
if [[ ! -x /usr/bin/xgc2-lichtblick-web ]]; then
  echo "[verified-viz] WARN: xgc2-lichtblick-web missing" >&2
else
  if ! ss -ltn | grep -q ':8080'; then
    /usr/bin/xgc2-lichtblick-web >"$LOG_DIR/lb.log" 2>&1 &
    echo $! >"$LOG_DIR/lb.pid"
    touch "$LOG_DIR/started_lichtblick"
    sleep 1
  fi
fi

LAYOUT="$ROOT/layouts/b2_sim_3d.json"
echo ""
echo "=============================================="
echo " VERIFIED DATA PATH OK"
echo "=============================================="
echo " TF: odom -> b2_description -> legs + R5a"
echo " Topics recovered + foxglove advertise OK"
echo " robot_description: package://b2arx_description (meshes via ROS_PACKAGE_PATH)"
echo ""
echo " Open Lichtblick:"
echo "   http://127.0.0.1:8080/?ds=foxglove-websocket&ds.url=ws%3A%2F%2F127.0.0.1%3A8080%2Fws"
echo ""
echo " THEN in UI (required once if empty layout):"
echo "   1) Add panel → 3D"
echo "   2) Fixed / follow frame: b2_description (or odom)"
echo "   3) Layers → add URDF → source param /robot_description"
echo "   4) Enable topic /remote/b2/path"
echo "   Layout template: $LAYOUT  (Layouts → Import if available)"
echo ""
echo " Logs: $LOG_DIR"
echo " Ctrl-C stops this stack"
echo "=============================================="

# stay up
while true; do
  sleep 5
  if ! kill -0 "$(cat "$LOG_DIR/sim.pid")" 2>/dev/null; then
    echo "[verified-viz] sim died" >&2
    tail -20 "$LOG_DIR/sim.log" >&2 || true
    exit 3
  fi
  if ! kill -0 "$(cat "$LOG_DIR/ground.pid")" 2>/dev/null; then
    echo "[verified-viz] ground died" >&2
    exit 3
  fi
done
