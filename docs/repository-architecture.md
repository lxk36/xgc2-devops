# Repository Architecture

`xgc2-devops` has three responsibilities:

1. Mount platform repositories and product source repositories as submodules.
2. Validate product metadata and ownership boundaries.
3. Generate machine-readable catalogs for APT, app-store, Docker, and future
   product portals.

It should not manually maintain product descriptions, install snippets, or
package indexes. Those belong to the product repositories.

## Target Layout

```text
xgc2-devops/
  platforms/
    apt-repo/
    app-store/
  products/
    xgc2/
    ros1/
    ros2/
    webui/
  catalog/
    generated/
  docs/
  scripts/
  schemas/
```

## Platform Repositories

Platform repositories host or publish artifacts. They are not product source
repositories.

- `xgc2-apt-repo`: Debian repository service and SSH publish endpoint.
- `xgc2-app-store`: app catalog and application packaging platform.

## Product Repositories

Product repositories build artifacts. Examples:

- ROS 1 APT packages
- ROS 2 APT packages
- toolchain APT packages
- Docker images
- Go or web UI applications
- app-store applications

Each product repository owns its own build workflow and must include
`.xgc2/product.yml`.

## Ownership Rules

Active products must not duplicate ownership of:

- ROS package names, such as `px4_rotor_sim`
- Debian package names, such as `ros-noetic-xgc2-sss-px4-rotor-sim`
- installed file trees, such as `/opt/ros/noetic/share/px4_rotor_sim`
- Docker image names
- app-store app ids

If a product intentionally replaces another product, encode that with
`lifecycle.deprecated` and `lifecycle.replaced_by` in the old product metadata,
or with explicit Debian `Conflicts/Replaces` inside the product build scripts.

## Generated Catalogs

Generated files under `catalog/generated/` are build outputs. They can be
committed when useful for static hosting, but they must be reproducible from
product metadata.
