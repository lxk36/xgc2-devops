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
cd xgc2-devops
GH_TOKEN=... python3 scripts/orchestrate-apt-release.py \
  --product libxgc2-math-dev \
  --execute
```

The orchestrator triggers each product workflow layer-by-layer. A downstream
product is released only after the upstream workflow completes and the expected
APT package version is visible in the repository `Packages` index.

Quality jobs are treated as optional unless `--quality-required` is passed. Build,
deb packaging, publish, and APT index verification remain required.

## GitHub Actions

Use the `release-orchestrator` workflow in `xgc2-devops` for manual releases.
Release orchestration remains owned by `xgc2-devops`; the top-level workspace
should only point to the devops revision that contains the orchestration system.

Executing cross-repository workflows requires a token with Actions write access
to the product repositories. Store it as `XGC2_RELEASE_ORCHESTRATOR_TOKEN`.
