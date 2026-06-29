# XGC2 DevOps

`xgc2-devops` is the aggregation repository for XGC2 release infrastructure and
product source repositories. It should not become a hand-written catalog of ROS
packages, Docker apps, or Web UIs. Product repositories describe themselves with
`.xgc2/product.yml`; this repository validates those files and generates catalog
artifacts from them.

## Repository Roles

Platform repositories provide distribution infrastructure:

```text
platforms/apt-repo      -> xgc2-apt-repo
platforms/app-store     -> xgc2-app-store
```

Product repositories build installable artifacts:

```text
products/ros1/*
products/ros2/*
products/webui/*
products/xgc2
```

Long-lived source repositories should be mounted under `products/`; platform
repositories should be mounted under `platforms/`. Avoid adding new long-lived
repositories under temporary or top-level product directories.

## Product Metadata

Every product repository that participates in generated catalogs should include:

```text
.xgc2/product.yml
```

Validate and collect metadata:

```bash
scripts/docker-ci-local.sh
```

The validator rejects duplicate active ownership of:

- APT package names
- ROS package names
- owned installation paths
- Docker image names
- app-store app ids

If a repository is replaced by another product, mark it deprecated in its own
metadata instead of letting two active products claim the same ROS package or
file paths.

## APT Bootstrap

The following command shape is for target machines and release smoke tests. For
development workstations, prefer Docker-based validation so product packages do
not pollute the host ROS prefix:

```bash
scripts/docker-apt-smoke.sh ros-noetic-xgc2-swarm-sync-sim
scripts/docker-upgrade-xgc2-apt.sh --dry-run
scripts/docker-upgrade-xgc2-apt.sh --exclude-file products/ros1_dev/config/pre_product_apt_excludes.txt
```

```bash
curl -fsSL https://xgc2.apt.xiaokang.ink/xgc2-archive-keyring.gpg -o /tmp/xgc2-archive-keyring.gpg

gpg --show-keys --with-fingerprint --with-colons /tmp/xgc2-archive-keyring.gpg 2>&1 \
| grep -q '^fpr:\+2A8E11B36F56D307ADF626D85E5FDC30979EA43F:$' \
&& sudo install -d -m 0755 /etc/apt/keyrings \
&& cat /tmp/xgc2-archive-keyring.gpg \
| sudo tee /etc/apt/keyrings/xgc2-archive-keyring.gpg > /dev/null \
&& echo 'deb [signed-by=/etc/apt/keyrings/xgc2-archive-keyring.gpg] https://xgc2.apt.xiaokang.ink focal main' \
| sudo tee /etc/apt/sources.list.d/xgc2.list

sudo apt-get update
sudo apt-get install ros-noetic-xgc2-swarm-sync-sim
```
