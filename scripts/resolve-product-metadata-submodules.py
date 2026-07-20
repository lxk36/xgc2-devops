#!/usr/bin/env python3
"""Resolve tracked product catalog sources to root-repository gitlinks."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


PRODUCT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
CATALOG_SCHEMA = "xgc2.catalog.v1"
DEFAULT_CATALOG = "catalog/generated/products.json"


class ResolutionError(ValueError):
    """The tracked catalog cannot be resolved safely to checkout roots."""


@dataclass(frozen=True)
class ProductSource:
    product_id: str
    source: str
    gitlink: str


@dataclass(frozen=True)
class Resolution:
    catalog: str
    submodules: tuple[str, ...]
    sources: tuple[ProductSource, ...]


def run_git(root: Path, args: list[str], *, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
    )
    if result.returncode != 0:
        stderr = (
            result.stderr.decode("utf-8", errors="replace")
            if isinstance(result.stderr, bytes)
            else result.stderr
        ).strip()
        command = " ".join(["git", *args])
        raise ResolutionError(f"{command} failed: {stderr or 'unknown git error'}")
    return result.stdout


def repository_root(root: Path) -> Path:
    output = run_git(root.resolve(), ["rev-parse", "--show-toplevel"])
    assert isinstance(output, str)
    return Path(output.strip()).resolve()


def catalog_relative_path(root: Path, catalog: str) -> str:
    candidate = Path(catalog)
    absolute = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise ResolutionError(f"catalog must be inside repository: {absolute}") from exc
    value = relative.as_posix()
    if not value or value == ".":
        raise ResolutionError("catalog path must name a file")
    return value


def tracked_catalog(root: Path, relative_path: str) -> dict[str, Any]:
    run_git(root, ["ls-files", "--error-unmatch", "--", relative_path])
    raw = run_git(root, ["show", f":{relative_path}"])
    assert isinstance(raw, str)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResolutionError(
            f"tracked catalog {relative_path} is invalid JSON: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise ResolutionError(f"tracked catalog {relative_path} root must be an object")
    if value.get("schema") != CATALOG_SCHEMA:
        raise ResolutionError(
            f"tracked catalog {relative_path} must use schema {CATALOG_SCHEMA}"
        )
    products = value.get("products")
    if not isinstance(products, list) or not products:
        raise ResolutionError(f"tracked catalog {relative_path} products must be non-empty")
    return value


def validate_source(product_id: str, source: object) -> str:
    if not isinstance(source, str) or not source:
        raise ResolutionError(f"{product_id}: catalog _source must be a non-empty string")
    if "\\" in source or any(ord(character) < 32 for character in source):
        raise ResolutionError(f"{product_id}: catalog _source is not a safe POSIX path: {source!r}")
    path = PurePosixPath(source)
    parts = path.parts
    if (
        path.is_absolute()
        or path.as_posix() != source
        or any(part in {"", ".", ".."} for part in parts)
        or not parts
        or parts[0] != "products"
        or parts[-2:] != (".xgc2", "product.yml")
    ):
        raise ResolutionError(
            f"{product_id}: catalog _source must be a normalized "
            f"products/**/.xgc2/product.yml path: {source!r}"
        )
    return source


def catalog_sources(catalog: dict[str, Any]) -> list[tuple[str, str]]:
    seen_ids: set[str] = set()
    seen_sources: set[str] = set()
    result: list[tuple[str, str]] = []
    for index, item in enumerate(catalog["products"]):
        if not isinstance(item, dict):
            raise ResolutionError(f"catalog products[{index}] must be an object")
        product_id = item.get("id")
        if not isinstance(product_id, str) or not PRODUCT_ID.fullmatch(product_id):
            raise ResolutionError(f"catalog products[{index}].id is invalid")
        if product_id in seen_ids:
            raise ResolutionError(f"duplicate catalog product id: {product_id}")
        source = validate_source(product_id, item.get("_source"))
        if source in seen_sources:
            raise ResolutionError(f"duplicate catalog _source: {source}")
        seen_ids.add(product_id)
        seen_sources.add(source)
        result.append((product_id, source))
    return result


def validate_gitlink_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if (
        "\\" in path
        or any(ord(character) < 32 for character in path)
        or candidate.is_absolute()
        or candidate.as_posix() != path
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or not candidate.parts
        or candidate.parts[0] != "products"
    ):
        raise ResolutionError(f"unsafe product gitlink path in repository index: {path!r}")
    return path


def indexed_gitlinks(root: Path) -> tuple[str, ...]:
    raw = run_git(root, ["ls-files", "--stage", "-z", "--", "products"], binary=True)
    assert isinstance(raw, bytes)
    gitlinks: list[str] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, _object_id, stage = metadata.split(b" ", 2)
        except ValueError as exc:
            raise ResolutionError("git ls-files returned an invalid index record") from exc
        path = validate_gitlink_path(raw_path.decode("utf-8", errors="strict"))
        if stage != b"0":
            raise ResolutionError(f"unmerged product index entry: {path}")
        if mode == b"160000":
            gitlinks.append(path)
    if not gitlinks:
        raise ResolutionError("repository index has no product gitlinks")
    return tuple(sorted(gitlinks))


def configured_submodule_paths(root: Path) -> set[str]:
    run_git(root, ["ls-files", "--error-unmatch", "--", ".gitmodules"])
    output = run_git(
        root,
        ["config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
    )
    assert isinstance(output, str)
    paths: set[str] = set()
    for line in output.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) != 2 or not fields[1]:
            raise ResolutionError(".gitmodules contains an invalid submodule path entry")
        paths.add(fields[1])
    if not paths:
        raise ResolutionError(".gitmodules contains no submodule paths")
    return paths


def resolve(root: Path, catalog_path: str = DEFAULT_CATALOG) -> Resolution:
    root = repository_root(root)
    relative_catalog = catalog_relative_path(root, catalog_path)
    catalog = tracked_catalog(root, relative_catalog)
    sources = catalog_sources(catalog)
    gitlinks = indexed_gitlinks(root)
    configured = configured_submodule_paths(root)

    resolved: list[ProductSource] = []
    selected: set[str] = set()
    for product_id, source in sources:
        matches = [
            gitlink
            for gitlink in gitlinks
            if source == gitlink or source.startswith(f"{gitlink}/")
        ]
        if not matches:
            raise ResolutionError(
                f"{product_id}: catalog source {source} is not contained by a tracked gitlink"
            )
        if len(matches) != 1:
            raise ResolutionError(
                f"{product_id}: catalog source {source} maps to ambiguous gitlinks: "
                + ", ".join(matches)
            )
        gitlink = matches[0]
        if gitlink not in configured:
            raise ResolutionError(
                f"{product_id}: gitlink {gitlink} has no matching .gitmodules path"
            )
        selected.add(gitlink)
        resolved.append(ProductSource(product_id, source, gitlink))

    return Resolution(
        catalog=relative_catalog,
        submodules=tuple(sorted(selected)),
        sources=tuple(sorted(resolved, key=lambda item: (item.source, item.product_id))),
    )


def verify_checkout(root: Path, resolution: Resolution) -> None:
    missing = [item for item in resolution.sources if not (root / item.source).is_file()]
    if missing:
        details = "\n".join(
            f"  - {item.product_id}: {item.source} (root {item.gitlink})"
            for item in missing
        )
        raise ResolutionError(
            "checked-out submodules do not provide every tracked catalog source:\n"
            f"{details}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="xgc2-devops repository root")
    parser.add_argument(
        "--catalog",
        default=DEFAULT_CATALOG,
        help="tracked product catalog path",
    )
    parser.add_argument(
        "--verify-checkout",
        action="store_true",
        help="also require every catalog _source file to exist after checkout",
    )
    args = parser.parse_args()

    try:
        root = repository_root(Path(args.root))
        resolution = resolve(root, args.catalog)
        if args.verify_checkout:
            verify_checkout(root, resolution)
    except (OSError, UnicodeError, ResolutionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for submodule in resolution.submodules:
        print(submodule)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
