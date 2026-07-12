#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: helper/run-uav-tracking-sim.sh [options] [-- ROSLAUNCH_ARG:=VALUE ...]

Starts the single FS150 UAV trajectory-tracking simulation inside the ROS1
development container. Gazebo GUI and RViz are enabled by default.

Options:
  --container NAME       Container name. Default: xgc2-ros1-uav-tracking
  --reuse-container      Reuse the existing container instead of recreating it.
  --reset-container      Recreate the container before launch. Default.
  --keep-container       Keep the container after roslaunch exits.
  --network MODE         Docker network mode. Default: bridge
  --ros-master-uri URI   ROS master URI. Default: http://127.0.0.1:11311
  --gazebo-master-uri URI
                         Gazebo master URI. Default: http://127.0.0.1:11345
  --build                Build the source overlay before launch.
  --skip-build           Do not auto-build when /ros1_ws/devel is missing.
  --backend NAME         tracking_backend. Default: nmpc_attitude_rate
  --speed MPS            reference_line_speed. Default: 1.0
  --radius M             reference_radius. Default: 3.0
  --height M             reference_height/takeoff_altitude. Default: 3.0
  --z-amplitude M        reference_z_amplitude. Default: 0.0
  --analytic-type ID     reference_analytic_type. Default: 9 (torus-knot)
  --torus-omega VALUE    reference_torus_omega. Default: 0.3
  --torus-scale VALUE    reference_torus_scale. Default: 2.0
  --no-auto-command      Do not automatically publish takeoff/custom1 commands.
  --yaw                  Enable yaw control. Default: false
  --no-gui               Disable Gazebo GUI.
  --no-rviz              Disable RViz.
  --dry-run              Print the container launch command.
  -h, --help             Show this help.
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
container_name="${XGC2_ROS1_CONTAINER:-xgc2-ros1-uav-tracking}"
reset_container=1
cleanup_container=1
docker_network="${XGC2_DOCKER_NETWORK:-bridge}"
ros_master_uri="${XGC2_UAV_ROS_MASTER_URI:-${XGC2_ROS_MASTER_URI:-http://127.0.0.1:11311}}"
gazebo_master_uri="${XGC2_UAV_GAZEBO_MASTER_URI:-${XGC2_GAZEBO_MASTER_URI:-http://127.0.0.1:11345}}"
build_mode="auto"
backend="nmpc_attitude_rate"
speed="1.0"
radius="3.0"
height="3.0"
z_amplitude="0.0"
analytic_type="9"
torus_omega="0.3"
torus_scale="2.0"
auto_command=1
yaw="false"
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
    --backend)
      backend="${2:?--backend requires a value}"
      shift 2
      ;;
    --speed)
      speed="${2:?--speed requires a value}"
      shift 2
      ;;
    --radius)
      radius="${2:?--radius requires a value}"
      shift 2
      ;;
    --height)
      height="${2:?--height requires a value}"
      shift 2
      ;;
    --z-amplitude)
      z_amplitude="${2:?--z-amplitude requires a value}"
      shift 2
      ;;
    --analytic-type)
      analytic_type="${2:?--analytic-type requires a value}"
      shift 2
      ;;
    --torus-omega)
      torus_omega="${2:?--torus-omega requires a value}"
      shift 2
      ;;
    --torus-scale)
      torus_scale="${2:?--torus-scale requires a value}"
      shift 2
      ;;
    --no-auto-command)
      auto_command=0
      shift
      ;;
    --yaw)
      yaw="true"
      shift
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
  "tracking_backend:=${backend}"
  "takeoff_altitude:=${height}"
  "enable_yaw_control:=${yaw}"
  "reference_radius:=${radius}"
  "reference_line_speed:=${speed}"
  "reference_height:=${height}"
  "reference_z_amplitude:=${z_amplitude}"
  "reference_analytic_type:=${analytic_type}"
  "reference_torus_omega:=${torus_omega}"
  "reference_torus_scale:=${torus_scale}"
)
launch_args+=("${extra_roslaunch_args[@]}")

launch_command="$(printf '%q ' roslaunch /xgc2-devops/helper/fs150_uav1_tracking_with_visualization.launch "${launch_args[@]}")"
auto_command_launch="$(printf '%q ' rosrun gazebo_sim_examples uav_auto_takeoff_track.py --ns uav1 --height "${height}")"
container_command="
  set -euo pipefail
  if [[ -r /etc/profile.d/xgc-ros1.sh ]]; then . /etc/profile.d/xgc-ros1.sh; else . /opt/ros/noetic/setup.bash; fi
  if [[ -r /ros1_ws/devel/setup.bash ]]; then . /ros1_ws/devel/setup.bash; fi
  cd /xgc2-devops
  exec ${launch_command}
"
auto_container_command="
  set -euo pipefail
  if [[ -r /etc/profile.d/xgc-ros1.sh ]]; then . /etc/profile.d/xgc-ros1.sh; else . /opt/ros/noetic/setup.bash; fi
  if [[ -r /ros1_ws/devel/setup.bash ]]; then . /ros1_ws/devel/setup.bash; fi
  cd /xgc2-devops
  exec ${auto_command_launch}
"

if [[ "${dry_run}" == "1" ]]; then
  printf '%q ' "${script_dir}/start-ros1-container.sh" "${start_args[@]}"
  printf '\n'
  if [[ "${build_mode}" == "always" || "${build_mode}" == "auto" ]]; then
    printf '%q ' "${script_dir}/build-ros1-overlay.sh" --container "${container_name}"
    printf '\n'
  fi
  printf 'docker exec %q bash -lc %q\n' "${container_name}" "${container_command}"
  if [[ "${auto_command}" == "1" ]]; then
    printf 'docker exec -d %q bash -lc %q\n' "${container_name}" "${auto_container_command}"
  fi
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

if [[ "${auto_command}" == "1" ]]; then
  docker exec -d "${container_name}" bash -lc "${auto_container_command}" >/dev/null
fi

docker_exec_args=(docker exec)
if [[ -t 0 && -t 1 ]]; then
  docker_exec_args+=(-it)
fi
docker_exec_args+=("${container_name}" bash -lc "${container_command}")
"${docker_exec_args[@]}"
