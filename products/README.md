# XGC2 Products

`products/` stores XGC2 deliverable repositories. A product is a component that can be built, tested, packaged, released, or deployed with a clear ownership boundary.

This directory is not a dumping ground for ad hoc workspace source. ROS1 source
truth belongs under `products/ros1` as real product repositories; local catkin
workspaces are disposable Docker build views over those repositories.

## Directory Classes

- `common/`: system-level libraries and tools that are not tied to ROS, such as `xgc2-acados`, `xgc2-tbb`, `libxgc2-state-machine-dev`, and `mavlink-routerd`.
- `utils/`: host-level utility packages and services that are not tied to a
  ROS distribution.
- `ros1/`: ROS Noetic products, grouped by domain. High-frequency source
  iteration still happens here; Docker decides which repositories are mounted
  into a temporary catkin workspace.
- `ros2/`: ROS2 or Gazebo Sim generation products.
- `robotics/`: real-vehicle profiles and target-machine service packages.
- `webui/`: frontend or visualization products.
- `xgc2/`: application-level product.

## Development APT Policy

Development workstations should not install XGC2 product packages directly into
the host ROS prefix. Use Docker for both APT smoke tests and local ROS1
iteration.

For source iteration, refresh the Aliyun runtime image tag and recreate the
development container:

```bash
helper/update-image.sh
helper/start-ros1-container.sh
```

The update helper only pulls the image and tags the source basename locally. It
does not install APT packages, build derived images, or modify the image. The
container helper mounts the real source tree and recreates an existing container
by default so mount, GPU, and environment changes take effect.

Both helpers default to the Aliyun ACR mirror of the app-store ROS1 runtime
image and local tag:

```text
crpi-pest1z0t9z6yd8c6.cn-beijing.personal.cr.aliyuncs.com/xgc2-app-store/xgc-ros1-runtime:latest
xgc-ros1-runtime:latest
```

For published package smoke tests, use a disposable APT test container:

```bash
scripts/docker-apt-smoke.sh ros-noetic-xgc2-ros1-utils
scripts/docker-upgrade-xgc2-apt.sh --dry-run
```

Host `sudo apt install` commands in product READMEs are target-machine install
instructions, not the normal development validation path. If a workstation was
previously polluted with XGC2 packages, use `scripts/print-host-xgc2-apt-purge.sh`
to print a reviewable cleanup command.

## Product Metadata

Packaged products should keep release metadata under `.xgc2/product.yml`. The metadata is the source of truth for:

- product id and version;
- package names;
- apt install names;
- supported distro and architecture scope;
- installed ownership paths;
- smoke-test commands.

Products may also contain `.xgc2/scripts/` for packaging helpers. Those scripts are product infrastructure and should not be installed into runtime packages unless explicitly required.

## README Policy

Each product should have a top-level `README.md` that answers four questions quickly:

1. What is this product?
2. What package does it publish?
3. How does a user install and smoke-test it?
4. What source, runtime, and release boundaries does it own?

This is a convention for now. CI does not yet enforce README shape or required sections.

## README Template

Use this template for new product READMEs or when cleaning old ones. Keep sections short and delete fields that are not relevant.

````markdown
# <Product Name>

Short one-paragraph description of what this product provides and who should install it.

## Package

- Product id: `<.xgc2 product id>`
- Source path: `products/<domain>/<name>`
- Release branch: `<branch>`
- Package type: `<system-deb | ros1-apt | ros2-apt | webui | app>`
- Published package(s):
  - `<apt-package-name>`
- Main runtime command(s):
  - `<command-or-roslaunch>`

## Install

```bash
sudo apt update
sudo apt install <package-name>
```

## Smoke Test

```bash
<minimal command proving the installed package is usable>
```

## What This Product Owns

- `<installed headers / launch files / binaries / libraries / config>`
- `<runtime behavior>`
- `<public API or CLI contract>`
- `<release metadata>`

## What This Product Does Not Own

- `<upstream source if vendored or fetched>`
- `<workspace-only experiments>`
- `<downstream application logic>`
- `<large generated files or build outputs>`

## Dependencies

Runtime dependencies:

- `<package>`

Build dependencies:

- `<package>`

Downstream packages should depend on `<published package>` instead of copying this source tree.

## Source Layout

```text
<path>                    <purpose>
.xgc2/product.yml         Release metadata
.xgc2/scripts/            Packaging and release helpers
```

## Build And Test

```bash
<local build command>
<local test command>
```

## Release Notes

- Supported distros: `<focal | jammy | noble | noetic | ...>`
- Supported architectures: `<amd64 | arm64>`
- CI workflow: `<workflow name or path>`
- Apt repository: `https://xgc2.apt.xiaokang.ink`
````

## Productization Rule

Before adding or reorganizing a ROS1 repository under `products/ros1`, confirm:

- the package has a stable responsibility boundary;
- downstream users can depend on a package name instead of a source path;
- generated build artifacts are excluded;
- headers, launch files, config, and binaries needed by downstream users are installed;
- CI can build and smoke-test the package for the intended distro and architecture set;
- `.xgc2/product.yml` and the product README describe the same package names.
- duplicate ROS package names are not introduced under `products/ros1`.

## Local Iteration Rule

High-frequency work should not create a second source tree. Mount the real
repositories from `products/ros1` and `products/common` into a Docker catkin
workspace, build there, and run Gazebo/RViz/SITL there. Use APT only when testing
published products or release DAGs.
