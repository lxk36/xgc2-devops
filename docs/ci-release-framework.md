# XGC2 CI / Release Framework

## Product contract

Each APT product branch owns `ci.yml` and `release.yml`.

`ci.yml` runs for push and pull requests. It builds and install-checks all
declared distributions and amd64/arm64 targets, then retains debs and
`xgc2.build-artifact.v1` for 14 days. It never receives production credentials.

`release.yml` is a fallback prepare/compatibility worker. Its standard inputs
are:

- `expected_version`
- `expected_source_sha`
- `prepare_action` (`release` or `compatibility-verify`)
- `apt_overlay_url`
- `dependency_set_digest`
- `run_cpp_quality`
- `run_source_tests`

For `release`, it performs the same build/install/test contract and uploads
build artifacts. For `compatibility-verify`, it validates against the release
staging overlay and does not emit publishable artifacts. Product workflows do
not accept `publish_apt`, do not generate release manifests, and do not call
SSH/APT publishing helpers.

## Control plane

The `xgc2-devops` release orchestrator:

- discovers APT products from non-empty `apt.install` or `apt.packages` metadata;
- builds the dependency graph and computes rebuild/verify/order impact;
- locks source SHAs, versions, distributions, package sets, and dependency
  digests;
- prepares dependency-ready nodes with at most four workers;
- validates every deb and manifest centrally;
- stages exact bundles in a release-scoped APT overlay;
- promotes one complete immutable generation only after the entire plan passes;
- verifies production Packages, deb SHA256, source manifests, and release lock.

The APT service's global lock serializes staging, promotion, garbage collection,
and legacy migration operations. Production is served through a `live`
generation symlink; a release becomes visible with one atomic switch.

## Fast-pass and failure policy

Fast-pass requires exact product, version, distribution, architecture, source
SHA, build/release digest, deb SHA256, and release lock agreement. Version-only
matches are never sufficient.

Independent branches continue preparing after a deterministic failure, but a
failed selected node prevents global promote. Transient network, lock, and APT
visibility errors retry; compile, test, schema, version, source, and hash errors
fail immediately. Resume uses the original release state and never replans.

## Enforcement

Catalog CI, release preflight, and scheduled audits reject any product-side APT
credential, production Environment, publish job/input, release-manifest writer,
SSH publisher, `reprepro`, or `aptly publish` use. This enforcement applies to
all products with APT metadata, regardless of their descriptive `kind`.

Jenkins may later provide heavy builders only by emitting the same trusted build
manifest contract. GitHub remains responsible for approval, dependency
scheduling, attestation, staging, and production promotion.
