#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: helper/run-fs150-qgc-sim.sh [options] [-- ROSLAUNCH_ARG:=VALUE ...]

Starts the FS150 PX4/Gazebo SITL inside the ROS1 development container for
QGroundControl inspection only. It does not publish takeoff, mode, tracking, or
offboard commands.

Options:
  --container NAME       Container name. Default: xgc2-ros1-fs150-qgc
  --reuse-container      Reuse the existing container instead of recreating it.
  --reset-container      Recreate the container before launch. Default.
  --keep-container       Keep the container after roslaunch exits.
  --network MODE         Docker network mode. Default: host
  --ros-master-uri URI   ROS master URI. Default: http://127.0.0.1:11311
  --gazebo-master-uri URI
                         Gazebo master URI. Default: http://127.0.0.1:11345
  --build                Build the source overlay before launch.
  --skip-build           Do not auto-build when /ros1_ws/devel is missing.
  --id ID                PX4 instance ID. Default: 3, mapping to MAV_SYS_ID 4.
  --model-name NAME      Gazebo model name. Default: fs150_<ID>
  --no-gui               Disable Gazebo GUI.
  --paused               Start Gazebo paused.
  --interactive          Run PX4 without daemonizing so the PX4 shell is visible.
  --dry-run              Print the container launch command.
  -h, --help             Show this help.

QGroundControl:
  Default host-network launch exposes QGC UDP on 127.0.0.1:14550 and MAVLink TCP
  on 127.0.0.1:(4560 + ID). With the default ID=3, manual TCP is 127.0.0.1:4563.
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
container_name="${XGC2_FS150_QGC_CONTAINER:-xgc2-ros1-fs150-qgc}"
reset_container=1
cleanup_container=1
docker_network="${XGC2_DOCKER_NETWORK:-host}"
ros_master_uri="${XGC2_FS150_ROS_MASTER_URI:-${XGC2_ROS_MASTER_URI:-http://127.0.0.1:11311}}"
gazebo_master_uri="${XGC2_FS150_GAZEBO_MASTER_URI:-${XGC2_GAZEBO_MASTER_URI:-http://127.0.0.1:11345}}"
build_mode="auto"
px4_id="3"
model_name=""
gui="true"
paused="false"
interactive="false"
dry_run=0
extra_roslaunch_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --container)
      container_name="${2:?--container requires a name}"
      shift 2
      ;;
    --reset-container)
      reset_container=1
      shift
      ;;
    --reuse-container)
      reset_container=0
      cleanup_container=0
      shift
      ;;
    --keep-container)
      cleanup_container=0
      shift
      ;;
    --network)
      docker_network="${2:?--network requires a mode}"
      shift 2
      ;;
    --ros-master-uri)
      ros_master_uri="${2:?--ros-master-uri requires a URI}"
      shift 2
      ;;
    --gazebo-master-uri)
      gazebo_master_uri="${2:?--gazebo-master-uri requires a URI}"
      shift 2
      ;;
    --build)
      build_mode="always"
      shift
      ;;
    --skip-build)
      build_mode="never"
      shift
      ;;
    --id)
      px4_id="${2:?--id requires a value}"
      shift 2
      ;;
    --model-name)
      model_name="${2:?--model-name requires a value}"
      shift 2
      ;;
    --no-gui)
      gui="false"
      shift
      ;;
    --paused)
      paused="true"
      shift
      ;;
    --interactive)
      interactive="true"
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
      extra_roslaunch_args=("$@")
      break
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! [[ "${px4_id}" =~ ^[0-9]+$ ]]; then
  echo "--id must be a non-negative integer: ${px4_id}" >&2
  exit 2
fi

case "${container_name}" in
  xgc2-ros1-uav-tracking|xgc2-ros1-ugv-tracking)
    echo "refusing to use tracking container name for FS150 QGC sim: ${container_name}" >&2
    echo "choose a dedicated name with --container or XGC2_FS150_QGC_CONTAINER" >&2
    exit 2
    ;;
