#!/usr/bin/env bash
set -euo pipefail

pattern='^(ros-noetic-xgc2-|ros-jazzy-xgc2-|libxgc2-|xgc2-|ros-noetic-swarm-sync-sim$|ros-noetic-livox-ros-driver2?$)'

mapfile -t packages < <(
  dpkg-query -W -f='${binary:Package}\n' 2>/dev/null \
    | grep -E "${pattern}" \
    | sort -u || true
)

cat <<'NOTE'
# Review this before running. It targets XGC2-published ROS/system packages and
# their base libraries, not every package installed by apt on the host.
NOTE

if [[ ${#packages[@]} -eq 0 ]]; then
  cat <<'NO_PACKAGES'
# No installed XGC2 packages matched the cleanup pattern.
NO_PACKAGES
  exit 0
fi

printf 'sudo apt-get purge -y'
for package in "${packages[@]}"; do
  printf ' \\\n  %q' "${package}"
done
printf '\n'

cat <<'CLEANUP'
sudo apt-get autoremove --purge -y
sudo rm -f /etc/apt/sources.list.d/xgc2.list /etc/apt/keyrings/xgc2-archive-keyring.gpg
sudo apt-get update
CLEANUP
