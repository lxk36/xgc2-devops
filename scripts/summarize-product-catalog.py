#!/usr/bin/env python3
"""Generate a Markdown module graph for the XGC2 product catalog."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def normalize_repo(remote: str) -> str:
    remote = remote.strip()
    patterns = (
        r"^git@github\.com:(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^https://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^https://[^@]+@github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, remote)
        if match:
            return match.group("repo")
    return remote


def infer_ref(source_dir: Path) -> str:
    branch = run_git(["branch", "--show-current"], source_dir)
    if branch:
        return branch
    remote_branches = run_git(["branch", "-r", "--contains", "HEAD"], source_dir)
    candidates: list[str] = []
    for line in remote_branches.splitlines():
        candidate = line.strip().lstrip("*").strip()
        if candidate.startswith("origin/") and candidate != "origin/HEAD":
            candidates.append(candidate.split("/", 1)[1])
    for preferred in ("noetic", "master", "main"):
        if preferred in candidates:
            return preferred
    if len(candidates) == 1:
        return candidates[0]
    remote_head = run_git(
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        source_dir,
    )
    if remote_head.startswith("origin/"):
        return remote_head.split("/", 1)[1]
    return "HEAD"


def node_id(value: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", value)


def mermaid_label(value: str) -> str:
    return value.replace('"', "'")


def list_field(data: dict[str, Any], *path: str) -> list[str]:
    value: Any = data
    for key in path:
        if not isinstance(value, dict):
            return []
        value = value.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


@dataclass(frozen=True)
class ProductSummary:
    product_id: str
    name: str
    kind: str
    version: str
    source: str
    source_dir: Path
    repo: str
    ref: str
    apt_packages: tuple[str, ...]

    @property
    def path_parts(self) -> tuple[str, ...]:
        return tuple(self.source_dir.parts)


def load_products(root: Path, catalog_path: Path) -> list[ProductSummary]:
    with catalog_path.open("r", encoding="utf-8") as handle:
        catalog = json.load(handle)

    products: list[ProductSummary] = []
    for item in catalog.get("products", []):
        source = str(item["_source"])
        source_file = root / source
        source_dir = source_file.parent.parent.relative_to(root)
        absolute_source_dir = root / source_dir
        remote = normalize_repo(run_git(["remote", "get-url", "origin"], absolute_source_dir))
        products.append(
            ProductSummary(
                product_id=str(item["id"]),
                name=str(item.get("name", item["id"])),
                kind=str(item.get("kind", "")),
                version=str(item.get("version", "")),
                source=source,
                source_dir=source_dir,
                repo=remote or "(local)",
                ref=infer_ref(absolute_source_dir),
                apt_packages=tuple(list_field(item, "apt", "packages")),
            )
        )
    return sorted(products, key=lambda product: product.source)


def path_label(part: str) -> str:
    return part.replace("_", "_")


def product_node_label(product: ProductSummary) -> str:
    lines = [
        product.source_dir.name,
        product.product_id,
    ]
    if product.version:
        lines.append(product.version)
    lines.append(f"{product.repo}@{product.ref}")
    return "\\n".join(lines)


def module_tree_mermaid(products: list[ProductSummary]) -> str:
    lines = [
        "```mermaid",
        "flowchart TD",
        '  root["xgc2-devops"]',
    ]
    emitted_nodes = {"root"}
    emitted_edges: set[tuple[str, str]] = set()

    def emit_node(path_key: str, label: str) -> None:
        identifier = node_id(path_key)
        if identifier not in emitted_nodes:
            lines.append(f'  {identifier}["{mermaid_label(label)}"]')
            emitted_nodes.add(identifier)

    def emit_edge(parent: str, child: str) -> None:
        edge = (parent, child)
        if edge not in emitted_edges:
            lines.append(f"  {parent} --> {child}")
            emitted_edges.add(edge)

    product_by_path = {product.source_dir.as_posix(): product for product in products}
    all_paths: set[str] = set()
    for product in products:
        parts: list[str] = []
        for part in product.path_parts:
            parts.append(part)
            all_paths.add("/".join(parts))

    for path_key in sorted(all_paths):
        product = product_by_path.get(path_key)
        if product:
            label = product_node_label(product)
        else:
            label = path_label(Path(path_key).name)
        emit_node(path_key, label)

        parent_path = str(Path(path_key).parent)
        parent_id = "root" if parent_path == "." else node_id(parent_path)
        emit_edge(parent_id, node_id(path_key))

    lines.append("```")
    return "\n".join(lines)


def product_table(products: list[ProductSummary]) -> str:
    lines = [
        "| Product | Kind | Version | Repository | Source |",
        "| --- | --- | --- | --- | --- |",
    ]
    for product in products:
        lines.append(
            "| "
            f"`{product.product_id}` | "
            f"{product.kind} | "
            f"{product.version or '-'} | "
            f"`{product.repo}@{product.ref}` | "
            f"`{product.source_dir.as_posix()}` |"
        )
    return "\n".join(lines)


def summary_markdown(products: list[ProductSummary]) -> str:
    repos = {product.repo for product in products}
    apt_products = [product for product in products if product.apt_packages]
    lines = [
        "# XGC2 Product Module Catalog",
        "",
        "## Inventory",
        "",
        f"- Products: `{len(products)}`",
        f"- APT products: `{len(apt_products)}`",
        f"- Source repositories: `{len(repos)}`",
        "",
        "## Module Graph",
        "",
        module_tree_mermaid(products),
        "",
        "## Products",
        "",
        product_table(products),
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="xgc2-devops repository root")
    parser.add_argument("--catalog", required=True, help="collect-products JSON output")
    parser.add_argument("--output", required=True, help="Markdown summary output")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    catalog_path = (root / args.catalog).resolve()
    output_path = (root / args.output).resolve()
    products = load_products(root, catalog_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(summary_markdown(products), encoding="utf-8")
    print(f"wrote {output_path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
