#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

test -f package.xml
test -f setup.py
test -f .xgc2/product.yml
test -f contract/zenoh_v1.yaml
test -f xgc2_b2_link/forwarder_node.py
test -f xgc2_b2_link/ground_peer.py
test -f xgc2_b2_link/sim_publisher.py
test -f xgc2_b2_link/transport.py
test -f xgc2_b2_link/codec.py

# Shared contract must stay versioned
grep -q 'version: 1' contract/zenoh_v1.yaml
grep -q 'up/odom' contract/zenoh_v1.yaml
grep -q 'up/joint_states' contract/zenoh_v1.yaml
grep -q 'up/power_summary' contract/zenoh_v1.yaml
grep -q 'down/cmd' contract/zenoh_v1.yaml

# High-level commands only by default (no cmd_vel as default allow)
if grep -E 'default_allow_types:.*cmd_vel' contract/zenoh_v1.yaml; then
  echo "cmd_vel must not be in default_allow_types" >&2
  exit 1
fi

python3 -m py_compile xgc2_b2_link/codec.py
python3 -m py_compile xgc2_b2_link/contract.py
python3 -m py_compile xgc2_b2_link/transport.py
python3 -m py_compile xgc2_b2_link/forwarder_node.py
python3 -m py_compile xgc2_b2_link/ground_peer.py
python3 -m py_compile xgc2_b2_link/sim_publisher.py
python3 -m py_compile xgc2_b2_link/sim_models.py
python3 -m py_compile xgc2_b2_link/ground_ros1.py

PYTHONPATH=. python3 -m pytest test/ -q

echo "compliance OK"
