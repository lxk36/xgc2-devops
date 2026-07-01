#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "${repo_root}"

if [[ -n "${SUBMODULE_SSH_KEY:-}" ]]; then
  install -m 0700 -d "${HOME}/.ssh"
  key_file="${HOME}/.ssh/xgc2-submodule-key"
  printf '%s\n' "${SUBMODULE_SSH_KEY}" > "${key_file}"
  chmod 0600 "${key_file}"
  ssh-keyscan github.com >> "${HOME}/.ssh/known_hosts"
  git config --global core.sshCommand \
    "ssh -i ${key_file} -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes"
elif [[ -n "${GH_TOKEN:-}" ]]; then
  git config --global \
    url."https://x-access-token:${GH_TOKEN}@github.com/".insteadOf git@github.com:
else
  git config --global url."https://github.com/".insteadOf git@github.com:
fi

submodules=(
  products/common/acados
  products/common/math
  products/common/mavlink-router
  products/common/state-machine
  products/common/tbb
  products/ros1/common/ros1-utils
  products/ros1/communication/runtime-sync
  products/ros1/driver/livox_ros_driver
  products/ros1/driver/livox_ros_driver2
  products/ros1/manager/linux-utils
  products/ros1/perception/detection
  products/ros1/perception/slam
  products/ros1/planner/planner
  products/ros1/simulator/convex_geometry
  products/ros1/simulator/gazebo-sim
  products/ros1/simulator/swarm-sync-sim
  products/ros1_dev
  products/ros2/simulator/px4-sitl-116
)

for submodule_path in "${submodules[@]}"; do
  if ! git ls-files -s "${submodule_path}" | grep -q '^160000 '; then
    echo "::warning title=Submodule not indexed::${submodule_path} is not a gitlink in this revision"
    continue
  fi
  if ! git submodule update --init --recursive --depth=1 "${submodule_path}"; then
    echo "::warning title=Submodule checkout failed::${submodule_path} could not be checked out; catalog will continue with available products"
  fi
done
