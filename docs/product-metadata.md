# Product Metadata

Each product repository should provide `.xgc2/product.yml`.

## Required Fields

```yaml
schema: xgc2.product.v1
id: xgc2-swarm-sync-sim
name: XGC2 Swarm Sync Sim
kind: ros1-apt
```

`id` is the stable catalog identifier. It should not change when package
versions change.

`kind` can be:

- `ros1-apt`
- `ros2-apt`
- `toolchain-apt`
- `desktop-apt`
- `webui-docker`
- `app-store`
- `docker-image`
- `mixed`

## Ownership Fields

Use ownership fields to prevent two active products from claiming the same
runtime surface:

```yaml
ros:
  distro: noetic
  packages:
    - px4_rotor_sim

apt:
  distribution: focal
  install:
    - ros-noetic-xgc2-swarm-sync-sim
  packages:
    - ros-noetic-xgc2-sss-px4-rotor-sim

ownership:
  paths:
    - /opt/ros/noetic/share/px4_rotor_sim
    - /opt/ros/noetic/lib/libpx4_lib.so
```

The catalog validator treats these as exclusive ownership claims for active
products.

## Deprecation

If a product is kept only for history, mark it deprecated:

```yaml
lifecycle:
  deprecated: true
  replaced_by: xgc2-swarm-sync-sim
  notes: Functionality moved into the maintained swarm simulation package.
```

Deprecated products are excluded from active duplicate ownership checks.

## Usage

Keep install and smoke-test commands in the product repository:

```yaml
usage:
  install: |
    sudo apt update
    sudo apt install ros-noetic-xgc2-swarm-sync-sim
  smoke_test: |
    roslaunch px4_rotor_sim px4_rotor_sim_single.launch open_rviz:=false
```

Generated catalogs can render these snippets without copying them into
`xgc2-devops`.

## Release ordering and CI artifacts

`apt.depends` is the source of Debian runtime edges. Use `release.requires`
only when a build or installation check needs another XGC2 product first but
the relationship must not become a Debian `Depends` entry:

```yaml
release:
  repository: lxk36/xgc2-gazebo-sim-tools
  ref: noetic
  workflow: release.yml
  ci_workflow: ci.yml
  requires:
    - xgc2-scout-description
```

The release planner merges both edge sets, rejects cycles and hidden XGC2
installation dependencies, and waits for both architecture indexes and trusted
manifests before releasing downstream nodes. `ci_workflow` identifies the push
workflow whose exact-source artifacts may be reused; it defaults to `ci.yml`.
