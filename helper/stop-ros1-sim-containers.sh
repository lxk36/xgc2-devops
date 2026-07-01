#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: helper/stop-ros1-sim-containers.sh [options] [CONTAINER ...]

Stops and removes XGC2 ROS1 helper containers. By default it removes the known
development and single-robot simulation containers only.

Options:
  --all-xgc2-ros1   Also remove every container whose name starts with xgc2-ros1-
  --dry-run         Print containers that would be removed.
  -h, --help        Show this help.
USAGE
}

all_prefix=0
dry_run=0
containers=(
  xgc2-ros1-uav-tracking
  xgc2-ros1-ugv-tracking
  xgc2-ros1-build
  xgc2-ros1-dev
)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all-xgc2-ros1)
      all_prefix=1
      shift
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      containers+=("$@")
      break
      ;;
    -*)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      containers+=("$1")
      shift
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 127
fi

if [[ "${all_prefix}" == "1" ]]; then
  while read -r name; do
    [[ -n "${name}" ]] && containers+=("${name}")
  done < <(docker ps -a --format '{{.Names}}' | grep '^xgc2-ros1-' || true)
fi

mapfile -t containers < <(printf '%s\n' "${containers[@]}" | awk 'NF && !seen[$0]++')

existing=()
for name in "${containers[@]}"; do
  if docker inspect "${name}" >/dev/null 2>&1; then
    existing+=("${name}")
  fi
done

if [[ "${#existing[@]}" -eq 0 ]]; then
  echo "No XGC2 ROS1 helper containers found."
  exit 0
fi

if [[ "${dry_run}" == "1" ]]; then
  printf '%s\n' "${existing[@]}"
  exit 0
fi

docker rm -f "${existing[@]}" >/dev/null
printf 'Removed %d container(s): %s\n' "${#existing[@]}" "${existing[*]}"
