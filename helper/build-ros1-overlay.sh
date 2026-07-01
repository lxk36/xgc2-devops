#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: helper/build-ros1-overlay.sh [options]

Builds source overlays inside the ROS1 development container. CMake libraries
are installed into /ros1_ws/devel first; catkin packages are then built with
/ros1_ws/devel before /opt/ros/noetic in CMAKE_PREFIX_PATH.

Options:
  --container NAME      Container name. Default: xgc2-ros1-dev
  --ros-ws DIR          Container catkin workspace. Default: /ros1_ws
  --overlay-file FILE   Container overlay list. Default: /xgc2-devops/helper/ros1-source-overlay.txt
  --jobs N              Build parallelism. Default: nproc
  -h, --help            Show this help.
USAGE
}

container_name="${XGC2_ROS1_CONTAINER:-xgc2-ros1-dev}"
container_ros_ws="${XGC2_ROS1_CONTAINER_WS:-/ros1_ws}"
container_overlay_file="${XGC2_ROS1_CONTAINER_OVERLAY_FILE:-/xgc2-devops/helper/ros1-source-overlay.txt}"
jobs=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --container)
      container_name="${2:?--container requires a name}"
      shift 2
      ;;
    --ros-ws)
      container_ros_ws="${2:?--ros-ws requires a directory}"
      shift 2
      ;;
    --overlay-file)
      container_overlay_file="${2:?--overlay-file requires a file}"
      shift 2
      ;;
    --jobs)
      jobs="${2:?--jobs requires a number}"
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

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 127
fi

if [[ "$(docker inspect -f '{{.State.Running}}' "${container_name}" 2>/dev/null || true)" != "true" ]]; then
  echo "container is not running: ${container_name}" >&2
  exit 2
fi

docker exec \
  -e "XGC2_ROS1_CONTAINER_WS=${container_ros_ws}" \
  -e "XGC2_ROS1_CONTAINER_OVERLAY_FILE=${container_overlay_file}" \
  -e "XGC2_ROS1_BUILD_JOBS=${jobs}" \
  -e "MPLBACKEND=Agg" \
  "${container_name}" bash -lc '
    set -euo pipefail
    ros_ws="${XGC2_ROS1_CONTAINER_WS}"
    overlay_file="${XGC2_ROS1_CONTAINER_OVERLAY_FILE}"
    jobs="${XGC2_ROS1_BUILD_JOBS}"
    if [[ -z "${jobs}" ]]; then
      jobs="$(nproc)"
    fi
    export MPLBACKEND=Agg

    if [[ -r /etc/profile.d/xgc-ros1.sh ]]; then
      . /etc/profile.d/xgc-ros1.sh
    elif [[ -r /opt/ros/noetic/setup.bash ]]; then
      . /opt/ros/noetic/setup.bash
    fi

    mkdir -p "${ros_ws}/build" "${ros_ws}/devel" "${ros_ws}/src"

    while read -r kind path extra; do
      if [[ -z "${kind:-}" || "${kind}" == \#* ]]; then
        continue
      fi
      if [[ -n "${extra:-}" ]]; then
        echo "invalid overlay entry: ${kind} ${path} ${extra}" >&2
        exit 2
      fi
      if [[ "${kind}" != "cmake" ]]; then
        continue
      fi
      source_dir="/xgc2-devops/${path}"
      build_dir="${ros_ws}/build/_xgc2_common/$(basename "${path}")"
      cmake -S "${source_dir}" -B "${build_dir}" \
        -DCMAKE_BUILD_TYPE=RelWithDebInfo \
        -DCMAKE_INSTALL_PREFIX="${ros_ws}/devel" \
        -DCMAKE_PREFIX_PATH="${ros_ws}/devel;/opt/ros/noetic"
      cmake --build "${build_dir}" --target install -- -j"${jobs}"
    done < "${overlay_file}"

    export CMAKE_PREFIX_PATH="${ros_ws}/devel:${CMAKE_PREFIX_PATH:-}"
    cd "${ros_ws}"
    catkin_make \
      -DCMAKE_BUILD_TYPE=RelWithDebInfo \
      -DCMAKE_PREFIX_PATH="${ros_ws}/devel;/opt/ros/noetic" \
      -j"${jobs}"

    . "${ros_ws}/devel/setup.bash"
    echo "Overlay CMAKE_PREFIX_PATH=${CMAKE_PREFIX_PATH}"
    echo "Overlay ROS_PACKAGE_PATH=${ROS_PACKAGE_PATH}"
  '
