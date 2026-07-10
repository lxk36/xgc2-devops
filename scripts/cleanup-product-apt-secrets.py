#!/usr/bin/env python3
"""Inventory or delete legacy product-side XGC2 APT GitHub secrets.

The command is deliberately dry-run by default and has a closed deletion
allowlist.  It never reads secret values and never touches the central
lxk36/xgc2-devops production Environment.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


APT_SECRET_NAMES = (
    "APT_REPO_HOST",
    "APT_REPO_KNOWN_HOSTS",
    "APT_REPO_PORT",
    "APT_REPO_SSH_KEY",
    "APT_REPO_USER",
)
CENTRAL_REPOSITORY = "lxk36/xgc2-devops"
EXECUTE_CONFIRMATION = "DELETE_LEGACY_PRODUCT_APT_SECRETS"


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def normalize_github_repository(url: str) -> str:
    value = url.strip()
    if value.endswith(".git"):
        value = value[:-4]
    if value.startswith("git@github.com:"):
        value = value.split(":", 1)[1]
    elif "github.com/" in value:
        value = value.split("github.com/", 1)[1]
    if value.count("/") != 1:
        raise ValueError(f"cannot normalize GitHub repository: {url!r}")
    return value


def require_product_repository(repository: str) -> None:
    normalized = normalize_github_repository(repository)
    if normalized.casefold() == CENTRAL_REPOSITORY.casefold():
        raise ValueError(
            f"refusing to access central repository secrets: {CENTRAL_REPOSITORY}"
        )


def product_repositories(root: Path, catalog_path: Path) -> list[str]:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    products = catalog.get("products")
    if not isinstance(products, list):
        raise ValueError("catalog products must be an array")
    repositories: set[str] = set()
    for product in products:
        if not isinstance(product, dict):
            continue
        apt = product.get("apt")
        if not isinstance(apt, dict) or not (apt.get("install") or apt.get("packages")):
            continue
        release = product.get("release")
        configured = release.get("repository") if isinstance(release, dict) else None
        if configured:
            repository = normalize_github_repository(str(configured))
        else:
            source_value = product.get("_source")
            if not isinstance(source_value, str):
                raise ValueError(f"{product.get('id')}: catalog source is missing")
            source_dir = (root / source_value).parent.parent
            result = run(
                ["git", "-C", str(source_dir), "remote", "get-url", "origin"],
                check=False,
            )
            if result.returncode:
                raise ValueError(
                    f"{product.get('id')}: cannot resolve repository: {result.stderr.strip()}"
                )
            repository = normalize_github_repository(result.stdout)
        require_product_repository(repository)
        repositories.add(repository)
    return sorted(repositories)


def secret_names(repository: str, environment: str | None = None) -> set[str]:
    require_product_repository(repository)
    command = ["gh", "secret", "list", "--repo", repository, "--json", "name"]
    if environment:
        command.extend(["--env", environment])
    result = run(command)
    values = json.loads(result.stdout or "[]")
    return {
        str(item["name"])
        for item in values
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def environment_names(repository: str) -> list[str]:
    require_product_repository(repository)
    result = run(
        [
            "gh",
            "api",
            f"repos/{repository}/environments",
            "--paginate",
            "--slurp",
        ]
    )
    pages = json.loads(result.stdout or "[]")
    if not isinstance(pages, list):
        raise ValueError("GitHub Environment response must be an array")
    names: set[str] = set()
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("GitHub Environment page must be an object")
        environments = page.get("environments", [])
        if not isinstance(environments, list):
            raise ValueError("GitHub Environment list must be an array")
        for environment in environments:
            if not isinstance(environment, dict) or not isinstance(
                environment.get("name"), str
            ):
                raise ValueError("GitHub Environment entry is missing its name")
            names.add(environment["name"])
    return sorted(names)


def delete_secret(repository: str, name: str, environment: str | None = None) -> None:
    require_product_repository(repository)
    if name not in APT_SECRET_NAMES:
        raise ValueError(f"refusing to delete non-APT secret: {name}")
    command = ["gh", "secret", "delete", name, "--repo", repository]
    if environment:
        command.extend(["--env", environment])
    run(command)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    catalog_path = (root / args.catalog).resolve()
    if args.execute and args.confirm != EXECUTE_CONFIRMATION:
        parser.error(f"--execute requires --confirm {EXECUTE_CONFIRMATION}")

    repositories = product_repositories(root, catalog_path)
    failures: list[str] = []
    selected: list[tuple[str, str, str | None]] = []
    environments_by_repository: dict[str, list[str]] = {}
    for repository in repositories:
        try:
            existing = secret_names(repository)
        except (
            OSError,
            ValueError,
            subprocess.CalledProcessError,
            json.JSONDecodeError,
        ) as exc:
            failures.append(f"{repository}: cannot list repository secrets: {exc}")
            continue
        for name in APT_SECRET_NAMES:
            if name in existing:
                selected.append((repository, name, None))

        try:
            environments = environment_names(repository)
        except (
            OSError,
            ValueError,
            subprocess.CalledProcessError,
            json.JSONDecodeError,
        ) as exc:
            failures.append(f"{repository}: cannot list Environments: {exc}")
            continue
        environments_by_repository[repository] = environments
        for environment in environments:
            try:
                environment_existing = secret_names(repository, environment)
            except (
                OSError,
                ValueError,
                subprocess.CalledProcessError,
                json.JSONDecodeError,
            ) as exc:
                failures.append(
                    f"{repository}/environment:{environment}: "
                    f"cannot list secrets: {exc}"
                )
                continue
            for name in APT_SECRET_NAMES:
                if name in environment_existing:
                    selected.append((repository, name, environment))

    preflight_succeeded = not failures
    mode = "DELETE" if args.execute and preflight_succeeded else "DRY-RUN"
    if args.execute and not preflight_succeeded:
        mode = "ABORTED"
    for repository, name, environment in selected:
        scope = f"environment:{environment}" if environment else "repository"
        print(f"{mode}\t{repository}\t{scope}\t{name}")
        if not args.execute or not preflight_succeeded:
            continue
        try:
            delete_secret(repository, name, environment)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            failures.append(f"{repository}/{scope}/{name}: delete failed: {exc}")

    if args.execute and preflight_succeeded:
        for repository in repositories:
            readback_environments = set(environments_by_repository.get(repository, []))
            try:
                readback_environments.update(environment_names(repository))
            except (
                OSError,
                ValueError,
                subprocess.CalledProcessError,
                json.JSONDecodeError,
            ) as exc:
                failures.append(
                    f"{repository}: Environment discovery readback failed: {exc}"
                )
            readback_scopes: list[str | None] = [None, *sorted(readback_environments)]
            for environment in readback_scopes:
                scope = f"environment:{environment}" if environment else "repository"
                try:
                    remaining = secret_names(repository, environment) & set(
                        APT_SECRET_NAMES
                    )
                except (
                    OSError,
                    ValueError,
                    subprocess.CalledProcessError,
                    json.JSONDecodeError,
                ) as exc:
                    failures.append(f"{repository}/{scope}: readback failed: {exc}")
                    continue
                if remaining:
                    failures.append(
                        f"{repository}/{scope}: legacy APT secrets remain: "
                        f"{sorted(remaining)}"
                    )

    print(
        json.dumps(
            {
                "mode": mode.lower(),
                "repositories": len(repositories),
                "selected": len(selected),
                "failures": failures,
            },
            sort_keys=True,
        )
    )
    if failures:
        for failure in failures:
            print(f"error: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
