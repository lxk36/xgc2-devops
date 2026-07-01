#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: helper/start-ros1-container.sh [options] [-- COMMAND...]

Starts or enters the ROS1 development container. The container uses the
prepared local runtime image, mounts the source tree, and exposes X11, GPU, and
devices. It does not install APT packages or build any Docker image.
The source overlay is linked into /ros1_ws/src before the container starts.

An existing container with the same name is recreated by default so stale
mounts, GPU flags, or environment variables do not survive across runs.

Options:
  --image IMAGE       Runtime image. Default: xgc-ros1-runtime:latest
  --name NAME         Container name. Default: xgc2-ros1-dev
  --workspace DIR     Host workspace root mounted at /workspace.
  --ros-ws DIR        Host catkin workspace mounted at /ros1_ws.
  --overlay-file FILE Source overlay list. Default: helper/ros1-source-overlay.txt
  --network MODE      Docker network mode. Default: bridge
  --ros-master-uri URI
                      ROS master URI injected into the container.
                      Default: http://127.0.0.1:11311
  --gazebo-master-uri URI
                      Gazebo master URI injected into the container.
                      Default: http://127.0.0.1:11345
  --no-overlay-links  Do not update /ros1_ws/src symlinks before start.
  --no-gpu            Do not pass --gpus all.
  --detach            Start the container but do not enter a shell.
  --reuse             Reuse an existing container instead of recreating it.
  --reset             Explicitly recreate an existing container.
  --dry-run           Print the docker command and exit.
  -h, --help          Show this help.

Environment:
  XGC2_ROS1_RUNTIME_LOCAL_TAG  Runtime image override.
  XGC2_ROS1_CONTAINER          Container name override.
  XGC2_WORKSPACE_ROOT          Host workspace root override.
  XGC2_ROS1_WS                 Host catkin workspace override.
  XGC2_ROS1_OVERLAY_FILE       Source overlay file override.
  XGC2_DOCKER_NETWORK          Docker network mode override.
  XGC2_ROS_MASTER_URI          ROS master URI override.
  XGC2_GAZEBO_MASTER_URI       Gazebo master URI override.
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
workspace_root="$(git -C "${repo_root}" rev-parse --show-superproject-working-tree 2>/dev/null || true)"
if [[ -z "${workspace_root}" ]]; then
  workspace_root="${repo_root}"
fi

image="${XGC2_ROS1_RUNTIME_LOCAL_TAG:-xgc-ros1-runtime:latest}"
container_name="${XGC2_ROS1_CONTAINER:-xgc2-ros1-dev}"
workspace_root="${XGC2_WORKSPACE_ROOT:-${workspace_root}}"
ros_ws="${XGC2_ROS1_WS:-${repo_root}/.work/ros1_ws}"
overlay_file="${XGC2_ROS1_OVERLAY_FILE:-${script_dir}/ros1-source-overlay.txt}"
docker_network="${XGC2_DOCKER_NETWORK:-bridge}"
ros_master_uri="${XGC2_ROS_MASTER_URI:-http://127.0.0.1:11311}"
gazebo_master_uri="${XGC2_GAZEBO_MASTER_URI:-http://127.0.0.1:11345}"
link_overlay=1
use_gpu=1
detach=0
recreate=1
dry_run=0
command=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)
      image="${2:?--image requires an image}"
      shift 2
      ;;
    --name)
      container_name="${2:?--name requires a name}"
      shift 2
      ;;
    --workspace)
      workspace_root="${2:?--workspace requires a directory}"
      shift 2
      ;;
    --ros-ws)
      ros_ws="${2:?--ros-ws requires a directory}"
      shift 2
      ;;
    --overlay-file)
      overlay_file="${2:?--overlay-file requires a file}"
      shift 2
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
    --no-overlay-links)
      link_overlay=0
      shift
      ;;
    --no-gpu)
      use_gpu=0
      shift
      ;;
    --detach)
      detach=1
      shift
      ;;
    --reuse)
      recreate=0
      shift
      ;;
    --reset)
      recreate=1
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
      command=("$@")
      break
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

