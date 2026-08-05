#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

if ! command -v rg >/dev/null 2>&1; then
  echo "secret policy: ripgrep (rg) is required" >&2
  exit 2
fi

scan_targets=("$@")
if (( ${#scan_targets[@]} == 0 )); then
  for candidate in products scripts .github; do
    if [[ -e "${candidate}" ]]; then
      scan_targets+=("${candidate}")
    fi
  done
fi

if (( ${#scan_targets[@]} == 0 )); then
  echo "secret policy: no scan targets exist" >&2
  exit 2
fi

rg_args=(
  --hidden
  --color never
  --files-with-matches
  --pcre2
  --glob '!**/.git'
  --glob '!**/.git/**'
  --glob '!**/external/**'
  --glob '!**/node_modules/**'
  --glob '!**/vendor/**'
  # Some product repositories vendor upstream ASIO below include/asio rather
  # than a directory literally named vendor. Treat it as third-party code.
  --glob '!**/include/asio/**'
  # Scanner implementations necessarily contain the blocked patterns. Exclude
  # them at every nested product boundary while continuing to scan their tests,
  # source, configuration, and workflow files.
  --glob '!**/scripts/check-first-party-secrets.sh'
)

failed=0

reject() {
  local label="$1"
  local pattern="$2"
  local status

  if rg "${rg_args[@]}" --regexp "${pattern}" -- "${scan_targets[@]}"; then
    echo "secret policy: rejected ${label}" >&2
    failed=1
    return
  else
    status=$?
  fi

  if (( status != 1 )); then
    echo "secret policy: rg failed while checking ${label}" >&2
    exit "${status}"
  fi
}

# Lab shortcut usernames and passwords are intentional intranet functionality;
# only deployable tokens and key material belong in this policy.
reject \
  'the retired field bootstrap token' \
  '\bxgc-field-bootstrap\b'
reject \
  'private-key material' \
  '-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----'

if (( failed != 0 )); then
  echo 'secret policy: move credentials to an approved secret source and commit only placeholders or environment references' >&2
  exit 1
fi

echo 'secret policy: first-party tree contains no blocked credential literals'
