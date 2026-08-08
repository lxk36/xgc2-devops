#!/usr/bin/env bash
# One-shot: sim + ground recovery + RSP + foxglove + CORS layout server + lichtblick.
# Prints a single URL that should open with data source + layout (URDF layer).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
B2ARX="${B2ARX_ROOT:-/home/lxk/Dev/xgc2-vibe-coding/xgc2-devops/products/ros2/robot/b2arx_description}"
# rospack searches *parents* for packages; point at the folder that *contains* b2arx_description
B2ARX_PARENT="$(cd "$(dirname "$B2ARX")" && pwd)"
LOG_DIR="${LOG_DIR:-/tmp/xgc2_b2_one_shot}"
ROBOT_ID="${ROBOT_ID:-b2-sim}"
TCP_PORT="${TCP_PORT:-7448}"
FOXGLOVE_PORT="${FOXGLOVE_PORT:-8765}"
LAYOUT_PORT="${LAYOUT_PORT:-8091}"

mkdir -p "$LOG_DIR"
source /opt/ros/noetic/setup.bash
# After setup.bash (it resets ROS_PACKAGE_PATH)
export ROS_PACKAGE_PATH="${B2ARX_PARENT}:${ROS_PACKAGE_PATH:-}"
export PYTHONPATH="/opt/ros/noetic/lib/python3/dist-packages:${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

