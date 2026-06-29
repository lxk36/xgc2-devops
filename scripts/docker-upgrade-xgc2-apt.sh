#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/docker-upgrade-xgc2-apt.sh [options]

Installs or upgrades XGC2 APT artifacts inside a Docker container. By default it
starts a disposable runtime container and installs every active APT binary
package whose product metadata supports the selected distribution.

Options:
  --container NAME        Upgrade an existing running container with docker exec.
  --image IMAGE          Runtime image for disposable-container mode.
  --distribution DIST    APT distribution to select from product metadata.
                          Default: focal
  --package-source NAME  Metadata field to install: packages or install.
                          Default: packages
  --only-extra           Install only packages passed through --extra or
                         XGC2_APT_EXTRA_PACKAGES.
  --exclude PACKAGE      Remove a package from the selected install set.
  --exclude-file PATH    Read excluded packages from a newline-delimited file.
  --extra PACKAGE        Add an explicit package to the install set.
  --upgrade-only         Only upgrade already-installed packages.
  --skip-unavailable     Skip selected packages that are not in the configured
                         APT indexes, and print them to stderr.
  --dry-run              Print the final package set and exit.
  --print-packages       Print one final package per line and exit.
  -h, --help             Show this help.

Environment:
  XGC2_ROS1_RUNTIME_IMAGE  Default disposable runtime image.
  XGC2_APT_DISTRIBUTION    Default distribution.
  XGC2_APT_PACKAGE_SOURCE  Default metadata field: packages or install.
  XGC2_APT_EXCLUDE_PACKAGES
  XGC2_APT_EXTRA_PACKAGES

Examples:
  scripts/docker-upgrade-xgc2-apt.sh --dry-run
  scripts/docker-upgrade-xgc2-apt.sh --container xgc2-ros1-dev-gui
  scripts/docker-upgrade-xgc2-apt.sh --package-source install
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

image="${XGC2_ROS1_RUNTIME_IMAGE:-crpi-pest1z0t9z6yd8c6.cn-beijing.personal.cr.aliyuncs.com/xgc2-app-store/xgc-ros1-runtime:latest}"
distribution="${XGC2_APT_DISTRIBUTION:-focal}"
package_source="${XGC2_APT_PACKAGE_SOURCE:-packages}"
container_name=""
dry_run=0
print_packages=0
upgrade_only=0
skip_unavailable=0
only_extra=0
excludes=()
extras=()

if [[ -n "${XGC2_APT_EXCLUDE_PACKAGES:-}" ]]; then
  # shellcheck disable=SC2206
  excludes=(${XGC2_APT_EXCLUDE_PACKAGES})
fi
if [[ -n "${XGC2_APT_EXTRA_PACKAGES:-}" ]]; then
  # shellcheck disable=SC2206
  extras=(${XGC2_APT_EXTRA_PACKAGES})
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --container)
      container_name="${2:?--container requires a name}"
      shift 2
      ;;
    --image)
      image="${2:?--image requires an image}"
      shift 2
      ;;
    --distribution)
      distribution="${2:?--distribution requires a value}"
      shift 2
      ;;
    --package-source)
      package_source="${2:?--package-source requires packages or install}"
      shift 2
      ;;
    --only-extra)
      only_extra=1
      shift
      ;;
    --exclude)
      excludes+=("${2:?--exclude requires a package}")
      shift 2
      ;;
    --exclude-file)
      exclude_file="${2:?--exclude-file requires a path}"
      if [[ ! -f "${exclude_file}" ]]; then
        echo "exclude file not found: ${exclude_file}" >&2
        exit 2
      fi
      while IFS= read -r line; do
        line="${line%%#*}"
        line="${line//[[:space:]]/}"
        [[ -n "${line}" ]] && excludes+=("${line}")
      done < "${exclude_file}"
      shift 2
      ;;
    --extra)
      extras+=("${2:?--extra requires a package}")
      shift 2
      ;;
    --upgrade-only)
      upgrade_only=1
      shift
      ;;
    --skip-unavailable)
      skip_unavailable=1
      shift
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    --print-packages)
      print_packages=1
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

if [[ "${package_source}" != "packages" && "${package_source}" != "install" ]]; then
  echo "--package-source must be either packages or install" >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 127
fi

work_dir="${repo_root}/.work"
mkdir -p "${work_dir}"
catalog_path=""
if [[ "${only_extra}" != "1" ]]; then
  catalog_path="$(mktemp "${work_dir}/xgc2-products.XXXXXX.json")"
  trap 'rm -f "${catalog_path}"' EXIT

  (
    cd "${repo_root}"
    python3 scripts/collect-products.py --root . --output "${catalog_path#${repo_root}/}" >/dev/null
  )
fi