workspace_root="$(realpath "${workspace_root}")"
repo_root="$(realpath "${repo_root}")"
mkdir -p "${ros_ws}/src"
ros_ws="$(realpath "${ros_ws}")"
overlay_file="$(realpath "${overlay_file}")"

if [[ ! -d "${workspace_root}" ]]; then
  echo "workspace does not exist: ${workspace_root}" >&2
  exit 2
fi
if [[ ! -d "${repo_root}" ]]; then
  echo "repo root does not exist: ${repo_root}" >&2
  exit 2
fi
if [[ "${link_overlay}" == "1" ]]; then
  "${script_dir}/prepare-ros1-overlay.sh" --ros-ws "${ros_ws}" --overlay-file "${overlay_file}"
fi

ros_profile='if [[ -r /etc/profile.d/xgc-ros1.sh ]]; then . /etc/profile.d/xgc-ros1.sh; elif [[ -r /opt/ros/noetic/setup.bash ]]; then . /opt/ros/noetic/setup.bash; fi; if [[ -r /ros1_ws/devel/setup.bash ]]; then . /ros1_ws/devel/setup.bash; fi'
keepalive_command="${ros_profile}; sleep infinity"

run_args=(
  docker run -d
  --name "${container_name}"
  --privileged
  --network "${docker_network}"
  --ipc host
  --restart unless-stopped
  -e "DISABLE_ROS1_EOL_WARNINGS=1"
  -e "DISPLAY=${DISPLAY:-}"
  -e "ROS_MASTER_URI=${ros_master_uri}"
  -e "GAZEBO_MASTER_URI=${gazebo_master_uri}"
  -e "NVIDIA_VISIBLE_DEVICES=${NVIDIA_VISIBLE_DEVICES:-all}"
  -e "NVIDIA_DRIVER_CAPABILITIES=${NVIDIA_DRIVER_CAPABILITIES:-all}"
  -e "XGC2_DEVOPS_ROOT=/xgc2-devops"
  -e "XGC2_WORKSPACE_ROOT=/workspace"
  -e "XGC2_ROS1_WS=/ros1_ws"
  -v "/dev:/dev"
  -v "/tmp/.X11-unix:/tmp/.X11-unix:rw"
  -v "${workspace_root}:/workspace:rw"
  -v "${repo_root}:/xgc2-devops:rw"
  -v "${ros_ws}:/ros1_ws:rw"
  -w "/xgc2-devops"
)

xauthority="${XAUTHORITY:-}"
if [[ -z "${xauthority}" && -r "${HOME}/.Xauthority" ]]; then
  xauthority="${HOME}/.Xauthority"
fi
if [[ -n "${xauthority}" && -r "${xauthority}" ]]; then
  run_args+=(-e "XAUTHORITY=/tmp/.xgc2-docker.xauth" -v "${xauthority}:/tmp/.xgc2-docker.xauth:ro")
fi

if [[ "${use_gpu}" == "1" ]]; then
  run_args+=(--gpus all)
fi

run_args+=("${image}" bash -lc "${keepalive_command}")

print_command() {
  printf '%q ' "$@"
  printf '\n'
}

container_exists() {
  docker inspect "${container_name}" >/dev/null 2>&1
}

container_running() {
  [[ "$(docker inspect -f '{{.State.Running}}' "${container_name}" 2>/dev/null || true)" == "true" ]]
}

if [[ "${dry_run}" == "1" ]]; then
  print_command "${run_args[@]}"
  exit 0
fi

if [[ "${recreate}" == "1" ]] && container_exists; then
  docker rm -f "${container_name}" >/dev/null
fi

if container_exists; then
  if ! container_running; then
    docker start "${container_name}" >/dev/null
  fi
else
  "${run_args[@]}" >/dev/null
fi

if [[ "${detach}" == "1" ]]; then
  echo "ROS1 development container is running: ${container_name}"
  exit 0
fi

if [[ "${#command[@]}" -gt 0 ]]; then
  exec docker exec -it "${container_name}" bash -lc "
    set -euo pipefail
    ${ros_profile}
    cd /xgc2-devops
    $(printf '%q ' "${command[@]}")
  "
fi

exec docker exec -it "${container_name}" bash -lc "
  ${ros_profile}
  cd /xgc2-devops
  exec bash
"
