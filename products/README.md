# XGC2 Products

`products/` stores XGC2 deliverable repositories. A product is a component that can be built, tested, packaged, released, or deployed with a clear ownership boundary.

This directory is not a dumping ground for active workspace source. Active ROS1 development belongs in `products/ros1_dev`; stable or reusable components should be promoted into a product directory and consumed through its released package.

## Directory Classes

- `common/`: system-level libraries and tools that are not tied to ROS, such as `xgc2-acados`, `xgc2-tbb`, `libxgc2-state-machine-dev`, and `mavlink-routerd`.
- `ros1/`: productized ROS Noetic packages released as apt packages, grouped by domain.
- `ros1_dev/`: high-frequency ROS1 development workspace. It depends on productized packages instead of duplicating their sources.
- `ros2/`: ROS2 or Gazebo Sim generation products.
- `webui/`: frontend or visualization products.
- `xgc1/`, `xgc2/`: application-level products.

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

## Promotion Rule

Before moving a package from `ros1_dev` or another development workspace into `products/`, confirm:

- the package has a stable responsibility boundary;
- downstream users can depend on a package name instead of a source path;
- generated build artifacts are excluded;
- headers, launch files, config, and binaries needed by downstream users are installed;
- CI can build and smoke-test the package for the intended distro and architecture set;
- `.xgc2/product.yml` and the product README describe the same package names.
