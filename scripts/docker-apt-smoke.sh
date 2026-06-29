#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/docker-apt-smoke.sh <apt-package> [<apt-package> ...]

Installs XGC2 APT packages inside a disposable Docker container. This is the
development smoke-test path; do not install product packages directly on the
host workstation.

Environment:
  XGC2_APT_SMOKE_IMAGE     Docker image to use.
                           Default: crpi-pest1z0t9z6yd8c6.cn-beijing.personal.cr.aliyuncs.com/xgc2-app-store/xgc-ros1-runtime:latest
  XGC2_APT_DISTRIBUTION    XGC2 APT distribution.
                           Default: focal
  XGC2_APT_SMOKE_COMMAND   Optional command to run after package install.
USAGE
}

if [[ $# -eq 0 ]]; then
  usage >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required for APT smoke tests" >&2
  exit 127
fi

image="${XGC2_APT_SMOKE_IMAGE:-crpi-pest1z0t9z6yd8c6.cn-beijing.personal.cr.aliyuncs.com/xgc2-app-store/xgc-ros1-runtime:latest}"
distribution="${XGC2_APT_DISTRIBUTION:-focal}"
packages=("$@")

docker run --rm \
  --network host \
  -e "XGC2_APT_DISTRIBUTION=${distribution}" \
  -e "XGC2_APT_SMOKE_COMMAND=${XGC2_APT_SMOKE_COMMAND:-}" \
  "${image}" \
  bash -lc '
    set -euo pipefail
    packages=("$@")
    export DEBIAN_FRONTEND=noninteractive

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
    apt-get install -y --no-install-recommends "${packages[@]}"
    dpkg-query -W -f="\${binary:Package}\t\${Version}\n" "${packages[@]}"

    if [[ -n "${XGC2_APT_SMOKE_COMMAND}" ]]; then
      bash -lc "${XGC2_APT_SMOKE_COMMAND}"
    fi
  ' bash "${packages[@]}"
