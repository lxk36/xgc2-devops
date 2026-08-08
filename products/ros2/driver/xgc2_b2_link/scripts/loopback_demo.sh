#!/usr/bin/env bash
# Start G4 ground then G3 dry-run over TCP. Ctrl-C stops both.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
PORT="${PORT:-7448}"
RID="${ROBOT_ID:-b2-01}"

python3 -m xgc2_b2_link.ground_peer \
  --robot-id "$RID" --transport tcp --tcp-role server --tcp-port "$PORT" --send-echo &
G4=$!
sleep 0.5
python3 -m xgc2_b2_link.forwarder_node \
  --robot-id "$RID" --transport tcp --tcp-role client --tcp-port "$PORT" --dry-run-no-ros
kill "$G4" 2>/dev/null || true
wait "$G4" 2>/dev/null || true