esac

if [[ -z "${model_name}" ]]; then
  model_name="fs150_${px4_id}"
fi

mav_sys_id="$((px4_id + 1))"
mavlink_tcp_port="$((4560 + px4_id))"
sdk_udp_port="$((14540 + px4_id))"
mavros_local_port="$((14540 + px4_id))"
mavros_remote_port="$((14580 + px4_id))"

start_args=(
  --name "${container_name}"
  --detach
  --network "${docker_network}"
  --ros-master-uri "${ros_master_uri}"
  --gazebo-master-uri "${gazebo_master_uri}"
)
if [[ "${reset_container}" == "1" ]]; then
  start_args+=(--reset)
else
  start_args+=(--reuse)
fi

launch_args=(
  "ID:=${px4_id}"
  "model_name:=${model_name}"
  "gui:=${gui}"
  "paused:=${paused}"
  "interactive:=${interactive}"
)
launch_args+=("${extra_roslaunch_args[@]}")
launch_command="$(printf '%q ' roslaunch gazebo_sim_fs150_sitl fs150.launch "${launch_args[@]}")"

container_command="
  set -euo pipefail
  if [[ -r /etc/profile.d/xgc-ros1.sh ]]; then . /etc/profile.d/xgc-ros1.sh; else . /opt/ros/noetic/setup.bash; fi
  if [[ -r /ros1_ws/devel/setup.bash ]]; then . /ros1_ws/devel/setup.bash; fi
  cd /xgc2-devops
  exec ${launch_command}
"

print_qgc_hint() {
  cat <<EOF
FS150 QGC inspection launch:
  container: ${container_name}
  docker network: ${docker_network}
  PX4 ID: ${px4_id} -> MAV_SYS_ID: ${mav_sys_id}
  Gazebo model: ${model_name}
  QGC UDP: 127.0.0.1:14550
  QGC manual TCP: 127.0.0.1:${mavlink_tcp_port}
  MAVROS FCU URL: udp://:${mavros_local_port}@localhost:${mavros_remote_port}
  SDK UDP port: ${sdk_udp_port}
EOF
  if [[ "${docker_network}" != "host" ]]; then
    cat <<'EOF'
  note: non-host Docker networking may require connecting QGC to the container IP
        or adding explicit Docker port publishing outside this helper.
EOF
  fi
}

if [[ "${dry_run}" == "1" ]]; then
  printf '%q ' "${script_dir}/start-ros1-container.sh" "${start_args[@]}"
  printf '\n'
  if [[ "${build_mode}" == "always" || "${build_mode}" == "auto" ]]; then
    printf '%q ' "${script_dir}/build-ros1-overlay.sh" --container "${container_name}"
    printf '\n'
  fi
  printf 'docker exec %q bash -lc %q\n' "${container_name}" "${container_command}"
  print_qgc_hint
  exit 0
fi

cleanup() {
  local status=$?
  if [[ "${cleanup_container}" == "1" ]]; then
    docker rm -f "${container_name}" >/dev/null 2>&1 || true
  fi
  exit "${status}"
}
trap cleanup EXIT

"${script_dir}/start-ros1-container.sh" "${start_args[@]}"

if [[ "${build_mode}" == "always" ]] ||
   [[ "${build_mode}" == "auto" &&
      "$(docker exec "${container_name}" bash -lc 'test -r /ros1_ws/devel/setup.bash && echo yes || echo no')" != "yes" ]]; then
  "${script_dir}/build-ros1-overlay.sh" --container "${container_name}"
fi

print_qgc_hint

docker_exec_args=(docker exec)
if [[ -t 0 && -t 1 ]]; then
  docker_exec_args+=(-it)
fi
docker_exec_args+=("${container_name}" bash -lc "${container_command}")
"${docker_exec_args[@]}"
