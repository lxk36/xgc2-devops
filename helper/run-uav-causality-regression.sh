#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: helper/run-uav-causality-regression.sh [options]

Runs repeated UAV NMPC tracking simulations and records high-rate causality
signals: raw VRPN, Gazebo ground truth, estimator posterior/innovation,
MAVROS attitude target, and NMPC debug samples.

Options:
  --runs N              Number of runs. Default: 10
  --duration SEC        Rosbag duration per run. Default: 90
  --container NAME      Container name. Default: xgc2-ros1-uav-causality
  --output-dir DIR      Host output directory. Default: .work/uav-causality/<timestamp>
  --build               Build the ROS1 overlay before the first run.
  --jobs N              Build parallelism. Default: 8
  --backend NAME        tracking_backend. Default: nmpc_attitude_rate
  --controller-config-file FILE
                        px4_multirotor_controller YAML config override
  --speed MPS           reference_line_speed. Default: 1.0
  --radius M            reference_radius. Default: 3.0
  --height M            reference_height/takeoff_altitude. Default: 3.0
  --z-amplitude M       reference_z_amplitude. Default: 0.0
  --analytic-type ID    reference_analytic_type. Default: 9
  --torus-omega VALUE   reference_torus_omega. Default: 0.3
  --torus-scale VALUE   reference_torus_scale. Default: 2.0
  --fs150-base-sdf FILE FS150 source SDF for renderer. Default: package SDF
  --fs150-sdf FILE      FS150 SDF passed to PX4/Gazebo. Default: renderer output
  --fs150-render-sdf BOOL
                        Render fs150_sdf from fs150_base_sdf. Default: true
  --yaw                 Enable yaw control. Default: false
  --gui                 Start Gazebo GUI. Default: false
  --rviz                Start RViz. Default: false
  -h, --help            Show this help.
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

runs=10
duration=90
container_name="xgc2-ros1-uav-causality"
output_dir="${repo_root}/.work/uav-causality/$(date +%Y%m%d-%H%M%S)"
build=0
jobs=8
backend="nmpc_attitude_rate"
controller_config_file=""
speed="1.0"
radius="3.0"
height="3.0"
z_amplitude="0.0"
analytic_type="9"
torus_omega="0.3"
torus_scale="2.0"
fs150_base_sdf=""
fs150_sdf=""
fs150_render_sdf="true"
yaw="false"
gui="false"
rviz="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runs)
      runs="${2:?--runs requires a value}"
      shift 2
      ;;
    --duration)
      duration="${2:?--duration requires a value}"
      shift 2
      ;;
    --container)
      container_name="${2:?--container requires a value}"
      shift 2
      ;;
    --output-dir)
      output_dir="${2:?--output-dir requires a value}"
      shift 2
      ;;
    --build)
      build=1
      shift
      ;;
    --jobs)
      jobs="${2:?--jobs requires a value}"
      shift 2
      ;;
    --backend)
      backend="${2:?--backend requires a value}"
      shift 2
      ;;
    --controller-config-file)
      controller_config_file="${2:?--controller-config-file requires a value}"
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
    --fs150-base-sdf)
      fs150_base_sdf="${2:?--fs150-base-sdf requires a value}"
      shift 2
      ;;
    --fs150-sdf)
      fs150_sdf="${2:?--fs150-sdf requires a value}"
      shift 2
      ;;
    --fs150-render-sdf)
      fs150_render_sdf="${2:?--fs150-render-sdf requires a value}"
      shift 2
      ;;
    --yaw)
      yaw="true"
      shift
      ;;
    --gui)
      gui="true"
      shift
      ;;
    --rviz)
      rviz="true"
      shift
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

mkdir -p "${output_dir}"
output_dir="$(realpath "${output_dir}")"

source_env='if [[ -r /etc/profile.d/xgc-ros1.sh ]]; then . /etc/profile.d/xgc-ros1.sh; else . /opt/ros/noetic/setup.bash; fi; if [[ -r /ros1_ws/devel/setup.bash ]]; then . /ros1_ws/devel/setup.bash; fi'

wait_for_topic() {
  local topic="$1"
  local timeout="$2"
  local start
  start="$(date +%s)"
  while true; do
    if docker exec "${container_name}" bash -lc "${source_env}; rostopic list 2>/dev/null | grep -qx '${topic}'"; then
      return 0
    fi
    if (( "$(date +%s)" - start >= timeout )); then
      return 1
    fi
    sleep 1
  done
}

cleanup_container() {
  docker rm -f "${container_name}" >/dev/null 2>&1 || true
}

trap cleanup_container EXIT

"${script_dir}/start-ros1-container.sh" --name "${container_name}" --detach --reset
if [[ "${build}" == "1" ]]; then
  "${script_dir}/build-ros1-overlay.sh" --container "${container_name}" --jobs "${jobs}"
fi
cleanup_container

summary_file="${output_dir}/summary.tsv"
printf 'run\tclassification\tmax_pos_innov\tmax_est_vrpn_resid\tmax_est_gazebo_resid\tmax_vrpn_gazebo_resid\tmax_nmpc_pos\tmax_nmpc_vel\tmax_att_err\tmax_thrust_dir_err\tmax_ref_att_step\tmax_body_rate\tmax_body_rate_step\tmax_alpha\tmax_ref_alpha\tmax_alpha_ref_delta\tmax_imu_raw_w\tmin_z_after25\tfirst_events\n' > "${summary_file}"

