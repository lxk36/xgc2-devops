# XGC2 CI / Release Framework

## Development Loop

Local Docker is the primary development loop. Developers iterate with mounted source
overlays, run builds and Gazebo/RViz simulations locally, and push commits mainly as
recoverable checkpoints.

## Product CI Contract

Every product release branch owns its own two workflows. For example, `noetic`,
`master`, `v1.12-noetic`, and `v1.14-noetic` keep branch-local workflow files
that match the product built from that branch. `xgc2-devops` does not copy,
generate, or centrally manage child workflow implementations.

Each product branch should expose:

- `ci.yml`: push, pull request, and manual reference checks. It builds and tests
  source, may upload deb artifacts and manifests, but must never publish APT.
- `release.yml`: manual-only release entrypoint. It is triggered by
  `xgc2-devops` and owns deb build, APT publish, and release manifest emission.

Required `release.yml` inputs:

- `expected_version`
- `expected_source_sha`
- `publish_apt`
- `run_cpp_quality`
- `run_source_tests`
- `release_id`
- `release_lock_digest`

`publish_apt`, `run_cpp_quality`, and `run_source_tests` must explicitly declare
`type: boolean`; an untyped `"false"` is a non-empty string and can accidentally
enable optional work.

`trusted_ci_run_id` is optional. When present, release downloads artifacts only
from that exact run and validates `xgc2.build-artifact.v1` before publishing.

## Top-Level Orchestration

`release-orchestrator.yml` is the only global release workflow. It:

- Collects `.xgc2/product.yml` metadata.
- Builds the internal APT dependency graph from `apt.packages`, `apt.install`,
  and `apt.depends`.
- Expands selected products with upstream prerequisites and downstream consumers.
- Classifies nodes as `release` or `verify`.
- Runs one dependency-ready queue with at most four product releases in flight.
- Waits for child workflows and verifies both architecture indexes plus release
  manifests before making downstream nodes ready.

`catalog.yml` remains lightweight. It validates metadata, shows module/DAG
summaries, and runs workflow audits without triggering child releases.

Top-level devops owns the release DAG and workflow contract checks only. Child
repositories own their build logic, dependency installation, packaging scripts,
and branch-specific workflow details.

## Release Reuse

Fast-pass is allowed only when all are true:

- The expected APT version exists for every required package, distribution, and
  architecture.
- The canonical release manifest reports the expected product, distribution,
  target architecture, source SHA, and release-lock digest.
- The manifest deb SHA256 equals the SHA256 in the APT Packages stanza.

Version-only fast-pass is not allowed.
