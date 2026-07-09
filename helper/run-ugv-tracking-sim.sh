#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: helper/run-ugv-tracking-sim.sh [options] [-- ROSLAUNCH_ARG:=VALUE ...]

Starts the single Scout UGV trajectory-tracking simulation inside the ROS1
development container. Gazebo GUI and RViz are enabled by default.

Options:
  --container NAME       Container name. Default: xgc2-ros1-ugv-tracking
  --reuse-container      Reuse the existing container instead of recreating it.
  --reset-container      Recreate the container before launch. Default.
  --keep-container       Keep the container after roslaunch exits.
  --network MODE         Docker network mode. Default: bridge
  --ros-master-uri URI   ROS master URI. Default: http://127.0.0.1:11312
  --gazebo-master-uri URI
                         Gazebo master URI. Default: http://127.0.0.1:11346
  --build                Build the source overlay before launch.
  --skip-build           Do not auto-build when /ros1_ws/devel is missing.
  --speed MPS            reference_default_line_speed. Default: 1.0
  --radius M             reference_default_radius. Default: 3.0
  --trajectory-type ID   reference_default_type. Default: 1 (circle)
  --duration SEC         reference_default_duration. Default: 120.0
  --random-targets       Replace the analytic reference with periodic random targets.
  --replan-period SEC    Random-target replanning period. Default: 5.0
  --random-range M       Sample random x/y targets in [-M, M]. Default: 5.0
  --random-seed N        Random-target seed; 0 uses a random seed. Default: 0
  --no-gui               Disable Gazebo GUI.
  --no-rviz              Disable RViz.
  --dry-run              Print the container launch command.
  -h, --help             Show this help.
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
container_name="${XGC2_ROS1_CONTAINER:-xgc2-ros1-ugv-tracking}"
reset_container=1
cleanup_container=1
docker_network="${XGC2_DOCKER_NETWORK:-bridge}"
ros_master_uri="${XGC2_UGV_ROS_MASTER_URI:-${XGC2_ROS_MASTER_URI:-http://127.0.0.1:11312}}"
gazebo_master_uri="${XGC2_UGV_GAZEBO_MASTER_URI:-${XGC2_GAZEBO_MASTER_URI:-http://127.0.0.1:11346}}"
build_mode="auto"
speed="1.0"
radius="3.0"
trajectory_type="1"
duration="120.0"
random_targets=0
replan_period="5.0"
random_range="5.0"
random_seed="0"
gui="true"
rviz="true"
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
    --speed)
      speed="${2:?--speed requires a value}"
      shift 2
      ;;
    --radius)
      radius="${2:?--radius requires a value}"
      shift 2
      ;;
    --trajectory-type)
      trajectory_type="${2:?--trajectory-type requires a value}"
      shift 2
      ;;
    --duration)
      duration="${2:?--duration requires a value}"
      shift 2
      ;;
    --random-targets)
      random_targets=1
      shift
      ;;
    --replan-period)
      replan_period="${2:?--replan-period requires a value}"
      shift 2
      ;;
    --random-range)
      random_range="${2:?--random-range requires a value}"
      shift 2
      ;;
    --random-seed)
      random_seed="${2:?--random-seed requires a value}"
      shift 2
      ;;
    --no-gui)
      gui="false"
      shift
      ;;
    --no-rviz)
      rviz="false"
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
  "gui:=${gui}"
  "rviz:=${rviz}"
  "reference_default_type:=${trajectory_type}"
  "reference_default_duration:=${duration}"
  "reference_default_radius:=${radius}"
  "reference_default_line_speed:=${speed}"
)
if [[ "${random_targets}" == "1" ]]; then
  launch_args+=(
    "ugv_yaw:=0.0"
    "reference_default_enabled:=false"
    "target_replanner_enabled:=true"
    "target_replanner_random_targets:=true"
    "target_replanner_period:=${replan_period}"
    "target_replanner_random_min_x:=-${random_range}"
    "target_replanner_random_max_x:=${random_range}"
    "target_replanner_random_min_y:=-${random_range}"
    "target_replanner_random_max_y:=${random_range}"
    "target_replanner_random_seed:=${random_seed}"
  )
fi
launch_args+=("${extra_roslaunch_args[@]}")
launch_command="$(printf '%q ' roslaunch gazebo_sim_examples scout_ugv1_nmpc_tracking.launch "${launch_args[@]}")"

container_command="
  set -euo pipefail
  if [[ -r /etc/profile.d/xgc-ros1.sh ]]; then . /etc/profile.d/xgc-ros1.sh; else . /opt/ros/noetic/setup.bash; fi
  if [[ -r /ros1_ws/devel/setup.bash ]]; then . /ros1_ws/devel/setup.bash; fi
  cd /xgc2-devops
  exec ${launch_command}
"

if [[ "${dry_run}" == "1" ]]; then
  printf '%q ' "${script_dir}/start-ros1-container.sh" "${start_args[@]}"
  printf '\n'
  if [[ "${build_mode}" == "always" || "${build_mode}" == "auto" ]]; then
    printf '%q ' "${script_dir}/build-ros1-overlay.sh" --container "${container_name}"
    printf '\n'
  fi
  printf 'docker exec %q bash -lc %q\n' "${container_name}" "${container_command}"
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

docker_exec_args=(docker exec)
if [[ -t 0 && -t 1 ]]; then
  docker_exec_args+=(-it)
fi
docker_exec_args+=("${container_name}" bash -lc "${container_command}")
"${docker_exec_args[@]}"