selected_packages=()
if [[ "${only_extra}" != "1" ]]; then
  mapfile -t selected_packages < <(
    python3 - "${catalog_path}" "${distribution}" "${package_source}" <<'PY'
import json
import sys

catalog_path, distribution, package_source = sys.argv[1:4]

with open(catalog_path, "r", encoding="utf-8") as handle:
    catalog = json.load(handle)

for product in catalog.get("products", []):
    lifecycle = product.get("lifecycle")
    if isinstance(lifecycle, dict) and lifecycle.get("deprecated") is True:
        continue

    apt = product.get("apt")
    if not isinstance(apt, dict):
        continue

    distributions = [
        item.strip()
        for item in str(apt.get("distribution", "")).split(",")
        if item.strip()
    ]
    if distribution not in distributions:
        continue

    packages = apt.get(package_source)
    if isinstance(packages, list):
        for package in packages:
            print(str(package).strip())
PY
  )
fi

declare -A excluded=()
for package in "${excludes[@]}"; do
  [[ -n "${package}" ]] && excluded["${package}"]=1
done

declare -A seen=()
packages=()
for package in "${selected_packages[@]}" "${extras[@]}"; do
  [[ -n "${package}" ]] || continue
  [[ -z "${excluded[${package}]+x}" ]] || continue
  [[ -z "${seen[${package}]+x}" ]] || continue
  seen["${package}"]=1
  packages+=("${package}")
done

if [[ "${#packages[@]}" -eq 0 ]]; then
  echo "no packages selected for distribution ${distribution}" >&2
  exit 1
fi

if [[ "${dry_run}" == "1" || "${print_packages}" == "1" ]]; then
  printf '%s\n' "${packages[@]}"
  exit 0
fi

apt_install_flags=(-y --no-install-recommends)
if [[ "${upgrade_only}" == "1" ]]; then
  apt_install_flags+=(--only-upgrade)
fi

container_script='
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
packages=("$@")

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg

curl -fsSL https://xgc2.apt.xiaokang.ink/xgc2-archive-keyring.gpg \
  -o /tmp/xgc2-archive-keyring.gpg
gpg --show-keys --with-fingerprint --with-colons /tmp/xgc2-archive-keyring.gpg 2>&1 \
  | grep -q "^fpr:\+2A8E11B36F56D307ADF626D85E5FDC30979EA43F:$"

install -d -m 0755 /etc/apt/keyrings
cat /tmp/xgc2-archive-keyring.gpg > /etc/apt/keyrings/xgc2-archive-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/xgc2-archive-keyring.gpg] https://xgc2.apt.xiaokang.ink ${XGC2_APT_DISTRIBUTION} main" \
  > /etc/apt/sources.list.d/xgc2.list

apt-get update
if [[ "${XGC2_APT_SKIP_UNAVAILABLE}" == "1" ]]; then
  available_packages=()
  missing_packages=()
  for package in "${packages[@]}"; do
    if apt-cache show "${package}" >/dev/null 2>&1; then
      available_packages+=("${package}")
    else
      missing_packages+=("${package}")
    fi
  done
  if [[ "${#missing_packages[@]}" -gt 0 ]]; then
    printf "warning: skipping unavailable APT package: %s\n" "${missing_packages[@]}" >&2
  fi
  if [[ "${#available_packages[@]}" -eq 0 ]]; then
    echo "no selected packages are available in the configured APT indexes" >&2
    exit 1
  fi
  packages=("${available_packages[@]}")
fi
apt-get install "${XGC2_APT_INSTALL_FLAGS[@]}" "${packages[@]}"
dpkg-query -W -f='\''${binary:Package}\t${Version}\n'\'' "${packages[@]}" 2>/dev/null || true
'

install_flags_serialized="$(printf '%q ' "${apt_install_flags[@]}")"

if [[ -n "${container_name}" ]]; then
  docker exec \
    -e "XGC2_APT_DISTRIBUTION=${distribution}" \
    -e "XGC2_APT_INSTALL_FLAGS=${install_flags_serialized}" \
    -e "XGC2_APT_SKIP_UNAVAILABLE=${skip_unavailable}" \
    "${container_name}" \
    bash -lc "XGC2_APT_INSTALL_FLAGS=(${install_flags_serialized}); ${container_script}" \
    bash "${packages[@]}"
else
  docker run --rm \
    --network host \
    -e "XGC2_APT_DISTRIBUTION=${distribution}" \
    -e "XGC2_APT_INSTALL_FLAGS=${install_flags_serialized}" \
    -e "XGC2_APT_SKIP_UNAVAILABLE=${skip_unavailable}" \
    "${image}" \
    bash -lc "XGC2_APT_INSTALL_FLAGS=(${install_flags_serialized}); ${container_script}" \
    bash "${packages[@]}"
fi
