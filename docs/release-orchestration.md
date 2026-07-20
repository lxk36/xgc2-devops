# XGC2 APT Release Orchestration

`xgc2-devops` is the standard automated and batch XGC2 APT release control
plane. Product repositories build and test packages, but they do not hold APT
credentials, create release manifests, or mutate repository indexes. The
server's WAF-protected administrator console remains available for explicit
operator upload/delete maintenance; it uses the same global lock and atomic
generation switch and is not a substitute for dependency-ordered batch release.

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

`apt.depends` declares hard Debian installation/runtime dependencies, while
`apt.recommends` declares optional runtime integrations that Debian installs by
default but may omit. `release.requires` adds a build, install-check, or
scheduling prerequisite without creating a Debian relationship. None of these
fields is allowed to imply release impact: every direct internal edge must be
classified explicitly under `release.dependency_policy`, keyed by the upstream
**product id**:

```yaml
apt:
  depends:
    - ros-noetic-xgc2-runtime-sync
  recommends:
    - ros-noetic-xgc2-gazebo-sim-examples
release:
  requires:
    - libxgc2-math-dev
  dependency_policy:
    libxgc2-math-dev: rebuild
    xgc2-runtime-sync: verify
    xgc2-gazebo-sim-tools: order
```

- `rebuild`: bump, rebuild, test, stage, and continue propagating downstream.
- `verify`: run compatibility/install/smoke checks against staging without
  publishing a new package.
- `order`: add a scheduling edge only when both products are already selected.

Choose policy from the actual coupling, independently of whether Debian uses a
hard `Depends`:

- compiled headers, linked ABI, or generated source/interface: `rebuild`;
- runtime resources, configuration, launch composition, CLI/topic/service
  contracts: `verify`;
- aggregate/meta package version floors and sequencing-only edges: `order`.

Catalog collection and release planning both reject missing, invalid, or
non-direct policy keys. This is deliberate: adding an internal Debian package
dependency or recommendation must not silently expand the release train. During
a bounded migration only, `collect-products.py` and
`orchestrate-apt-release.py` accept
`--allow-implicit-dependency-policy`; it restores the legacy
`apt.depends=rebuild` and `release.requires=order` behavior, treats a new
`apt.recommends` edge as `verify`, and emits a warning.
CI and production preflight must never use that escape hatch. The generated
release-plan format remains `xgc2.release-plan.v2`; each item records the
resolved edge origins in the additive `dependency_sources` map so reviews can
distinguish `apt.depends`, `apt.recommends`, and `release.requires`.

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
SSH, aptly, reprepro, or publish helper calls. Removing product-repository
credentials prevents restoring an old workflow from restoring product-level
write access. The WAF-authenticated server administrator console is retained as
a separate human operations path and still performs atomic, globally locked
mutations.

Credential cleanup never disables the administrator console or its upload and
exact-delete operations. A production server deployment must converge
`ADMIN_UI_ENABLED=true` and `ALLOW_LEGACY_PUBLISH=true`, then verify the backend
`/admin/manage` page and `/admin/api/status`; SafeLine remains the external
authentication boundary.
