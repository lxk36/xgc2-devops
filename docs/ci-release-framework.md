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

## Top-Level Orchestration

`release-orchestrator.yml` is the only global release workflow. It:

- Collects `.xgc2/product.yml` metadata.
- Builds the internal APT dependency graph from `apt.packages`, `apt.install`,
  and `apt.depends`.
- Expands selected products with upstream prerequisites and downstream consumers.
- Classifies nodes as `release` or `verify`.
- Runs visible GitHub Actions matrix jobs per dependency layer.
- Waits for child repository workflows and verifies APT visibility.

`catalog.yml` remains lightweight. It validates metadata, shows module/DAG
summaries, and runs workflow audits without triggering child releases.

Top-level devops owns the release DAG and workflow contract checks only. Child
repositories own their build logic, dependency installation, packaging scripts,
and branch-specific workflow details.

## Release Reuse

Fast-pass is allowed only when both are true:

- The expected APT version exists for every required package, distribution, and
  architecture.
- A release manifest for that version reports the expected source SHA.

Version-only fast-pass is not allowed.
