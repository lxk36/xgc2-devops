#!/usr/bin/env python3
"""Collect and validate XGC2 product metadata."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


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
    ignored = {".git", ".github", "catalog/generated"}
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
    args = parser.parse_args()

    root = Path(args.root).resolve()
    schema_path = (root / args.schema).resolve()
    output_path = (root / args.output).resolve()
    schema = load_schema(schema_path)

    products: list[dict[str, Any]] = []
    for metadata_path in iter_metadata_files(root):
        product = load_yaml(metadata_path)
        validate_schema(product, schema, metadata_path)
        product["_source"] = str(metadata_path.relative_to(root))
        products.append(product)

    errors = validate_ownership(products)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

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
