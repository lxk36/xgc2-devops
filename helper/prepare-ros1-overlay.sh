#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: helper/prepare-ros1-overlay.sh [options]

Creates symlinks from /ros1_ws/src to the real high-frequency ROS1 source
packages. It never copies source code.

Options:
  --ros-ws DIR          Host catkin workspace. Default: .work/ros1_ws
  --overlay-file FILE   Overlay list. Default: helper/ros1-source-overlay.txt
  --container-root DIR  Repo mount path inside the container. Default: /xgc2-devops
  -h, --help            Show this help.
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
ros_ws="${XGC2_ROS1_WS:-${repo_root}/.work/ros1_ws}"
overlay_file="${XGC2_ROS1_OVERLAY_FILE:-${script_dir}/ros1-source-overlay.txt}"
container_root="${XGC2_DEVOPS_CONTAINER_ROOT:-/xgc2-devops}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ros-ws)
      ros_ws="${2:?--ros-ws requires a directory}"
      shift 2
      ;;
    --overlay-file)
      overlay_file="${2:?--overlay-file requires a file}"
      shift 2
      ;;
    --container-root)
      container_root="${2:?--container-root requires a directory}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

repo_root="$(realpath "${repo_root}")"
mkdir -p "${ros_ws}/src"
ros_ws="$(realpath "${ros_ws}")"
overlay_file="$(realpath "${overlay_file}")"

while read -r kind path extra; do
  if [[ -z "${kind:-}" || "${kind}" == \#* ]]; then
    continue
  fi
  if [[ -n "${extra:-}" ]]; then
    echo "invalid overlay entry: ${kind} ${path} ${extra}" >&2
    exit 2
  fi
  case "${kind}" in
    cmake)
      continue
      ;;
    catkin)
      source_dir="${repo_root}/${path}"
      if [[ ! -f "${source_dir}/package.xml" ]]; then
        echo "catkin package missing package.xml: ${source_dir}" >&2
        exit 2
      fi
      link_path="${ros_ws}/src/$(basename "${path}")"
      if [[ -e "${link_path}" && ! -L "${link_path}" ]]; then
        echo "refusing to replace non-symlink: ${link_path}" >&2
        exit 2
      fi
      ln -sfnT "${container_root}/${path}" "${link_path}"
      ;;
    *)
      echo "unknown overlay kind: ${kind}" >&2
      exit 2
      ;;
  esac
done < "${overlay_file}"

echo "Prepared ROS1 source overlay: ${ros_ws}/src"
