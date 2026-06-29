#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/docker-ci-local.sh [--image IMAGE] [-- COMMAND...]

Runs the repository validation steps inside the same ROS1 runtime image used for
local development. This is the pre-GitHub-CI path for xgc2-devops.

Environment:
  XGC2_ROS1_RUNTIME_IMAGE  Runtime image.

Default validation:
  - install PyYAML/jsonschema inside the container
  - bash -n shell scripts
  - collect product metadata
  - check ROS1 product/dev boundaries
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
image="${XGC2_ROS1_RUNTIME_IMAGE:-crpi-pest1z0t9z6yd8c6.cn-beijing.personal.cr.aliyuncs.com/xgc2-app-store/xgc-ros1-runtime:latest}"
host_uid="$(id -u)"
host_gid="$(id -g)"
custom_command=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)
      image="${2:?--image requires an image}"
      shift 2
      ;;
    --)
      shift
      custom_command=("$@")
      break
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

if [[ "${#custom_command[@]}" -gt 0 ]]; then
  command_text="$(printf '%q ' "${custom_command[@]}")"
else
  command_text='
set -euo pipefail
apt-get update
apt-get install -y --no-install-recommends python3-pip
python3 -m pip install --no-cache-dir PyYAML jsonschema
bash -n scripts/*.sh products/ros1_dev/scripts/*.sh
python3 scripts/collect-products.py --root . --output .work/products-container-ci.json
python3 scripts/check-ros1-dev-boundaries.py --root .
'
fi

docker run --rm \
  --network host \
  -e "HOST_UID=${host_uid}" \
  -e "HOST_GID=${host_gid}" \
  -v "${repo_root}:/workspace" \
  -w /workspace \
  "${image}" \
  bash -lc "
    set -euo pipefail
    mkdir -p .work
    ${command_text}
    if [[ \"\${HOST_UID}\" != \"0\" ]] && [[ \"\${HOST_GID}\" != \"0\" ]]; then
      chown -R \"\${HOST_UID}:\${HOST_GID}\" .work 2>/dev/null || true
    fi
  "
