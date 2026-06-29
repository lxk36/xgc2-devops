#!/usr/bin/env python3
"""Check ROS1 product packages against the ros1_dev pre-product tree."""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


IGNORED_PARTS = {
    ".git",
    ".github",
    ".work",
    "__pycache__",
    "build",
    "devel",
    "extract",
    "install",
    "install-root",
    "log",
    "logs",
    "node_modules",
}


def is_ignored(path: Path) -> bool:
    return any(part in IGNORED_PARTS for part in path.parts)


def read_package_name(package_xml: Path) -> str:
    try:
        root = ET.parse(package_xml).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"{package_xml}: invalid XML: {exc}") from exc

    name = root.findtext("name")
    if not name:
        raise ValueError(f"{package_xml}: missing <name>")
    return name.strip()


def collect_packages(root: Path) -> dict[str, list[Path]]:
    packages: dict[str, list[Path]] = {}
    if not root.exists():
        return packages

    for package_xml in sorted(root.rglob("package.xml")):
        relative = package_xml.relative_to(root)
        if is_ignored(relative):
            continue
        name = read_package_name(package_xml)
        packages.setdefault(name, []).append(package_xml)
    return packages


def read_name_list(path: Path) -> set[str]:
    names: set[str] = set()
    if not path.exists():
        return names

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            names.add(line)
    return names


def is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail if products/ros1 and products/ros1_dev/src contain the same "
            "ROS package name outside the explicit ros1_dev pre-product tree."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="xgc2-devops repository root",
    )
    parser.add_argument(
        "--product-root",
        type=Path,
        default=Path("products/ros1"),
        help="ROS1 product tree, relative to --root unless absolute",
    )
    parser.add_argument(
        "--dev-root",
        type=Path,
        default=Path("products/ros1_dev/src"),
        help="ROS1 development source tree, relative to --root unless absolute",
    )
    parser.add_argument(
        "--pre-product-root",
        type=Path,
        default=Path("products/ros1_dev/src/pre_product"),
        help="Allowed ros1_dev pre-product tree, relative to --root unless absolute",
    )
    parser.add_argument(
        "--pre-product-allowlist",
        type=Path,
        default=Path("products/ros1_dev/config/pre_product_packages.txt"),
        help="Newline-delimited package names allowed to shadow products/ros1",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    product_root = args.product_root
    dev_root = args.dev_root
    pre_product_root = args.pre_product_root
    pre_product_allowlist = args.pre_product_allowlist
    if not product_root.is_absolute():
        product_root = root / product_root
    if not dev_root.is_absolute():
        dev_root = root / dev_root
    if not pre_product_root.is_absolute():
        pre_product_root = root / pre_product_root
    if not pre_product_allowlist.is_absolute():
        pre_product_allowlist = root / pre_product_allowlist

    try:
        product_packages = collect_packages(product_root)
        dev_packages = collect_packages(dev_root)
        pre_product_package_names = read_name_list(pre_product_allowlist)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    duplicates = sorted(set(product_packages) & set(dev_packages))
    violations: list[str] = []
    allowed_pre_products: list[str] = []
    for name in duplicates:
        dev_paths = dev_packages[name]
        dev_is_only_pre_product = all(is_under(path, pre_product_root) for path in dev_paths)
        if name in pre_product_package_names and dev_is_only_pre_product:
            allowed_pre_products.append(name)
        else:
            violations.append(name)

    if violations:
        print("ROS1 product/dev package boundary violation:", file=sys.stderr)
        for name in violations:
            print(f"- {name}", file=sys.stderr)
            for path in product_packages[name]:
                print(f"  product: {path}", file=sys.stderr)
            for path in dev_packages[name]:
                print(f"  dev:     {path}", file=sys.stderr)
        print(
            "\nKeep active product packages in products/ros1 and consume them from "
            "ros1_dev through their APT package. Only keep source in ros1_dev "
            "when that package is not productized or is the active iteration owner.",
            file=sys.stderr,
        )
        if pre_product_package_names:
            print(
                f"\nAllowed active pre-product sources must be listed in {pre_product_allowlist} "
                f"and live under {pre_product_root}.",
                file=sys.stderr,
            )
        return 1

    if allowed_pre_products:
        print("Allowed ROS1 dev pre-product sources:")
        for name in allowed_pre_products:
            print(f"- {name}")
        print("No invalid duplicate ROS1 package names across products/ros1 and ros1_dev/src.")
    else:
        print("No duplicate ROS1 package names across products/ros1 and ros1_dev/src.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
