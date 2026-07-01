#!/usr/bin/env python3
"""Write GitHub Actions matrix outputs for product catalog modules."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
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
    return result.stdout.strip() if result.returncode == 0 else ""


def normalize_repo(remote: str) -> str:
    patterns = (
        r"^git@github\.com:(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^https://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^https://[^@]+@github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, remote.strip())
        if match:
            return match.group("repo")
    return remote.strip() or "(local)"


def infer_ref(source_dir: Path) -> str:
    branch = run_git(["branch", "--show-current"], source_dir)
    if branch:
        return branch
    remote_branches = run_git(["branch", "-r", "--contains", "HEAD"], source_dir)
    candidates = []
    for line in remote_branches.splitlines():
        candidate = line.strip().lstrip("*").strip()
        if candidate.startswith("origin/") and candidate != "origin/HEAD":
            candidates.append(candidate.split("/", 1)[1])
    for preferred in ("noetic", "master", "main"):
        if preferred in candidates:
            return preferred
    if len(candidates) == 1:
        return candidates[0]
    return "HEAD"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--catalog", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    catalog_path = (root / args.catalog).resolve()
    with catalog_path.open("r", encoding="utf-8") as handle:
        catalog: dict[str, Any] = json.load(handle)

    rows: list[dict[str, str]] = []
    for product in sorted(catalog.get("products", []), key=lambda item: str(item["_source"])):
        source = str(product["_source"])
        source_dir = (root / source).parent.parent
        rows.append(
            {
                "product_id": str(product["id"]),
                "kind": str(product.get("kind", "")),
                "version": str(product.get("version", "")),
                "repository": normalize_repo(run_git(["remote", "get-url", "origin"], source_dir)),
                "ref": infer_ref(source_dir),
                "source": source_dir.relative_to(root).as_posix(),
            }
        )

    print(f"products_count={len(rows)}")
    print(f"products_matrix={json.dumps({'include': rows}, separators=(',', ':'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