for run in $(seq -w 1 "${runs}"); do
  run_dir="${output_dir}/run-${run}"
  mkdir -p "${run_dir}"
  echo "[causality] run ${run}/${runs}: ${run_dir}"

  "${script_dir}/start-ros1-container.sh" --name "${container_name}" --detach --reset

  launch_cmd=$(printf '%q ' \
    roslaunch /xgc2-devops/helper/fs150_uav1_tracking_with_visualization.launch \
    "gui:=${gui}" \
    "rviz:=${rviz}" \
    "tracking_backend:=${backend}" \
    "takeoff_altitude:=${height}" \
    "enable_yaw_control:=${yaw}" \
    "reference_radius:=${radius}" \
    "reference_line_speed:=${speed}" \
    "reference_height:=${height}" \
    "reference_z_amplitude:=${z_amplitude}" \
    "reference_analytic_type:=${analytic_type}" \
    "reference_torus_omega:=${torus_omega}" \
    "reference_torus_scale:=${torus_scale}" \
    "fs150_render_sdf:=${fs150_render_sdf}")

  if [[ -n "${controller_config_file}" ]]; then
    launch_cmd+=" $(printf '%q' "controller_config_file:=${controller_config_file}")"
  fi
  if [[ -n "${fs150_base_sdf}" ]]; then
    launch_cmd+=" $(printf '%q' "fs150_base_sdf:=${fs150_base_sdf}")"
  fi
  if [[ -n "${fs150_sdf}" ]]; then
    launch_cmd+=" $(printf '%q' "fs150_sdf:=${fs150_sdf}")"
  fi

  docker exec -d "${container_name}" bash -lc "
    set -euo pipefail
    ${source_env}
    cd /xgc2-devops
    exec ${launch_cmd} > /xgc2-devops/${run_dir#${repo_root}/}/roslaunch.log 2>&1
  "

  wait_for_topic "/gazebo/model_states" 90 || {
    echo "[causality] run ${run}: /gazebo/model_states did not appear" | tee "${run_dir}/error.log"
    docker logs "${container_name}" > "${run_dir}/container.log" 2>&1 || true
    cleanup_container
    continue
  }
  wait_for_topic "/uav1/alg/state_estimator/state" 90 || {
    echo "[causality] run ${run}: estimator topic did not appear" | tee "${run_dir}/error.log"
    docker logs "${container_name}" > "${run_dir}/container.log" 2>&1 || true
    cleanup_container
    continue
  }

  bag_path="/xgc2-devops/${run_dir#${repo_root}/}/causality.bag"
  topics=(
    /gazebo/model_states
    /vrpn_client_node/uav1/pose
    /vrpn_client_node/uav1/twist
    /uav1/alg/state_estimator/state
    /uav1/alg/nmpc/debug_sample
    /uav1/mavros/setpoint_raw/attitude
    /uav1/mavros/local_position/pose
    /uav1/mavros/local_position/velocity_local
    /uav1/mavros/imu/data_raw
    /uav1/mavros/imu/data
  )
  record_cmd=$(printf '%q ' timeout --signal=INT "${duration}" rosbag record --lz4 -O "${bag_path}" "${topics[@]}")
  docker exec "${container_name}" bash -lc "
    set -euo pipefail
    ${source_env}
    cd /xgc2-devops
    ${record_cmd}
  " > "${run_dir}/rosbag.log" 2>&1 &
  record_pid=$!

  sleep 3
  docker exec -d "${container_name}" bash -lc "
    set -euo pipefail
    ${source_env}
    cd /xgc2-devops
    exec rosrun gazebo_sim_examples uav_auto_takeoff_track.py --ns uav1 --height ${height} \
      > /xgc2-devops/${run_dir#${repo_root}/}/auto.log 2>&1
  " >/dev/null

  wait "${record_pid}" || true
  docker logs "${container_name}" > "${run_dir}/container.log" 2>&1 || true

  if [[ -f "${run_dir}/causality.bag" ]]; then
    docker exec "${container_name}" bash -lc "
      set -euo pipefail
      ${source_env}
      /xgc2-devops/helper/analyze-uav-causality-bag.py \
        /xgc2-devops/${run_dir#${repo_root}/}/causality.bag \
        --output-dir /xgc2-devops/${run_dir#${repo_root}/}/analysis \
        > /xgc2-devops/${run_dir#${repo_root}/}/analysis.log
    "
    python3 - <<PY >> "${summary_file}"
import json
from pathlib import Path
run = "${run}"
path = Path("${run_dir}") / "analysis" / "analysis.json"
data = json.loads(path.read_text())
first = ",".join(f"{e['time']:.2f}:{e['name']}" for e in data["events"][:5])
print(
    f"{run}\\t{data['classification']}\\t{data['max_pos_innovation']:.3f}\\t"
    f"{data['max_est_vrpn_pos_residual']:.3f}\\t{data['max_est_gazebo_pos_residual']:.3f}\\t"
    f"{data['max_vrpn_gazebo_pos_residual']:.3f}\\t{data['max_nmpc_pos_err']:.3f}\\t"
    f"{data['max_nmpc_vel_err']:.3f}\\t{data['max_nmpc_attitude_error']:.3f}\\t"
    f"{data['max_nmpc_thrust_direction_error']:.3f}\\t"
    f"{data['max_reference_attitude_step']:.3f}\\t"
    f"{data['max_nmpc_body_rate_command']:.3f}\\t"
    f"{data['max_nmpc_body_rate_step']:.3f}\\t{data['max_nmpc_alpha_command']:.3f}\\t"
    f"{data['max_reference_alpha']:.3f}\\t{data['max_alpha_reference_delta']:.3f}\\t"
    f"{data['max_imu_raw_angular_rate']:.3f}\\t{data['min_state_z_after_25s']:.3f}\\t{first}"
)
PY
  else
    echo "[causality] run ${run}: bag was not created" | tee "${run_dir}/error.log"
  fi

  cleanup_container
done

cat "${summary_file}"
