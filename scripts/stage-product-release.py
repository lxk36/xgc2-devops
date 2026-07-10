#!/usr/bin/env python3
"""Validate trusted CI artifacts and assemble deterministic release-train bundles."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


BUILD_SCHEMA = "xgc2.build-artifact.v1"
RELEASE_SCHEMA = "xgc2.release-artifact.v1"
RECEIPT_SCHEMA = "xgc2.stage-receipt.v1"
TRAIN_SCHEMA = "xgc2.release-train.v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
ARCHES = ("amd64", "arm64")


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def directory_digest(root: Path) -> str:
    """Hash directory content independently of tar ownership and mtimes."""

    digest = hashlib.sha256()
    paths = sorted(path for path in root.rglob("*") if path.is_file())
    if not paths:
        raise ValueError(f"release bundle is empty: {root}")
    for path in paths:
        relative = path.relative_to(root).as_posix()
        digest.update(f"{sha256(path)}  {relative}\n".encode("utf-8"))
    return digest.hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON document must be an object: {path}")
    return value


def plan_items(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["id"]): item
        for layer in plan.get("layers", [])
        for item in layer
    }


def expected_package_architectures(product: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    packages = [str(value) for value in product.get("apt_packages", [])]
    raw = product.get("apt_package_architectures", {})
    if not isinstance(raw, dict):
        raise ValueError(f"{product.get('id', '<unknown>')}: invalid package architecture map")
    unknown = sorted(set(map(str, raw)) - set(packages))
    if unknown:
        raise ValueError(
            f"{product.get('id', '<unknown>')}: package architecture map has unknown package(s): "
            + ", ".join(unknown)
        )
    result: dict[str, tuple[str, ...]] = {}
    for package in packages:
        values = raw.get(package, ARCHES)
        if (
            not isinstance(values, (list, tuple))
            or not values
            or any(str(arch) not in ARCHES for arch in values)
        ):
            raise ValueError(
                f"{product.get('id', '<unknown>')}: invalid architectures for {package}"
            )
        result[package] = tuple(arch for arch in ARCHES if arch in set(map(str, values)))
    install = set(map(str, product.get("apt_install", [])))
    narrowed = sorted(
        package for package in install if set(result.get(package, ARCHES)) != set(ARCHES)
    )
    if narrowed:
        raise ValueError(
            f"{product.get('id', '<unknown>')}: apt.install package(s) are not dual-arch: "
            + ", ".join(narrowed)
        )
    return result


def expected_package_distributions(product: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    packages = [str(value) for value in product.get("apt_packages", [])]
    distributions = tuple(map(str, product.get("apt_distributions", [])))
    raw = product.get("apt_package_distributions", {})
    if not isinstance(raw, dict):
        raise ValueError(f"{product.get('id', '<unknown>')}: invalid package distribution map")
    unknown = sorted(set(map(str, raw)) - set(packages))
    if unknown:
        raise ValueError(
            f"{product.get('id', '<unknown>')}: package distribution map has unknown package(s): "
            + ", ".join(unknown)
        )
    result: dict[str, tuple[str, ...]] = {}
    for package in packages:
        values = raw.get(package, distributions)
        if (
            not isinstance(values, (list, tuple))
            or not values
            or any(str(value) not in distributions for value in values)
        ):
            raise ValueError(
                f"{product.get('id', '<unknown>')}: invalid distributions for {package}"
            )
        result[package] = tuple(
            distribution
            for distribution in distributions
            if distribution in set(map(str, values))
        )
    install = set(map(str, product.get("apt_install", [])))
    missing = sorted(
        distribution
        for distribution in distributions
        if not any(distribution in result.get(package, distributions) for package in install)
    )
    if missing:
        raise ValueError(
            f"{product.get('id', '<unknown>')}: no install package for distribution(s): "
            + ", ".join(missing)
        )
    return result


def deb_metadata(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "dpkg-deb",
            "--show",
            "--showformat=${Package}\n${Version}\n${Architecture}\n",
            os.fspath(path),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    fields = result.stdout.splitlines()
    if len(fields) != 3 or not all(fields):
        raise ValueError(f"invalid Debian metadata: {path}")
    package, version, architecture = fields
    return {
        "file": path.name,
        "package": package,
        "version": version,
        "architecture": architecture,
        "sha256": sha256(path),
        "size": path.stat().st_size,
    }


def find_deb(root: Path, manifest_path: Path, filename: str) -> Path:
    if not filename or Path(filename).name != filename:
        raise ValueError(f"unsafe deb filename in {manifest_path}: {filename!r}")
    root = root.resolve()
    scope = manifest_path.resolve().parent
    while scope == root or root in scope.parents:
        matches = sorted(path for path in scope.rglob(filename) if path.is_file())
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"ambiguous {filename} below {scope}")
        if scope == root:
            break
        scope = scope.parent
    raise ValueError(f"manifest references missing deb {filename}: {manifest_path}")


def validate_entry(path: Path, declared: dict[str, Any]) -> dict[str, Any]:
    actual = deb_metadata(path)
    if declared != actual:
        raise ValueError(
            f"deb metadata or SHA256 mismatch for {path.name}: "
            f"declared={declared!r} actual={actual!r}"
        )
    return actual


def published_at(value: str) -> str:
    if not value:
        return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--published-at must include a timezone")
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def prepare(args: argparse.Namespace) -> int:
    plan = load_json(Path(args.plan))
    product = plan_items(plan).get(args.product)
    if product is None:
        raise ValueError(f"product is not in plan: {args.product}")
    if product.get("action") != "release":
        raise ValueError(f"{args.product}: only release targets produce staged bundles")
    if HEX64.fullmatch(args.release_lock_digest) is None:
        raise ValueError("--release-lock-digest must be a lowercase SHA256")
    expected_source = str(product.get("expected_source_sha", ""))
    if SOURCE_SHA.fullmatch(expected_source) is None:
        raise ValueError(f"{args.product}: invalid expected source SHA")
    expected_product_version = str(product.get("expected_version") or product.get("version", ""))
    expected_packages = {str(value) for value in product.get("apt_packages", [])}
    package_arches = expected_package_architectures(product)
    package_distributions = expected_package_distributions(product)
    expected_versions = {
        str(key): str(value)
        for key, value in (product.get("apt_versions") or {}).items()
    }
    required = {
        (distribution, arch)
        for distribution in product.get("apt_distributions", [])
        for arch in ARCHES
    }
    artifact_root = Path(args.artifact_dir).resolve(strict=True)
    selected: dict[tuple[str, str], list[tuple[Path, dict[str, Any], list[tuple[Path, dict[str, Any]]]]]] = {}
    package_identity_digests: dict[tuple[str, str, str], str] = {}
    seen_manifest = False
    for manifest_path in sorted(artifact_root.rglob("*.json")):
        try:
            manifest = load_json(manifest_path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        if manifest.get("schema") != BUILD_SCHEMA or manifest.get("product") != args.product:
            continue
        seen_manifest = True
        identity = (
            str(manifest.get("source_sha", "")),
            str(manifest.get("version", "")),
        )
        if identity != (expected_source, expected_product_version):
            raise ValueError(f"build manifest identity mismatch: {manifest_path}")
        distribution = str(manifest.get("distribution", ""))
        architecture = str(manifest.get("architecture", ""))
        if (distribution, architecture) not in required:
            raise ValueError(
                f"unexpected distribution/architecture in {manifest_path}: "
                f"{distribution}/{architecture}"
            )
        ci = manifest.get("ci")
        if not isinstance(ci, dict) or str(ci.get("run_id", "")) != str(args.run_id):
            raise ValueError(f"build manifest CI run mismatch: {manifest_path}")
        if not all(str(ci.get(key, "")) for key in ("workflow", "workflow_ref")):
            raise ValueError(f"build manifest has incomplete CI identity: {manifest_path}")
        entries = manifest.get("debs")
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"build manifest debs must be non-empty: {manifest_path}")
        resolved: list[tuple[Path, dict[str, Any]]] = []
        for declared in entries:
            if not isinstance(declared, dict):
                raise ValueError(f"invalid deb entry: {manifest_path}")
            deb = find_deb(artifact_root, manifest_path, str(declared.get("file", "")))
            actual = validate_entry(deb, declared)
            if actual["architecture"] not in {architecture, "all"}:
                raise ValueError(f"deb architecture mismatch: {deb}")
            if actual["version"] != expected_versions.get(distribution):
                raise ValueError(
                    f"{deb.name}: expected version {expected_versions.get(distribution)!r}, "
                    f"found {actual['version']!r}"
                )
            identity = (actual["package"], actual["version"], actual["architecture"])
            old_digest = package_identity_digests.setdefault(identity, actual["sha256"])
            if old_digest != actual["sha256"]:
                raise ValueError(
                    "immutable package identity has different SHA256 values: "
                    f"{identity} ({old_digest} != {actual['sha256']})"
                )
            resolved.append((deb, actual))
        selected.setdefault((distribution, architecture), []).append(
            (manifest_path, manifest, resolved)
        )
    if not seen_manifest:
        raise ValueError(f"{args.product}: no trusted build manifests found")
    missing = sorted(required - set(selected))
    if missing:
        raise ValueError(
            f"{args.product}: missing trusted build coverage: "
            + ", ".join(f"{dist}/{arch}" for dist, arch in missing)
        )
    for key, manifests in selected.items():
        packages = {
            str(entry["package"])
            for _path, _manifest, resolved in manifests
            for _deb, entry in resolved
        }
        expected_for_arch = {
            package
            for package, arches in package_arches.items()
            if key[1] in arches and key[0] in package_distributions[package]
        }
        if packages != expected_for_arch:
            raise ValueError(
                f"{args.product}: package coverage mismatch for {key[0]}/{key[1]}; "
                f"expected={sorted(expected_for_arch)} actual={sorted(packages)}"
            )
    covered_packages = {
        str(entry["package"])
        for manifests in selected.values()
        for _path, _manifest, resolved in manifests
        for _deb, entry in resolved
    }
    if covered_packages != expected_packages:
        raise ValueError(
            f"{args.product}: union package coverage mismatch; "
            f"expected={sorted(expected_packages)} actual={sorted(covered_packages)}"
        )

    output_root = Path(args.output_dir)
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True)
    receipts: list[dict[str, Any]] = []
    timestamp = published_at(args.published_at)
    for distribution in sorted(expected_versions):
        bundle = output_root / distribution
        bundle.mkdir(parents=True)
        build_digests: set[str] = set()
        train_debs: dict[tuple[str, str, str, str], dict[str, str]] = {}
        copied_debs: dict[str, str] = {}
        for arch in ARCHES:
            for manifest_path, manifest, resolved in selected[(distribution, arch)]:
                manifest_digest = sha256(manifest_path)
                included_name = f"{manifest_digest[:16]}-{manifest_path.name}"
                included = bundle / "build-manifests" / included_name
                included.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(manifest_path, included)
                build_digests.add(manifest_digest)
                for deb, entry in resolved:
                    previous_digest = copied_debs.get(deb.name)
                    if previous_digest and previous_digest != entry["sha256"]:
                        raise ValueError(f"duplicate deb filename with different content: {deb.name}")
                    if not previous_digest:
                        shutil.copy2(deb, bundle / deb.name)
                        copied_debs[deb.name] = str(entry["sha256"])
                    train_entry = {
                        key: str(entry[key])
                        for key in ("package", "version", "architecture", "sha256")
                    }
                    train_debs[tuple(train_entry.values())] = train_entry
                release_manifest = {
                    **{
                        key: manifest[key]
                        for key in (
                            "product",
                            "version",
                            "source_sha",
                            "distribution",
                            "architecture",
                            "ci",
                            "debs",
                        )
                    },
                    "schema": RELEASE_SCHEMA,
                    "release_id": args.release_id,
                    "release_lock_digest": args.release_lock_digest,
                    "build_manifest": included.relative_to(bundle).as_posix(),
                    "build_manifest_digest": manifest_digest,
                    "published_at": timestamp,
                }
                for _deb, entry in resolved:
                    output = (
                        bundle
                        / "manifests"
                        / args.product
                        / distribution
                        / arch
                        / f"{entry['package']}_{entry['version']}.json"
                    )
                    write_json(output, release_manifest)
        receipts.append(
            {
                "product": args.product,
                "distribution": distribution,
                "version": expected_product_version,
                "source_sha": expected_source,
                "bundle_dir": bundle.resolve().as_posix(),
                "bundle_digest": directory_digest(bundle),
                "build_manifest_digests": sorted(build_digests),
                "debs": sorted(
                    train_debs.values(),
                    key=lambda item: (
                        item["package"], item["version"], item["architecture"], item["sha256"]
                    ),
                ),
            }
        )
    write_json(
        Path(args.receipt),
        {
            "schema": RECEIPT_SCHEMA,
            "release_id": args.release_id,
            "release_lock_digest": args.release_lock_digest,
            "product": args.product,
            "run_id": int(args.run_id),
            "products": receipts,
        },
    )
    return 0


def create_train(args: argparse.Namespace) -> int:
    plan = load_json(Path(args.plan))
    items_by_id = plan_items(plan)
    plan_digest = canonical_digest(plan)
    if args.plan_digest and args.plan_digest != plan_digest:
        raise ValueError("release plan digest mismatch")
    expected = {
        str(item["id"])
        for item in items_by_id.values()
        if item.get("action") == "release"
    }
    receipts_by_product: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(args.receipt_dir).glob("*.json")):
        receipt = load_json(path)
        if receipt.get("schema") != RECEIPT_SCHEMA:
            continue
        if receipt.get("release_id") != args.release_id:
            raise ValueError(f"receipt release id mismatch: {path}")
        if receipt.get("release_lock_digest") != args.release_lock_digest:
            raise ValueError(f"receipt release lock mismatch: {path}")
        product_id = str(receipt.get("product", ""))
        if product_id in receipts_by_product:
            raise ValueError(f"duplicate stage receipt for {product_id}")
        receipts_by_product[product_id] = receipt
    if set(receipts_by_product) != expected:
        raise ValueError(
            "stage receipt set differs from release targets; "
            f"missing={sorted(expected - set(receipts_by_product))} "
            f"extra={sorted(set(receipts_by_product) - expected)}"
        )
    products: list[dict[str, Any]] = []
    for product_id in sorted(expected):
        receipt_products = receipts_by_product[product_id].get("products")
        if not isinstance(receipt_products, list) or not receipt_products:
            raise ValueError(f"empty stage receipt for {product_id}")
        for item in receipt_products:
            if not isinstance(item, dict):
                raise ValueError(f"invalid stage receipt product for {product_id}")
            planned = items_by_id[product_id]
            identity = {
                "product": product_id,
                "version": str(planned.get("expected_version") or planned.get("version", "")),
                "source_sha": str(planned.get("expected_source_sha", "")),
            }
            if any(item.get(key) != value for key, value in identity.items()):
                raise ValueError(f"stage receipt identity mismatch for {product_id}")
            distribution = str(item.get("distribution", ""))
            if distribution not in set(map(str, planned.get("apt_distributions", []))):
                raise ValueError(f"unexpected receipt distribution for {product_id}: {distribution}")
            if HEX64.fullmatch(str(item.get("bundle_digest", ""))) is None:
                raise ValueError(f"invalid bundle digest for {product_id}/{distribution}")
            build_digests = item.get("build_manifest_digests")
            if (
                not isinstance(build_digests, list)
                or not build_digests
                or any(HEX64.fullmatch(str(value)) is None for value in build_digests)
            ):
                raise ValueError(f"invalid build manifest digests for {product_id}/{distribution}")
            debs = item.get("debs")
            if not isinstance(debs, list) or not debs:
                raise ValueError(f"empty deb identity list for {product_id}/{distribution}")
            normalized_debs: list[dict[str, str]] = []
            identity_digests: dict[tuple[str, str, str], str] = {}
            for deb in debs:
                if not isinstance(deb, dict) or set(deb) != {
                    "package", "version", "architecture", "sha256"
                }:
                    raise ValueError(f"invalid deb identity for {product_id}/{distribution}")
                normalized = {key: str(deb[key]) for key in (
                    "package", "version", "architecture", "sha256"
                )}
                if not all(normalized.values()) or HEX64.fullmatch(normalized["sha256"]) is None:
                    raise ValueError(f"invalid deb identity for {product_id}/{distribution}")
                deb_identity = (
                    normalized["package"], normalized["version"], normalized["architecture"]
                )
                old_digest = identity_digests.setdefault(deb_identity, normalized["sha256"])
                if old_digest != normalized["sha256"]:
                    raise ValueError(
                        "immutable package identity has different SHA256 values for "
                        f"{product_id}/{distribution}: {deb_identity}"
                    )
                normalized_debs.append(normalized)
            products.append(
                {
                    **identity,
                    "distribution": distribution,
                    "bundle_digest": str(item["bundle_digest"]),
                    "build_manifest_digests": sorted(set(map(str, build_digests))),
                    "debs": sorted(
                        normalized_debs,
                        key=lambda deb: (
                            deb["package"], deb["version"], deb["architecture"], deb["sha256"]
                        ),
                    ),
                }
            )
    actual_distributions = {
        product_id: {
            str(item["distribution"])
            for item in products
            if item["product"] == product_id
        }
        for product_id in expected
    }
    identities = [(item["product"], item["distribution"]) for item in products]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate product/distribution in stage receipts")
    for product_id, distributions in actual_distributions.items():
        planned_distributions = set(map(str, items_by_id[product_id].get("apt_distributions", [])))
        if distributions != planned_distributions:
            raise ValueError(
                f"stage receipt distribution coverage mismatch for {product_id}; "
                f"expected={sorted(planned_distributions)} actual={sorted(distributions)}"
            )
    products.sort(key=lambda item: (str(item["product"]), str(item["distribution"])))
    train = {
        "schema": TRAIN_SCHEMA,
        "release_id": args.release_id,
        "plan_digest": plan_digest,
        "release_lock_digest": args.release_lock_digest,
        "products": products,
    }
    write_json(Path(args.output), train)
    print(canonical_digest(train))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--plan", required=True)
    prepare_parser.add_argument("--product", required=True)
    prepare_parser.add_argument("--artifact-dir", required=True)
    prepare_parser.add_argument("--run-id", required=True, type=int)
    prepare_parser.add_argument("--release-id", required=True)
    prepare_parser.add_argument("--release-lock-digest", required=True)
    prepare_parser.add_argument("--published-at", default="")
    prepare_parser.add_argument("--output-dir", required=True)
    prepare_parser.add_argument("--receipt", required=True)
    prepare_parser.set_defaults(handler=prepare)

    train = sub.add_parser("train")
    train.add_argument("--plan", required=True)
    train.add_argument("--receipt-dir", required=True)
    train.add_argument("--release-id", required=True)
    train.add_argument("--release-lock-digest", required=True)
    train.add_argument("--plan-digest", default="")
    train.add_argument("--output", required=True)
    train.set_defaults(handler=create_train)
    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
