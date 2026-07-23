#!/usr/bin/env python3
"""Collect and validate XGC2 product metadata."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


DEPENDENCY_POLICIES = {"rebuild", "verify", "order"}
DEFAULT_EXCLUSIONS = "catalog/product-exclusions.json"


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit(
            "PyYAML is required. Install it with: python3 -m pip install PyYAML"
        ) from exc

    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: metadata root must be a mapping")
    return data


def load_schema(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_excluded_product_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError(f"{path}: exclusion document root must be an object")
    if document.get("schema") != "xgc2.catalog-exclusions.v1":
        raise ValueError(
            f"{path}: schema must be xgc2.catalog-exclusions.v1"
        )
    products = document.get("products")
    if not isinstance(products, list):
        raise ValueError(f"{path}: products must be an array")

    excluded: set[str] = set()
    for index, entry in enumerate(products):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: products[{index}] must be an object")
        product_id = entry.get("id")
        reason = entry.get("reason")
        if not isinstance(product_id, str) or not product_id:
            raise ValueError(f"{path}: products[{index}].id must be a non-empty string")
        if not isinstance(reason, str) or not reason:
            raise ValueError(
                f"{path}: products[{index}].reason must be a non-empty string"
            )
        if product_id in excluded:
            raise ValueError(f"{path}: duplicate excluded product id: {product_id}")
        excluded.add(product_id)
    return excluded


def validate_schema(product: dict[str, Any], schema: dict[str, Any], path: Path) -> None:
    try:
        import jsonschema
    except ImportError as exc:
        raise SystemExit(
            "jsonschema is required. Install it with: python3 -m pip install jsonschema"
        ) from exc

    try:
        jsonschema.validate(product, schema)
    except jsonschema.ValidationError as exc:
        location = ".".join(str(part) for part in exc.path)
        prefix = f"{path}: "
        if location:
            prefix += f"{location}: "
        raise ValueError(prefix + exc.message) from exc


def iter_metadata_files(root: Path) -> list[Path]:
    ignored = {".git", ".github", ".work", "catalog/generated"}
    files: list[Path] = []
    for candidate in root.rglob(".xgc2/product.yml"):
        relative = candidate.relative_to(root)
        if any(part in ignored for part in relative.parts):
            continue
        files.append(candidate)
    return sorted(files)


def list_field(product: dict[str, Any], *path: str) -> list[str]:
    value: Any = product
    for key in path:
        if not isinstance(value, dict):
            return []
        value = value.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def scalar_field(product: dict[str, Any], *path: str) -> list[str]:
    value: Any = product
    for key in path:
        if not isinstance(value, dict):
            return []
        value = value.get(key)
    if value is None:
        return []
    return [str(value)]


def is_deprecated(product: dict[str, Any]) -> bool:
    lifecycle = product.get("lifecycle")
    return isinstance(lifecycle, dict) and lifecycle.get("deprecated") is True


def validate_non_publishing_catalogs(products: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for product in products:
        if product.get("kind") != "catalog":
            continue
        if list_field(product, "apt", "install") or list_field(product, "apt", "packages"):
            errors.append(
                f"{product['id']}: kind=catalog must not declare apt.install or apt.packages"
            )
    return errors


def find_duplicates(products: list[dict[str, Any]], key_name: str, values_by_product) -> list[str]:
    owners: dict[str, list[str]] = defaultdict(list)
    for product in products:
        if is_deprecated(product):
            continue
        for value in values_by_product(product):
            owners[value].append(str(product["id"]))

    errors: list[str] = []
    for value, product_ids in sorted(owners.items()):
        if len(product_ids) > 1:
            errors.append(
                f"duplicate {key_name} ownership for {value}: {', '.join(product_ids)}"
            )
    return errors


def validate_ownership(products: list[dict[str, Any]]) -> list[str]:
    checks = [
        ("APT package", lambda product: list_field(product, "apt", "packages")),
        ("APT install package", lambda product: list_field(product, "apt", "install")),
        ("ROS package", lambda product: list_field(product, "ros", "packages")),
        ("owned path", lambda product: list_field(product, "ownership", "paths")),
        ("Docker image", lambda product: list_field(product, "docker", "images")),
        ("app-store app id", lambda product: scalar_field(product, "app_store", "app_id")),
    ]

    errors: list[str] = []
    for key_name, getter in checks:
        errors.extend(find_duplicates(products, key_name, getter))
    return errors


def parse_dep_package(dependency: str) -> str:
    return dependency.split(" ", 1)[0].strip()


def is_apt_product(product: dict[str, Any]) -> bool:
    return bool(
        list_field(product, "apt", "packages")
        or list_field(product, "apt", "install")
    )


def validate_internal_dependency_policies(
    products: list[dict[str, Any]],
    *,
    allow_implicit: bool = False,
) -> list[str]:
    """Validate cross-product release impact classifications.

    JSON Schema can validate the policy value vocabulary, but only the full
    catalog can resolve an ``apt.depends`` or ``apt.recommends`` package to its
    owning product and prove that every direct internal edge has an explicit
    classification.
    """

    active = [product for product in products if is_apt_product(product)]
    active_ids = {str(product["id"]) for product in active}
    owners: dict[str, str] = {}
    for product in active:
        product_id = str(product["id"])
        provided = dict.fromkeys(
            [
                *list_field(product, "apt", "packages"),
                *list_field(product, "apt", "install"),
            ]
        )
        for package in provided:
            owners[package] = product_id
        owners.setdefault(product_id, product_id)
        for ros_distro in ("melodic", "noetic", "humble", "jazzy"):
            owners.setdefault(f"ros-{ros_distro}-{product_id}", product_id)

    errors: list[str] = []
    for product in active:
        product_id = str(product["id"])
        direct_upstream: set[str] = set()
        sources: dict[str, set[str]] = {}
        for dependency in list_field(product, "apt", "depends"):
            provider = owners.get(parse_dep_package(dependency))
            if provider and provider != product_id:
                direct_upstream.add(provider)
                sources.setdefault(provider, set()).add("apt.depends")
        for dependency in list_field(product, "apt", "recommends"):
            provider = owners.get(parse_dep_package(dependency))
            if provider and provider != product_id:
                direct_upstream.add(provider)
                sources.setdefault(provider, set()).add("apt.recommends")

        release = product.get("release")
        if not isinstance(release, dict):
            release = {}
        requires = release.get("requires", [])
        if not isinstance(requires, list):
            # Schema validation reports the structural error with its source.
            requires = []
        for raw_provider in requires:
            provider = str(raw_provider)
            if provider not in active_ids:
                errors.append(
                    f"{product_id}: release.requires references unknown APT product "
                    f"{provider}"
                )
                continue
            if provider == product_id:
                errors.append(f"{product_id}: release.requires cannot reference itself")
                continue
            direct_upstream.add(provider)
            sources.setdefault(provider, set()).add("release.requires")

        raw_policies = release.get("dependency_policy", {})
        if not isinstance(raw_policies, dict):
            # Schema validation reports the structural error with its source.
            raw_policies = {}
        policies = {str(provider): str(policy) for provider, policy in raw_policies.items()}
        invalid = sorted(
            f"{provider}={policy}"
            for provider, policy in policies.items()
            if policy not in DEPENDENCY_POLICIES
        )
        if invalid:
            errors.append(
                f"{product_id}: invalid release.dependency_policy: {', '.join(invalid)}"
            )
        unknown = sorted(set(policies) - direct_upstream)
        if unknown:
            errors.append(
                f"{product_id}: release.dependency_policy references non-direct "
                f"upstream product(s): {', '.join(unknown)}"
            )
        if allow_implicit:
            continue
        for provider in sorted(direct_upstream - set(policies)):
            edge_sources = "+".join(sorted(sources[provider]))
            errors.append(
                f"{product_id}: missing release.dependency_policy[{provider}] "
                f"for internal {edge_sources} edge"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".", help="repository root to scan")
    parser.add_argument(
        "--schema",
        default="schemas/product.schema.json",
        help="product metadata JSON schema",
    )
    parser.add_argument(
        "--output",
        default="catalog/generated/products.json",
        help="generated catalog path",
    )
    parser.add_argument(
        "--exclusions",
        default=DEFAULT_EXCLUSIONS,
        help="central list of products excluded from the active catalog",
    )
    parser.add_argument(
        "--allow-implicit-dependency-policy",
        action="store_true",
        help=(
            "temporary migration escape hatch; do not require explicit policies "
            "for every direct internal dependency"
        ),
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    schema_path = (root / args.schema).resolve()
    output_path = (root / args.output).resolve()
    exclusions_path = (root / args.exclusions).resolve()
    schema = load_schema(schema_path)
    excluded_product_ids = load_excluded_product_ids(exclusions_path)

    products: list[dict[str, Any]] = []
    for metadata_path in iter_metadata_files(root):
        product = load_yaml(metadata_path)
        validate_schema(product, schema, metadata_path)
        if str(product["id"]) in excluded_product_ids:
            continue
        product["_source"] = str(metadata_path.relative_to(root))
        products.append(product)

    errors = validate_non_publishing_catalogs(products)
    errors.extend(validate_ownership(products))
    errors.extend(
        validate_internal_dependency_policies(
            products,
            allow_implicit=args.allow_implicit_dependency_policy,
        )
    )
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    if args.allow_implicit_dependency_policy:
        print(
            "warning: implicit internal dependency policies are enabled; "
            "this mode is for migration only",
            file=sys.stderr,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "schema": "xgc2.catalog.v1",
                "products": products,
            },
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")

    print(f"collected {len(products)} products -> {output_path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
