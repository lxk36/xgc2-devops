#!/usr/bin/env python3
"""Reject stale or incomplete product metadata before release dispatch."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml


XGC2_PACKAGE = re.compile(r"\b(?:libxgc2-[a-z0-9.+-]+|ros-[a-z0-9${}_-]+-xgc2-[a-z0-9.+-]+)\b")
HARDCODED_VERSION = re.compile(r"\^version:\s+([0-9][A-Za-z0-9.+:~_-]*)\$")
DEBIAN_RELATION_OPERATORS = frozenset({"<<", "<=", "=", ">=", ">>"})
DPKG_RELATION_OPERATORS = {
    "lt": "<<",
    "le": "<=",
    "eq": "=",
    "ge": ">=",
    "gt": ">>",
}


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


def debian_relation_satisfied(
    actual_version: str,
    operator: str,
    required_version: str,
) -> bool:
    """Use dpkg's canonical ordering to evaluate a Debian version relation."""
    if operator not in DEBIAN_RELATION_OPERATORS:
        raise ValueError(f"unsupported Debian relationship operator: {operator}")
    result = subprocess.run(
        ["dpkg", "--compare-versions", actual_version, operator, required_version],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode not in {0, 1}:
        detail = result.stderr.strip() or "dpkg --compare-versions failed"
        raise ValueError(detail)
    return result.returncode == 0


def catalog_products(catalog: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not catalog:
        return []
    products = catalog.get("products", [])
    return [item for item in products if isinstance(item, dict)] if isinstance(products, list) else []


def build_package_owners(
    planned: list[dict[str, Any]], catalog: dict[str, Any] | None
) -> dict[str, tuple[str, dict[str, str]]]:
    owners: dict[str, tuple[str, dict[str, str]]] = {}
    for product in catalog_products(catalog):
        apt = product.get("apt") if isinstance(product.get("apt"), dict) else {}
        release = product.get("release") if isinstance(product.get("release"), dict) else {}
        apt_versions = release.get("apt_versions") if isinstance(release.get("apt_versions"), dict) else {}
        distributions = [
            value.strip()
            for value in str(apt.get("distribution", "focal")).split(",")
            if value.strip()
        ]
        versions = {
            distribution: str(apt_versions.get(distribution) or product.get("version", ""))
            for distribution in distributions or ["focal"]
        }
        package_distribution_map = (
            apt.get("package_distributions")
            if isinstance(apt.get("package_distributions"), dict)
            else {}
        )
        for package in (*apt.get("packages", []), *apt.get("install", [])):
            normalized = package_name(str(package))
            package_distributions = package_distribution_map.get(normalized, distributions)
            owners[normalized] = (
                str(product.get("id", "")),
                {
                    distribution: version
                    for distribution, version in versions.items()
                    if distribution in set(map(str, package_distributions))
                },
            )
    for item in planned:
        apt_versions = item.get("apt_versions") if isinstance(item.get("apt_versions"), dict) else {}
        versions = {
            str(distribution): str(
                apt_versions.get(str(distribution)) or item.get("expected_version", "")
            )
            for distribution in item.get("apt_distributions", ["focal"])
        }
        package_distribution_map = (
            item.get("apt_package_distributions")
            if isinstance(item.get("apt_package_distributions"), dict)
            else {}
        )
        for package in (*item.get("apt_packages", []), *item.get("apt_install", [])):
            normalized = package_name(str(package))
            package_distributions = package_distribution_map.get(
                normalized, item.get("apt_distributions", ["focal"])
            )
            owners[normalized] = (
                str(item["id"]),
                {
                    distribution: version
                    for distribution, version in versions.items()
                    if distribution in set(map(str, package_distributions))
                },
            )
    return owners


def owner_default_version(owner: tuple[str, dict[str, str]]) -> str:
    versions = owner[1]
    return versions.get("focal") or next(iter(versions.values()), "")


def package_variants(package: str) -> list[tuple[str, str | None]]:
    variants: list[tuple[str, str | None]] = [(package, None)]
    if package.startswith("ros-noetic-"):
        suffix = package[len("ros-noetic-") :]
        variants.extend(
            [(f"ros-${{ROS_DISTRO}}-{suffix}", "focal"), (f"ros-\\${{ROS_DISTRO}}-{suffix}", "focal")]
        )
        variants[0] = (package, "focal")
    elif package.startswith("ros-melodic-"):
        variants[0] = (package, "bionic")
        suffix = package[len("ros-melodic-") :]
        variants.extend(
            [(f"ros-${{ROS_DISTRO}}-{suffix}", "bionic"), (f"ros-\\${{ROS_DISTRO}}-{suffix}", "bionic")]
        )
    return variants


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
        if item.get("action") not in {"release", "compatibility-verify"}:
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
            for dependency in [
                *apt.get("depends", []),
                *apt.get("recommends", []),
            ]
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

        runtime_manifest_path = source / "manifest" / "px4_runtime.yaml"
        if runtime_manifest_path.exists():
            runtime_manifest = load_yaml(runtime_manifest_path)
            expected_runtime_version = (
                expected_product_version if allow_planned_updates else metadata_version
            )
            if str(runtime_manifest.get("debian_version", "")) != expected_runtime_version:
                errors.append(
                    f"{product_id}: manifest/px4_runtime.yaml debian_version="
                    f"{runtime_manifest.get('debian_version')} but product version is "
                    f"{expected_runtime_version}"
                )

        scripts_dir = source / ".xgc2" / "scripts"
        workflow_dir = source / ".github" / "workflows"
        inspected_paths = []
        if scripts_dir.exists():
            inspected_paths.extend(sorted(path for path in scripts_dir.rglob("*") if path.is_file()))
        if workflow_dir.exists():
            inspected_paths.extend(sorted(workflow_dir.glob("*.y*ml")))
        if inspected_paths:
            for path in inspected_paths:
                if path.name in {"package_debs.sh", "check_package_compliance.sh"}:
                    continue
                in_control_heredoc = False
                control_continuation = False
                for line_number, line in enumerate(
                    path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
                ):
                    if "DEBIAN/control" in line and "<<" in line:
                        in_control_heredoc = True
                    semantic_control = (
                        in_control_heredoc
                        or control_continuation
                        or "write_control" in line
                        or bool(
                            re.search(
                                r"\b(?:Pre-Depends|Depends|Recommends)\s*[:=]",
                                line,
                            )
                        )
                    )
                    control_continuation = semantic_control and line.rstrip().endswith("\\")
                    if in_control_heredoc and line.strip() in {"EOF", "CONTROL"}:
                        in_control_heredoc = False
                    if (
                        "grep" in line
                        or semantic_control
                    ):
                        continue
                    for owned_package in package_owners:
                        if any(
                            re.search(
                                rf"(?<![A-Za-z0-9+_.-]){re.escape(variant)}"
                                r"\s+\(\s*(?:<<|<=|=|>=|>>)\s*[^)]+\)",
                                line,
                            )
                            for variant, _distribution in package_variants(owned_package)
                        ):
                            errors.append(
                                f"{product_id}: {path.relative_to(source)}:{line_number} "
                                f"uses Debian relationship syntax in a command/package-name context"
                            )
                            break
            raw_dependency_text = "\n".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in inspected_paths
                if path.name != "check_package_compliance.sh"
            )
            dependency_text = "\n".join(
                line
                for line in raw_dependency_text.splitlines()
                if re.match(r"^\s*(?:Replaces|Breaks|Conflicts|Provides)\s*:", line) is None
            )
            for normalized, owner in sorted(package_owners.items()):
                if owner[0] == product_id:
                    continue
                item_distributions = set(map(str, item.get("apt_distributions", ["focal"])))
                relevant_variants = [
                    (variant, distribution)
                    for variant, distribution in package_variants(normalized)
                    if distribution is None or distribution in item_distributions
                ]
                if not any(
                    re.search(
                        rf"(?<![A-Za-z0-9+_.-]){re.escape(variant)}"
                        r"(?![A-Za-z0-9+_.-])",
                        dependency_text,
                    )
                    for variant, _dist in relevant_variants
                ):
                    continue
                if normalized not in declared_packages and owner[0] not in declared_products:
                    errors.append(
                        f"{product_id}: scripts use {normalized}, but "
                        "apt.depends/apt.recommends/release.requires does not "
                        f"declare {owner[0]}"
                    )
                relation_tokens: list[tuple[str, str | None]] = []
                for variant, inferred_distribution in relevant_variants:
                    relation_tokens.append((variant, inferred_distribution))
                    assignment = re.compile(
                        rf"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
                        rf"(['\"]?){re.escape(variant)}\2\s*(?:#.*)?$"
                    )
                    relation_tokens.extend(
                        (f"${{{match.group(1)}}}", inferred_distribution)
                        for match in assignment.finditer(dependency_text)
                    )
                for variant, inferred_distribution in relation_tokens:
                    relation = re.compile(
                        re.escape(variant)
                        + r"\s*\(\s*(<<|<=|=|>=|>>)\s*([^)$\s][^)]*)\)"
                    )
                    for operator, declared_version in relation.findall(dependency_text):
                        candidate_distributions = (
                            [inferred_distribution]
                            if inferred_distribution
                            else list(map(str, item.get("apt_distributions", ["focal"])))
                        )
                        for distribution in candidate_distributions:
                            expected = owner[1].get(distribution)
                            required = declared_version.strip()
                            if expected and not debian_relation_satisfied(
                                expected,
                                operator,
                                required,
                            ):
                                errors.append(
                                    f"{product_id}: {variant} relation requires {operator} {required} "
                                    f"for {distribution}, but {owner[0]} current APT version "
                                    f"{expected} does not satisfy it"
                                )
                    for line in dependency_text.splitlines():
                        if re.search(
                            rf"(?<![A-Za-z0-9+_.-]){re.escape(variant)}"
                            r"(?![A-Za-z0-9+_.-])",
                            line,
                        ) is None:
                            continue
                        compare_relations: list[tuple[str, str]] = []
                        if "dpkg --compare-versions" in line:
                            compare_relations.extend(
                                (DPKG_RELATION_OPERATORS[operator], version)
                                for operator, version in re.findall(
                                    r"\b(lt|le|eq|ge|gt)\s+['\"]([^'\"]+)['\"]",
                                    line,
                                )
                            )
                        hard_pins = re.findall(
                            rf"{re.escape(variant)}=([0-9][A-Za-z0-9.+:~_-]*)",
                            line,
                        )
                        candidate_distributions = (
                            [inferred_distribution]
                            if inferred_distribution
                            else list(item_distributions)
                        )
                        for operator, required in compare_relations:
                            for distribution in candidate_distributions:
                                expected = owner[1].get(distribution)
                                if expected and not debian_relation_satisfied(
                                    expected,
                                    operator,
                                    required,
                                ):
                                    errors.append(
                                        f"{product_id}: {variant} comparison requires "
                                        f"{operator} {required} for {distribution}, but "
                                        f"{owner[0]} current APT version {expected} does not "
                                        "satisfy it"
                                    )
                        for declared_version in hard_pins:
                            for distribution in candidate_distributions:
                                expected = owner[1].get(distribution)
                                if expected and declared_version != expected:
                                    errors.append(
                                        f"{product_id}: {variant} hard-coded install/compliance "
                                        f"version is {declared_version} for {distribution}, but "
                                        f"{owner[0]} current APT version is {expected}"
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
                    if not owner or allow_planned_updates:
                        continue
                    actual = owner_default_version(owner)
                    required = str(entry.get("version", ""))
                    if entry.get("local"):
                        satisfied = actual == required
                        relation_description = f"exact version {required}"
                    else:
                        satisfied = bool(required) and debian_relation_satisfied(
                            actual,
                            ">=",
                            required,
                        )
                        relation_description = f"compatible minimum {required}"
                    if not satisfied:
                        errors.append(
                            f"{product_id}: release-set {entry_name} requires "
                            f"{relation_description}, but {owner[0]} current APT version "
                            f"is {actual}"
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
