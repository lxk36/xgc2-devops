#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: helper/update-image.sh [options]

Pulls the latest Aliyun ROS1 runtime image, optionally installs the mature
development APT baseline in a temporary container, and commits it to a local
runtime tag.

This script does not run docker build. It only pulls the upstream runtime image
and uses apt inside a temporary container for products that are intentionally
classified as installable development baseline packages.

Options:
  --image IMAGE         Source image to pull.
  --tag TAG             Local tag to assign. Default: source basename.
  --apt-file FILE       APT package list. Default: helper/ros1-dev-apt-install.txt
  --apt-package NAME    Add one APT package to the install baseline.
  --no-apt              Only pull and tag the source image.
  --no-pull             Skip docker pull and use the local source image.
  -h, --help            Show this help.

Environment:
  XGC2_ROS1_RUNTIME_IMAGE       Source image.
  XGC2_ROS1_RUNTIME_LOCAL_TAG   Local tag alias.
  XGC2_ROS1_DEV_APT_FILE        APT package list.
  XGC2_ROS1_DEV_APT_PACKAGES    Space-separated extra APT packages.
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
image="${XGC2_ROS1_RUNTIME_IMAGE:-crpi-pest1z0t9z6yd8c6.cn-beijing.personal.cr.aliyuncs.com/xgc2-app-store/xgc-ros1-runtime:latest}"
local_tag="${XGC2_ROS1_RUNTIME_LOCAL_TAG:-}"
apt_file="${XGC2_ROS1_DEV_APT_FILE:-${script_dir}/ros1-dev-apt-install.txt}"
pull=1
install_apt=1
apt_packages=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)
      image="${2:?--image requires an image}"
      shift 2
      ;;
    --tag)
      local_tag="${2:?--tag requires a tag}"
      shift 2
      ;;
    --apt-file)
      apt_file="${2:?--apt-file requires a file}"
      shift 2
      ;;
    --apt-package)
      apt_packages+=("${2:?--apt-package requires a package name}")
      shift 2
      ;;
    --no-apt)
      install_apt=0
      shift
      ;;
    --no-pull)
      pull=0
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

if [[ -z "${local_tag}" ]]; then
  local_tag="${image##*/}"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 127
fi

if [[ "${pull}" == "1" ]]; then
  docker pull "${image}"
fi

if [[ "${install_apt}" == "1" ]]; then
  if [[ -f "${apt_file}" ]]; then
    while IFS= read -r line; do
      line="${line%%#*}"
      line="$(printf '%s' "${line}" | xargs)"
      if [[ -n "${line}" ]]; then
        apt_packages+=("${line}")
      fi
    done < "${apt_file}"
  else
    echo "APT package list does not exist: ${apt_file}" >&2
    exit 2
  fi
  if [[ -n "${XGC2_ROS1_DEV_APT_PACKAGES:-}" ]]; then
    read -r -a extra_apt_packages <<< "${XGC2_ROS1_DEV_APT_PACKAGES}"
    apt_packages+=("${extra_apt_packages[@]}")
  fi
fi

if [[ "${install_apt}" == "0" || "${#apt_packages[@]}" == "0" ]]; then
  docker tag "${image}" "${local_tag}"
  docker image inspect "${local_tag}" --format 'Tagged image: {{.RepoTags}} {{.Id}} {{.Created}}'
  exit 0
fi

unique_apt_packages=()
declare -A seen_apt_packages=()
for package in "${apt_packages[@]}"; do
  if [[ -z "${seen_apt_packages[${package}]+x}" ]]; then
    unique_apt_packages+=("${package}")
    seen_apt_packages["${package}"]=1
  fi
done

container="xgc2-update-image-$RANDOM-$$"
cleanup() {
  docker rm -f "${container}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Source image: ${image}"
echo "Local tag:    ${local_tag}"
echo "APT baseline:"
printf '  %s\n' "${unique_apt_packages[@]}"

docker create --name "${container}" --network host "${image}" sleep infinity >/dev/null
docker start "${container}" >/dev/null

docker exec -e DEBIAN_FRONTEND=noninteractive "${container}" bash -lc '
  set -euo pipefail
  if [[ -f /etc/apt/sources.list ]]; then
    sed -i \
      -e "s|http://archive.ubuntu.com/ubuntu|http://mirrors.aliyun.com/ubuntu|g" \
      -e "s|http://security.ubuntu.com/ubuntu|http://mirrors.aliyun.com/ubuntu|g" \
      /etc/apt/sources.list
  fi
  apt-get update
  apt-get install -y --no-install-recommends ca-certificates curl gnupg
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://xgc2.apt.xiaokang.ink/xgc2-archive-keyring.gpg \
    -o /tmp/xgc2-archive-keyring.gpg
  gpg --show-keys --with-fingerprint --with-colons /tmp/xgc2-archive-keyring.gpg 2>&1 \
    | grep -q "^fpr:\+2A8E11B36F56D307ADF626D85E5FDC30979EA43F:$"
  cat /tmp/xgc2-archive-keyring.gpg > /etc/apt/keyrings/xgc2-archive-keyring.gpg
  echo "deb [signed-by=/etc/apt/keyrings/xgc2-archive-keyring.gpg] https://xgc2.apt.xiaokang.ink focal main" \
    > /etc/apt/sources.list.d/xgc2.list
  apt-get update
  apt-get install -y --no-install-recommends "$@"
  rm -rf /var/lib/apt/lists/*
' _ "${unique_apt_packages[@]}"

docker commit \
  --change "ENV DISABLE_ROS1_EOL_WARNINGS=1" \
  "${container}" "${local_tag}" >/dev/null
docker image inspect "${local_tag}" --format 'Tagged image: {{.RepoTags}} {{.Id}} {{.Created}}'