python3 - <<'PY'
import os, re, signal, subprocess
def kill_port(port: int) -> None:
    try:
        out = subprocess.check_output(["ss", "-ltnp"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return
    for line in out.splitlines():
        if f":{port}" not in line:
            continue
        for m in re.finditer(r"pid=(\d+)", line):
            try:
                os.kill(int(m.group(1)), signal.SIGTERM)
            except ProcessLookupError:
                pass
def kill_cmd_substr(substr: str) -> None:
    for line in subprocess.check_output(["ps", "-eo", "pid,cmd"], text=True).splitlines():
        if substr not in line:
            continue
        if "one_shot_viz" in line or "ps -eo" in line:
            continue
        try:
            os.kill(int(line.split(None, 1)[0]), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
for p in (8765, 7448, 8091):
    kill_port(p)
for s in (
    "xgc2_b2_link.sim_publisher",
    "xgc2_b2_link.ground_peer",
    "/opt/ros/noetic/lib/robot_state_publisher/robot_state_publisher",
    "/opt/ros/noetic/lib/foxglove_bridge/foxglove_bridge",
    "cors_static_server.py",
    "http.server 8091",
):
    kill_cmd_substr(s)
print("cleaned previous workers")
PY
sleep 1

if ! python3 -c "import rosgraph; rosgraph.Master('/p').getPid()" 2>/dev/null; then
  nohup roscore >"$LOG_DIR/roscore.log" 2>&1 &
  echo $! >"$LOG_DIR/roscore.pid"
  for _ in $(seq 1 40); do
    python3 -c "import rosgraph; rosgraph.Master('/p').getPid()" 2>/dev/null && break
    sleep 0.1
  done
fi

python3 - <<PY
import rospy
rospy.init_node("xgc2_set_rd", anonymous=True)
xml = open("${B2ARX}/urdf/b2arx_visual.urdf", encoding="utf-8").read()
assert "package://b2arx_description/" in xml
rospy.set_param("/robot_description", xml)
print("robot_description ok", len(xml))
print("rospack", __import__("subprocess").check_output(["rospack","find","b2arx_description"], text=True).strip())
PY

nohup python3 -m xgc2_b2_link.ground_peer \
  --robot-id "$ROBOT_ID" --transport tcp --tcp-role server --tcp-port "$TCP_PORT" \
  --publish-ros --print-hz 0 \
  >"$LOG_DIR/ground.log" 2>&1 & echo $! >"$LOG_DIR/ground.pid"
sleep 0.7
nohup python3 -m xgc2_b2_link.sim_publisher \
  --robot-id "$ROBOT_ID" --transport tcp --tcp-role client --tcp-port "$TCP_PORT" \
  >"$LOG_DIR/sim.log" 2>&1 & echo $! >"$LOG_DIR/sim.pid"
nohup /opt/ros/noetic/lib/robot_state_publisher/robot_state_publisher \
  >"$LOG_DIR/rsp.log" 2>&1 & echo $! >"$LOG_DIR/rsp.pid"
sleep 2

# hard checks
timeout 3 rostopic echo -n 1 /remote/b2/odom >/dev/null
timeout 3 rostopic echo -n 1 /joint_states >/dev/null
python3 - <<'PY'
import rospy, tf2_ros
rospy.init_node("tfok", anonymous=True)
b = tf2_ros.Buffer(cache_time=rospy.Duration(3))
tf2_ros.TransformListener(b)
rospy.sleep(0.8)
for a,c in [("odom","b2_description"),("b2_description","b2_description_FL_calf"),("b2_description","R5a_link3")]:
    b.lookup_transform(a,c,rospy.Time(0), rospy.Duration(2))
    print("TF OK", a, "->", c)
PY

nohup roslaunch foxglove_bridge foxglove_bridge.launch \
  port:="${FOXGLOVE_PORT}" address:="127.0.0.1" \
  topic_whitelist:="['.*']" param_whitelist:="['.*']" \
  capabilities:="[clientPublish,parameters,parametersSubscribe,assets,connectionGraph,services]" \
  >"$LOG_DIR/fox.log" 2>&1 & echo $! >"$LOG_DIR/fox.pid"
sleep 2

python3 - <<PY
import json, websocket
ws = websocket.create_connection(
    "ws://127.0.0.1:${FOXGLOVE_PORT}", timeout=5, subprotocols=["foxglove.websocket.v1"]
)
topics=set()
for _ in range(20):
    msg = ws.recv()
    if isinstance(msg, bytes):
        continue
    d = json.loads(msg)
    if d.get("op") == "advertise":
        topics |= {c.get("topic") for c in (d.get("channels") or [])}
        break
ws.send(json.dumps({"op":"getParameters","id":"1","parameterNames":["/robot_description"]}))
urdf_ok=False
for _ in range(15):
    msg=ws.recv()
    if isinstance(msg, bytes):
        continue
    d=json.loads(msg)
    if d.get("op")=="parameterValues":
        for p in d.get("parameters") or []:
            if p.get("name")=="/robot_description":
                v=p.get("value") or ""
                if not isinstance(v,str):
                    v=str(v)
                urdf_ok = "package://b2arx_description" in v and len(v)>1000
        break
ws.close()
need={"/remote/b2/odom","/joint_states","/tf","/remote/b2/path"}
miss=sorted(need-topics)
print("foxglove topics", sorted(topics))
assert not miss, miss
assert urdf_ok, "robot_description missing via foxglove"
print("FOX_OK")
PY

# CORS layout server
nohup python3 "$ROOT/scripts/cors_static_server.py" \
  --port "$LAYOUT_PORT" --directory "$ROOT/layouts" \
  >"$LOG_DIR/layout_http.log" 2>&1 & echo $! >"$LOG_DIR/layout_http.pid"
sleep 0.3
# CORS preflight check
curl -sS -D- -o /dev/null -H "Origin: http://127.0.0.1:8080" \
  "http://127.0.0.1:${LAYOUT_PORT}/b2_sim_3d.json" | tr -d '\r' | grep -i 'access-control-allow-origin' 

if ! ss -ltn | grep -q ':8080'; then
  nohup /usr/bin/xgc2-lichtblick-web >"$LOG_DIR/lb.log" 2>&1 & echo $! >"$LOG_DIR/lb.pid"
  sleep 1
fi

# Prefer the HTML trampoline (always expands to full ds=foxglove-websocket).
# Do NOT paste a truncated ds=foxglove- into the address bar — that skips
# Lichtblick auto-connect and yields "[followTf] No coordinate frames found".
LAUNCH_URL="http://127.0.0.1:${LAYOUT_PORT}/open_b2_sim.html"
URL="http://127.0.0.1:8080/?ds=foxglove-websocket&ds.url=ws%3A%2F%2F127.0.0.1%3A8080%2Fws&layoutUrl=http%3A%2F%2F127.0.0.1%3A${LAYOUT_PORT}%2Fb2_sim_3d.json"
# Short form also works: omit ds so index auto-connect injects the full id.
URL_SHORT="http://127.0.0.1:8080/?layoutUrl=http%3A%2F%2F127.0.0.1%3A${LAYOUT_PORT}%2Fb2_sim_3d.json"

# write open helper
cat >"$LOG_DIR/OPEN_ME.url" <<EOF
$LAUNCH_URL
EOF
printf '%s\n' "$URL" >"$LOG_DIR/OPEN_DIRECT.url"

# Best-effort open in a local browser (non-fatal if headless/no display)
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$LAUNCH_URL" >/dev/null 2>&1 || true
fi

echo ""
echo "============================================================"
echo " BACKEND VERIFIED (topics + TF + foxglove URDF param + CORS)"
echo "============================================================"
echo " Open (recommended trampoline — cannot truncate ds):"
echo "   $LAUNCH_URL"
echo " Or direct (ds MUST be full foxglove-websocket, not foxglove-):"
echo "   $URL"
echo " Or short (auto-connect fills ds):"
echo "   $URL_SHORT"
echo ""
echo " Console '[followTf] No coordinate frames found' = WebSocket DS"
echo " not connected (wrong/truncated ds). Backend TF is already live."
echo " 'render done twice' is a benign Lichtblick panel remount warning."
echo " Logs: $LOG_DIR"
echo "============================================================"

# keep alive
while true; do
  sleep 30
  kill -0 "$(cat "$LOG_DIR/sim.pid")" 2>/dev/null || { echo sim dead; exit 3; }
  kill -0 "$(cat "$LOG_DIR/ground.pid")" 2>/dev/null || { echo ground dead; exit 3; }
done
