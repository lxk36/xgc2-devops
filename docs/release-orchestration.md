# XGC2 APT Release Orchestration

`scripts/orchestrate-apt-release.py` turns product metadata into an APT release DAG.
It is intentionally owned by `xgc2-devops`; product repositories still only build
and publish themselves.

## Local dry-run

```bash
cd xgc2-devops
python3 scripts/orchestrate-apt-release.py --product libxgc2-math-dev
```

For simulation stack releases, use product groups instead of manually listing
every child package:

```bash
python3 scripts/orchestrate-apt-release.py --group gazebo-sim
python3 scripts/orchestrate-apt-release.py --group simulator
```

`gazebo-sim` includes the ROS1 Gazebo Classic stack: worlds, PX4 SITL wrappers,
FS150, Scout/UGV, CERLAB UAV plugins, VRPN bridge, visualization, tools, and the
aggregate `xgc2-gazebo-sim` package. `simulator` additionally covers other
simulator products such as ROS2 PX4 SITL and swarm-sync simulation packages.

This collects `.xgc2/product.yml`, expands downstream products that depend on the
seed product, resolves each product repository/ref/workflow, and writes
`.work/release-plan.json`.

## Execute

```bash
gh workflow run release-orchestrator.yml \
  --repo lxk36/xgc2-devops \
  -f products=libxgc2-math-dev \
  -f execute=true
```

The planner CLI is dry-run only. Its former `--execute` path is deliberately
disabled so local dispatch cannot bypass approvals, immutable locks, recovery
checks, or the dependency-ready scheduler.

The GitHub orchestrator runs a dependency-ready queue. A downstream product is
released only after every upstream workflow completes and the expected package,
deb SHA256, source SHA manifest, and release-lock digest are visible for both
APT architectures.

Quality jobs are treated as optional unless `--quality-required` is passed. Build,
deb packaging, publish, and APT index verification remain required.

## GitHub Actions

Use the `release-orchestrator` workflow in `xgc2-devops` for manual releases.
Release orchestration remains owned by `xgc2-devops`; the top-level workspace
should only point to the devops revision that contains the orchestration system.

Executing cross-repository workflows requires a token with Actions write access
to the product repositories. Store it as `XGC2_RELEASE_ORCHESTRATOR_TOKEN`.

## Scheduling and recovery

Do not manually dispatch dependent product releases in parallel. The supported
batch entrypoint is `release-orchestrator`; direct product dispatch is reserved
for isolated recovery after all prerequisites are already APT-visible.

The workflow uses one dependency-ready queue instead of fixed layer barriers.
Ready products start up to `max_parallel`; a failure blocks only its downstream
closure. Supply `resume_run_id` to reuse the prior `release-state.json`, keeping
successful nodes as verify-only work while retrying failed and blocked work.
Every resumed success must re-confirm the current APT indexes and lock-bound
manifests before it can release downstream nodes. Resume is rejected when the
plan digest or release-lock digest differs. Resume downloads the prior plan,
lock, state, and per-node publish checkpoints verbatim; it never replans or bumps
versions. The exact child run ID is checkpointed immediately after dispatch. A
workflow wait timeout resumes that run, and a node whose workflow already
published but whose APT visibility check timed out resumes visibility
verification, without dispatching another build. Only exit code 75 (temporary
network/TLS, APT propagation, or publish-lock failure) is retried, after 15, 30,
and 60 seconds. Compile, test, version, metadata, and source-SHA failures are not
retried. `apt.depends` expresses installation dependencies; `release.requires`
adds ordering-only dependencies.

Visibility polling downloads each `(distribution, architecture)` Packages index
once per poll and reuses it for every package in that product. Scheduler metrics
report child workflow, publish-job, and APT visibility time separately.

## Trusted artifacts and APT single writer

Push CI retains debs plus `xgc2.build-artifact.v1` for 14 days. The build
manifest records source SHA, version, target architecture, distribution, CI run
identity, dpkg metadata, and deb SHA256; it intentionally has no release lock.
Release can reuse an exact-SHA successful push run, validates every manifest and
deb, and emits `xgc2.release-artifact.v1` with the release ID, lock digest, build
manifest digest, and publication time. If no matching live CI artifact appears
within 30 minutes, release falls back to the same validated in-release build path.

Architectures build in parallel, but only one `publish-apt` job downloads the
complete set and updates a distribution. The `xgc2-apt-<distribution>` GitHub
concurrency group only removes duplicate writers inside one repository. The APT
server's global `/srv/aptly/.xgc2-publish.lock` is the authoritative
cross-repository writer lock; aptly update and manifest installation happen
inside that lock. Build jobs never update APT indexes.
