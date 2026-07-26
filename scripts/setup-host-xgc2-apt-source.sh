#!/usr/bin/env bash
# Configure the signed XGC2 APT source and refresh indexes. No package install.
set -euo pipefail

SOURCE_URL="${XGC2_APT_SOURCE_URL:-https://xgc2.apt.xiaokang.ink}"
KEYRING_URL="${XGC2_APT_KEYRING_URL:-$SOURCE_URL/xgc2-archive-keyring.gpg}"
EXPECTED_FPR="${XGC2_APT_KEY_FINGERPRINT:-2A8E11B36F56D307ADF626D85E5FDC30979EA43F}"
COMPONENT="${XGC2_APT_COMPONENT:-main}"
LIST_FILE="${XGC2_APT_LIST_FILE:-/etc/apt/sources.list.d/xgc2.list}"
KEYRING_FILE="${XGC2_APT_KEYRING_FILE:-/etc/apt/keyrings/xgc2-archive-keyring.gpg}"
DISTRIBUTION="${XGC2_APT_DISTRIBUTION:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --distribution)
      DISTRIBUTION="${2:-}"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [--distribution CODENAME]"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$DISTRIBUTION" ]]; then
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRIBUTION="${VERSION_CODENAME:-}"
  fi
fi
DISTRIBUTION="${DISTRIBUTION:-focal}"

for command in curl gpg sudo tee install apt-get; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "Required command is missing: $command" >&2
    exit 1
  }
done

normalize_fpr() {
  tr -d ' :' <<<"$1" | tr '[:lower:]' '[:upper:]'
}

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/xgc2-apt-source.XXXXXX")"
cleanup() { rm -rf "$tmpdir"; }
trap cleanup EXIT

keyring_tmp="$tmpdir/xgc2-archive-keyring.gpg"
curl -fsSL "$KEYRING_URL" -o "$keyring_tmp"

found_fpr="$(
  gpg --show-keys --with-fingerprint --with-colons "$keyring_tmp" 2>/dev/null \
    | awk -F: '/^fpr:/{print $10; exit}'
)"
if [[ -z "$found_fpr" ]]; then
  echo "Could not read fingerprint from downloaded keyring." >&2
  exit 1
fi
if [[ "$(normalize_fpr "$found_fpr")" != "$(normalize_fpr "$EXPECTED_FPR")" ]]; then
  echo "XGC2 archive key fingerprint mismatch." >&2
  exit 1
fi

sudo install -d -m 0755 "$(dirname "$KEYRING_FILE")"
sudo install -m 0644 "$keyring_tmp" "$KEYRING_FILE"
printf 'deb [signed-by=%s] %s %s %s\n' "$KEYRING_FILE" "$SOURCE_URL" "$DISTRIBUTION" "$COMPONENT" \
  | sudo tee "$LIST_FILE" >/dev/null
sudo apt-get update -qq
