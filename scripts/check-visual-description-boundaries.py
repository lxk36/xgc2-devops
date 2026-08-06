#!/usr/bin/env python3
"""Enforce the XGC2 visual-only contract for selected description packages."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from xml.etree import ElementTree


PACKAGES = (
    "b2arx_description",
    "fs150_description",
    "mecanum_description",
    "scout_description",
)
DISTRO_ROOTS = {
    "noetic": "products/ros1/robot",
    "melodic": "products/ros1/robot/melodic",
    "jazzy": "products/ros2/robot",
}
DISTRO_PACKAGES = {
    "noetic": PACKAGES,
    "melodic": ("scout_description",),
    "jazzy": PACKAGES,
}
FORBIDDEN_DIRECTORIES = (
    "action",
    "config",
    "include",
    "launch",
    "maps",
    "models",
    "msg",
    "nodes",
    "rviz",
    "scripts",
    "srv",
    "src",
    "worlds",
)
FORBIDDEN_URDF_TAGS = {
    "collision",
    "gazebo",
    "inertial",
    "plugin",
    "sensor",
    "transmission",
}
SHARED_ASSET_DIRECTORIES = ("meshes", "textures", "urdf")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def check_package(package_root: Path) -> list[str]:
    errors: list[str] = []
    for name in FORBIDDEN_DIRECTORIES:
        directory = package_root / name
        if directory.is_dir() and any(path.is_file() for path in directory.rglob("*")):
            errors.append(f"{package_root}: visual-only package contains {name}/")

    urdf_root = package_root / "urdf"
    if not urdf_root.is_dir():
        errors.append(f"{package_root}: visual-only package is missing urdf/")
        return errors

    model_files = sorted(
        path for path in urdf_root.rglob("*") if path.suffix.lower() in {".urdf", ".xacro", ".gazebo"}
    )
    if not model_files:
        errors.append(f"{package_root}: visual-only package has no URDF model")
        return errors

    for model in model_files:
        if model.suffix.lower() != ".urdf" or not model.name.endswith("_visual.urdf"):
            errors.append(
                f"{model}: only *_visual.urdf model files are allowed in a visual-only package"
            )
            continue
        try:
            root = ElementTree.parse(model).getroot()
        except ElementTree.ParseError as exc:
            errors.append(f"{model}: invalid XML: {exc}")
            continue
        tags = {local_name(element.tag) for element in root.iter()}
        forbidden = sorted(tags & FORBIDDEN_URDF_TAGS)
        if forbidden:
            errors.append(f"{model}: forbidden URDF tags: {', '.join(forbidden)}")

    return errors


def asset_manifest(package_root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for directory_name in SHARED_ASSET_DIRECTORIES:
        directory = package_root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            relative = path.relative_to(package_root).as_posix()
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            manifest[relative] = digest.hexdigest()
    return manifest


def validate(root: Path, *, require_complete: bool = False) -> list[str]:
    errors: list[str] = []
    found: set[str] = set()
    for distro, distro_root in DISTRO_ROOTS.items():
        for package in DISTRO_PACKAGES[distro]:
            package_root = root / distro_root / package
            if not package_root.exists():
                continue
            found.add(f"{distro_root}/{package}")
            errors.extend(check_package(package_root))

    melodic_root = root / DISTRO_ROOTS["melodic"]
    for package in PACKAGES:
        package_root = melodic_root / package
        if package != "scout_description" and package_root.exists():
            errors.append(
                f"{package_root}: Melodic support is restricted to scout_description"
            )

    if not found:
        errors.append("no managed visual description packages were found")
    if require_complete:
        expected = {
            f"{DISTRO_ROOTS[distro]}/{package}"
            for distro, packages in DISTRO_PACKAGES.items()
            for package in packages
        }
        for missing in sorted(expected - found):
            errors.append(f"{root / missing}: required visual description checkout is missing")
    for package in PACKAGES:
        reference = root / DISTRO_ROOTS["noetic"] / package
        if not reference.exists():
            continue
        reference_manifest = asset_manifest(reference)
        for distro in ("melodic", "jazzy"):
            if package not in DISTRO_PACKAGES[distro]:
                continue
            candidate = root / DISTRO_ROOTS[distro] / package
            if not candidate.exists():
                continue
            candidate_manifest = asset_manifest(candidate)
            if reference_manifest == candidate_manifest:
                continue
            differing = sorted(
                path
                for path in set(reference_manifest) | set(candidate_manifest)
                if reference_manifest.get(path) != candidate_manifest.get(path)
            )
            errors.append(
                f"{package}: Noetic/{distro.capitalize()} visual assets differ: "
                f"{', '.join(differing)}"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".", help="xgc2-devops repository root")
    args = parser.parse_args()
    errors = validate(Path(args.root).resolve(), require_complete=True)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("visual description boundaries: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
