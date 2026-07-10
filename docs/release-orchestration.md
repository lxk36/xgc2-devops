# XGC2 APT Release Orchestration

`xgc2-devops` is the only XGC2 APT release control plane. Product repositories
build and test packages, but they do not hold APT credentials, create release
manifests, or mutate repository indexes.

## Entry point

Create a dry-run plan locally:

```bash
python3 scripts/orchestrate-apt-release.py --root . --group gazebo-sim
```

Execute a reviewed release through GitHub Actions:

```bash
gh workflow run release-orchestrator.yml \
  --repo lxk36/xgc2-devops \
  -f groups=gazebo-sim \
  -f execute=true
```

The `plan` job never receives production credentials. The `execute-release`
job references the protected `xgc2-apt-production` Environment and is globally
serialized by the `xgc2-apt-production` concurrency group. Do not dispatch
product release workflows manually.

## Prepare and promote

Every release has an immutable release ID, plan digest, and lock digest. Ready
nodes prepare with at most four builds in flight:

1. Reuse an exact-SHA successful push artifact only when none of its upstream
   packages changes in the same train.
2. Otherwise dispatch the product fallback builder with `publish_apt` disabled.
3. Re-read deb metadata, recompute SHA256, and validate all declared
   distribution/architecture/package coverage centrally.
4. Upload the validated bundle to `staging/<release_id>`.
5. Release downstream work only after the upstream staging index and manifest
   match the expected bundle digest.

Independent branches continue preparing after another branch fails, but no
production change occurs unless every selected release and compatibility node
passes. The orchestrator then submits one sorted `xgc2.release-train.v1` and the
APT service atomically switches the complete production generation.

## Dependency impact

`apt.depends` discovers internal package dependencies and defaults to
`rebuild`. `release.requires` expresses ordering and defaults to `order`.
Products may override a direct upstream edge:

```yaml
release:
  dependency_policy:
    libxgc2-math-dev: rebuild
    xgc2-runtime-sync: verify
    xgc2-gazebo-sim-tools: order
```

- `rebuild`: bump, rebuild, test, stage, and continue propagating downstream.
- `verify`: run compatibility/install/smoke checks against staging without
  publishing a new package.
- `order`: add a scheduling edge only when both products are already selected.

A node with staged upstream packages must run the release-scoped builder. Its
old push CI result describes a different dependency context and is not reused.
An explicitly failed root push CI remains a deterministic failure.

## Trusted artifacts

Push CI stores debs plus `xgc2.build-artifact.v1` for 14 days. Build manifests
record product, source SHA, version, distribution, target architecture, CI run
identity, dpkg metadata, and deb SHA256; they intentionally contain no release
lock. The control plane adds a release-specific prepare attestation and emits
`xgc2.release-artifact.v1` only after independently validating the files.

Artifact names are not an interface. The control plane downloads every artifact
from the exact run and discovers manifests recursively, allowing one product to
span multiple build artifacts.

## Recovery

`xgc2.release-state.v2` binds the stable release ID, plan, lock, train digest,
and monotonic node checkpoints. Resume always downloads the exact prior state:

- a dispatched node waits for its recorded run ID and never guesses by time;
- an artifact is downloaded and revalidated before reuse;
- a matching staging receipt fast-passes expired local artifacts;
- an uncertain promote queries the promotion receipt before retrying;
- a production success is reverified against Packages and manifests.

Only exit code 75 (temporary network, lock, or index visibility failure) is
retried after 15, 30, and 60 seconds. Compile, test, metadata, version, source,
and digest failures are not retried. Any plan, lock, or release-ID mismatch
rejects resume.

## Credential boundary

Only `lxk36/xgc2-devops` owns the `xgc2-apt-production` Environment:

- `APT_REPO_HOST`
- `APT_REPO_PORT`
- `APT_REPO_USER`
- `APT_REPO_SSH_KEY`
- `APT_REPO_KNOWN_HOSTS`

Product workflows and scripts are audited for these names and for all direct
SSH, aptly, reprepro, or publish helper calls. The APT service also disables the
legacy publish command after migration, so restoring an old workflow cannot
restore product-level write access.
