#!/usr/bin/env python3
"""Reject stale or incomplete product metadata before release dispatch."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml


XGC2_PACKAGE = re.compile(r"\b(?:libxgc2-[a-z0-9.+-]+|ros-[a-z0-9${}_-]+-xgc2-[a-z0-9.+-]+)\b")
HARDCODED_VERSION = re.compile(r"\^version:\s+([0-9][A-Za-z0-9.+:~_-]*)\$")


def items(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for layer in plan.get("layers", []) for item in layer]


def package_name(value: str) -> str:
    return value.split(" ", 1)[0]


def dependency_packages(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9+${}_.-]+", value)
        if not token[0].isdigit()
    }


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected mapping")
    return data


def scoped_version_matches(product_version: str, apt_version: str) -> bool:
    return apt_version == product_version or apt_version.startswith(
        (f"{product_version}~", f"{product_version}+")
    )


def catalog_products(catalog: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not catalog:
        return []
    products = catalog.get("products", [])
    return [item for item in products if isinstance(item, dict)] if isinstance(products, list) else []


def build_package_owners(
    planned: list[dict[str, Any]], catalog: dict[str, Any] | None
) -> dict[str, tuple[str, str]]:
    owners: dict[str, tuple[str, str]] = {}
    for product in catalog_products(catalog):
        apt = product.get("apt") if isinstance(product.get("apt"), dict) else {}
        release = product.get("release") if isinstance(product.get("release"), dict) else {}
        apt_versions = release.get("apt_versions") if isinstance(release.get("apt_versions"), dict) else {}
        version = str(apt_versions.get("focal") or product.get("version", ""))
        for package in (*apt.get("packages", []), *apt.get("install", [])):
            owners[str(package)] = (str(product.get("id", "")), version)
    for item in planned:
        apt_versions = item.get("apt_versions") if isinstance(item.get("apt_versions"), dict) else {}
        version = str(apt_versions.get("focal") or item.get("expected_version", ""))
        for package in (*item.get("apt_packages", []), *item.get("apt_install", [])):
            owners[str(package)] = (str(item["id"]), version)
    return owners


def validate(
    root: Path,
    plan: dict[str, Any],
    *,
    catalog: dict[str, Any] | None = None,
    allow_planned_updates: bool = False,
) -> list[str]:
    errors: list[str] = []
    planned = items(plan)
    package_owners = build_package_owners(planned, catalog)

    for item in planned:
        product_id = str(item["id"])
        if item.get("action") != "release":
            continue
        source = Path(str(item["source"]))
        if not source.is_absolute():
            source = root / source
        metadata_path = source / ".xgc2" / "product.yml"
        metadata = load_yaml(metadata_path)
        apt = metadata.get("apt", {}) if isinstance(metadata.get("apt"), dict) else {}
        release = metadata.get("release", {}) if isinstance(metadata.get("release"), dict) else {}
        declared_packages = {
            package
            for dependency in apt.get("depends", [])
            for package in dependency_packages(str(dependency))
        }
        declared_products = {str(dep) for dep in release.get("requires", [])}

        expected_product_version = str(item.get("expected_version", ""))
        planned_apt_versions = item.get("apt_versions")
        if (
            expected_product_version
            and isinstance(planned_apt_versions, dict)
            and not item.get("apt_version_template")
        ):
            for distribution, version in planned_apt_versions.items():
                if not scoped_version_matches(expected_product_version, str(version)):
                    errors.append(
                        f"{product_id}: planned APT version {distribution}={version} "
                        f"does not match product version {expected_product_version}"
                    )
        metadata_version = str(metadata.get("version", ""))
        metadata_apt_versions = release.get("apt_versions")
        if not allow_planned_updates and isinstance(metadata_apt_versions, dict):
            for distribution, version in metadata_apt_versions.items():
                if not scoped_version_matches(metadata_version, str(version)):
                    errors.append(
                        f"{product_id}: product version {metadata_version} and "
                        f"release.apt_versions[{distribution}]={version} are inconsistent"
                    )

        scripts_dir = source / ".xgc2" / "scripts"
        workflow_dir = source / ".github" / "workflows"
        inspected_paths = []
        if scripts_dir.exists():
            inspected_paths.extend(sorted(scripts_dir.glob("*.sh")))
        if workflow_dir.exists():
            inspected_paths.extend(sorted(workflow_dir.glob("*.y*ml")))
        if inspected_paths:
            dependency_text = "\n".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in inspected_paths
                if path.name != "check_package_compliance.sh"
            )
            for package in sorted(set(XGC2_PACKAGE.findall(dependency_text))):
                normalized = package.replace("ros-${ROS_DISTRO}-", "ros-noetic-").replace(
                    "ros-\${ROS_DISTRO}-", "ros-noetic-"
                )
                owner = package_owners.get(normalized)
                if (
                    owner
                    and owner[0] != product_id
                    and normalized not in declared_packages
                    and owner[0] not in declared_products
                ):
                    errors.append(
                        f"{product_id}: scripts use {normalized}, but apt.depends/release.requires does not declare {owner[0]}"
                    )
            version_text = "\n".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in inspected_paths
            )
            current_version = metadata_version
            for hardcoded in HARDCODED_VERSION.findall(version_text):
                if hardcoded != current_version:
                    errors.append(
                        f"{product_id}: compliance script hard-codes version {hardcoded}, metadata has {current_version}"
                    )

        release_set = source / ".xgc2" / "release-set.yml"
        if release_set.exists():
            release_data = load_yaml(release_set)
            packages = release_data.get("packages", {})
            if isinstance(packages, dict):
                for entry_name, entry in packages.items():
                    if not isinstance(entry, dict):
                        continue
                    apt_package = str(entry.get("apt", ""))
                    owner = package_owners.get(apt_package)
                    if owner and str(entry.get("version", "")) != owner[1] and not allow_planned_updates:
                        errors.append(
                            f"{product_id}: release-set {entry_name}={entry.get('version')} but plan requires {owner[1]}"
                        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--catalog", help="full collect-products JSON for hidden-dependency owners")
    parser.add_argument("--allow-planned-updates", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8")) if args.catalog else None
    errors = validate(
        root,
        plan,
        catalog=catalog,
        allow_planned_updates=args.allow_planned_updates,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("release plan metadata validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
