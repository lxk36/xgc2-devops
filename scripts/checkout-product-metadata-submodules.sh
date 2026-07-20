#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "${repo_root}"

# Resolve before changing credentials or contacting a remote. The tracked
# catalog is the release control-plane contract; an incomplete or invalid
# mapping must stop the job instead of silently producing a partial catalog.
resolution_file="$(mktemp "${TMPDIR:-/tmp}/xgc2-metadata-submodules.XXXXXX")"
cleanup() {
  rm -f -- "${resolution_file}"
}
trap cleanup EXIT

python3 scripts/resolve-product-metadata-submodules.py \
  --root "${repo_root}" > "${resolution_file}"
mapfile -t submodules < "${resolution_file}"
if (( ${#submodules[@]} == 0 )); then
  echo "::error title=Product metadata resolution failed::tracked catalog resolved to no submodules"
  exit 1
fi

if [[ -n "${SUBMODULE_SSH_KEY:-}" ]]; then
  install -m 0700 -d "${HOME}/.ssh"
  key_file="${HOME}/.ssh/xgc2-submodule-key"
  printf '%s\n' "${SUBMODULE_SSH_KEY}" > "${key_file}"
  chmod 0600 "${key_file}"
  ssh-keyscan github.com >> "${HOME}/.ssh/known_hosts"
  git config --global core.sshCommand \
    "ssh -i ${key_file} -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes"
elif [[ -n "${GH_TOKEN:-}" ]]; then
  git config --global \
    url."https://x-access-token:${GH_TOKEN}@github.com/".insteadOf git@github.com:
else
  git config --global url."https://github.com/".insteadOf git@github.com:
fi

for submodule_path in "${submodules[@]}"; do
  echo "checking out product metadata root: ${submodule_path}"
  if ! git submodule update --init --recursive --depth=1 "${submodule_path}"; then
    echo "::error title=Submodule checkout failed::${submodule_path} could not be checked out"
    exit 1
  fi
done

python3 scripts/resolve-product-metadata-submodules.py \
  --root "${repo_root}" \
  --verify-checkout > /dev/null
