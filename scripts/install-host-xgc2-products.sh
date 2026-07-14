#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/install-host-xgc2-products.sh [options]

Installs active XGC2 product packages on the Ubuntu host. The default profile
contains simulation, controller, perception, compatible planner algorithms,
and their ROS integration packages. Hardware drivers and host-tuning services
are not selected. Package names come from each product's `apt.install`
metadata, so obsolete binaries that remain in the APT repository are not
selected.

Known-broken product releases are also withheld until a corrected package is
published.

Options:
  --distribution DIST  Override the host Ubuntu codename.
  --profile PROFILE    Package profile: simulation-algorithms or all.
                       Default: simulation-algorithms
  --exclude PACKAGE    Exclude one product package. May be repeated.
  --yes                Pass --yes to apt-get. Without it, APT asks for review.
  --dry-run            Run APT dependency resolution without changing the host.
  --print-packages     Print the selected package names and exit.
  -h, --help           Show this help.

Examples:
  scripts/install-host-xgc2-products.sh --dry-run
  scripts/install-host-xgc2-products.sh
  scripts/install-host-xgc2-products.sh --yes
  scripts/install-host-xgc2-products.sh --profile all
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

distribution=""
profile="simulation-algorithms"
assume_yes=0
dry_run=0
print_packages=0
excludes=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --distribution)
      distribution="${2:?--distribution requires a value}"
      shift 2
      ;;
    --profile)
      profile="${2:?--profile requires a value}"
      shift 2
      ;;
    --exclude)
      excludes+=("${2:?--exclude requires a package}")
      shift 2
      ;;
    --yes)
      assume_yes=1
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

if [[ "${profile}" != "simulation-algorithms" && "${profile}" != "all" ]]; then
  echo "--profile must be simulation-algorithms or all" >&2
  exit 2
fi

if [[ ! -r /etc/os-release ]]; then
  echo "cannot detect the host operating system: /etc/os-release is missing" >&2
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
  echo "unsupported host operating system: ${ID:-unknown}; Ubuntu is required" >&2
  exit 1
fi

if [[ -z "${distribution}" ]]; then
  distribution="${VERSION_CODENAME:-}"
fi
if [[ -z "${distribution}" ]]; then
  echo "cannot detect the Ubuntu codename; pass --distribution explicitly" >&2
  exit 1
fi

architecture="$(dpkg --print-architecture)"
if [[ "${architecture}" != "amd64" && "${architecture}" != "arm64" ]]; then
  echo "unsupported host architecture: ${architecture}" >&2
  exit 1
fi

if [[ ! -r /etc/apt/sources.list.d/xgc2.list ]]; then
  echo "XGC2 APT source is not configured: /etc/apt/sources.list.d/xgc2.list" >&2
  exit 1
fi

work_dir="${repo_root}/.work"
mkdir -p "${work_dir}"
catalog_path="$(mktemp "${work_dir}/xgc2-host-products.XXXXXX.json")"
trap 'rm -f "${catalog_path}"' EXIT

(
  cd "${repo_root}"
  python3 scripts/collect-products.py --root . --output "${catalog_path}" >/dev/null
)

mapfile -t selected_packages < <(
  python3 - "${catalog_path}" "${distribution}" "${profile}" <<'PY'
import json
import sys

catalog_path, distribution, profile = sys.argv[1:4]
with open(catalog_path, "r", encoding="utf-8") as handle:
    catalog = json.load(handle)

safe_source_prefixes = (
    "products/ros1/controller/",
    "products/ros1/perception/",
    "products/ros1/planner/",
    "products/ros1/simulator/",
)
safe_integration_products = {
    "xgc2-ros-msgs",
    "xgc2-ros1-adapters",
    "xgc2-ros1-utils",
    "xgc2-runtime-sync",
}

packages = set()
for product in catalog.get("products", []):
    lifecycle = product.get("lifecycle")
    if isinstance(lifecycle, dict) and lifecycle.get("deprecated") is True:
        continue

    product_id = str(product.get("id", ""))
    product_version = str(product.get("version", ""))
    if product_id == "xgc2-planner" and product_version == "1.1.0-10":
        print(
            "warning: withholding xgc2-planner 1.1.0-10 because its planner-common "
            "package conflicts with ros-noetic-view-controller-msgs",
            file=sys.stderr,
        )
        continue

    if profile == "simulation-algorithms":
        source = str(product.get("_source", ""))
        if not source.startswith(safe_source_prefixes) and product_id not in safe_integration_products:
            continue

    apt = product.get("apt")
    if not isinstance(apt, dict):
        continue

    distributions = {
        item.strip()
        for item in str(apt.get("distribution", "")).split(",")
        if item.strip()
    }
    if distribution not in distributions:
        continue

    package_distributions = apt.get("package_distributions") or {}
    for package in apt.get("install") or []:
        package = str(package).strip()
        scoped_distributions = package_distributions.get(package)
        if scoped_distributions and distribution not in scoped_distributions:
            continue
        if package:
            packages.add(package)

for package in sorted(packages):
    print(package)
PY
)

declare -A excluded=()
for package in "${excludes[@]}"; do
  excluded["${package}"]=1
done

packages=()
for package in "${selected_packages[@]}"; do
  [[ -z "${excluded[${package}]+x}" ]] || continue
  packages+=("${package}")
done

if [[ ${#packages[@]} -eq 0 ]]; then
  echo "no active XGC2 product packages support ${distribution}" >&2
  exit 1
fi

if [[ "${print_packages}" == "1" ]]; then
  printf '%s\n' "${packages[@]}"
  exit 0
fi

printf 'Selected %d XGC2 packages from profile %s for Ubuntu %s/%s.\n' \
  "${#packages[@]}" "${profile}" "${distribution}" "${architecture}"
printf '  %s\n' "${packages[@]}"

apt_prefix=()
if [[ ${EUID} -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "root privileges are required and sudo is not installed" >&2
    exit 1
  fi
  apt_prefix=(sudo)
fi

if [[ "${dry_run}" == "1" ]]; then
  apt-get --simulate install --no-install-recommends "${packages[@]}"
  exit 0
fi

"${apt_prefix[@]}" apt-get update \
  --allow-releaseinfo-change-origin \
  --allow-releaseinfo-change-label

missing_packages=()
for package in "${packages[@]}"; do
  if ! apt-cache show "${package}" >/dev/null 2>&1; then
    missing_packages+=("${package}")
  fi
done
if [[ ${#missing_packages[@]} -gt 0 ]]; then
  printf 'packages unavailable for %s/%s after apt-get update:\n' \
    "${distribution}" "${architecture}" >&2
  printf '  %s\n' "${missing_packages[@]}" >&2
  exit 1
fi

apt_flags=(--no-install-recommends)
if [[ "${assume_yes}" == "1" ]]; then
  apt_flags+=(--yes)
fi

"${apt_prefix[@]}" env DEBIAN_FRONTEND=noninteractive \
  apt-get install "${apt_flags[@]}" "${packages[@]}"

printf '\nInstalled XGC2 product versions:\n'
dpkg-query -W -f='${binary:Package}\t${Version}\n' "${packages[@]}" \
  2>/dev/null | sort || true
