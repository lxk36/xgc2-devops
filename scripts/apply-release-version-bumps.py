#!/usr/bin/env python3
"""Apply version bumps from an XGC2 release plan."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


RELEASE_ACTION = "release"
SCRIPT_DEPENDENCY_PATHS = (
    ".xgc2/scripts/build_debs_in_docker.sh",
    ".xgc2/scripts/package_debs.sh",
    ".xgc2/scripts/check_package_compliance.sh",
)


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def git(args: list[str], cwd: Path, *, check: bool = False) -> str:
    result = run(["git", *args], cwd=cwd, check=check)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit("PyYAML is required: python3 -m pip install PyYAML") from exc
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: product metadata must be a mapping")
    return data


def dump_yaml(path: Path, data: dict[str, Any]) -> None:
    import yaml

    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)


def plan_items(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for layer in plan.get("layers", [])
        if isinstance(layer, list)
        for item in layer
        if isinstance(item, dict)
    ]


def package_owner_versions(plan: dict[str, Any]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for item in plan_items(plan):
        apt_versions = item.get("apt_versions")
        if not isinstance(apt_versions, dict) or not apt_versions:
            version = str(item.get("expected_version", ""))
        else:
            versions = sorted(set(str(value) for value in apt_versions.values()))
            version = versions[0] if len(versions) == 1 else str(apt_versions.get("focal", ""))
        if not version:
            continue
        for package in item.get("apt_packages", []):
            owners[str(package)] = version
        for package in item.get("apt_install", []):
            owners.setdefault(str(package), version)
    return owners


def update_dependency_version(dependency: str, owner_versions: dict[str, str]) -> str:
    package = dependency.split(" ", 1)[0]
    version = owner_versions.get(package)
    if not version:
        return dependency
    if "(" in dependency and ")" in dependency:
        return f"{package} (>= {version})"
    return dependency


def split_dependency(dependency: str) -> tuple[str, str]:
    parts = dependency.split(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], f" {parts[1]}"


def dependency_replacements(old_dependency: str, new_dependency: str) -> dict[str, str]:
    old_package, old_suffix = split_dependency(old_dependency)
    new_package, new_suffix = split_dependency(new_dependency)
    replacements = {old_dependency: new_dependency}
    old_ros_prefix = "ros-noetic-"
    new_ros_prefix = "ros-noetic-"
    if old_package.startswith(old_ros_prefix) and new_package.startswith(new_ros_prefix):
        old_name = old_package[len(old_ros_prefix) :]
        new_name = new_package[len(new_ros_prefix) :]
        for prefix in ("ros-${ROS_DISTRO}-", "ros-\\${ROS_DISTRO}-"):
            replacements[f"{prefix}{old_name}{old_suffix}"] = f"{prefix}{new_name}{new_suffix}"
    return replacements


def update_script_dependency_versions(
    source_dir: Path,
    old_depends: list[str],
    new_depends: list[str],
    *,
    apply: bool,
) -> set[str]:
    changed_paths: set[str] = set()
    if old_depends == new_depends:
        return changed_paths
    replacements: dict[str, str] = {}
    for old_dependency, new_dependency in zip(old_depends, new_depends):
        replacements.update(dependency_replacements(old_dependency, new_dependency))
    for relative_path in SCRIPT_DEPENDENCY_PATHS:
        path = source_dir / relative_path
        if not path.exists():
            continue
        old_text = path.read_text(encoding="utf-8")
        new_text = old_text
        for old_text_dependency, new_text_dependency in replacements.items():
            new_text = new_text.replace(old_text_dependency, new_text_dependency)
        if new_text != old_text:
            changed_paths.add(relative_path)
            if apply:
                path.write_text(new_text, encoding="utf-8")
    return changed_paths


def update_product_metadata(
    root: Path,
    item: dict[str, Any],
    *,
    owner_versions: dict[str, str],
    apply: bool,
) -> set[str]:
    if item.get("action") != RELEASE_ACTION:
        should_consider = False
    else:
        should_consider = True
    expected_version = str(item.get("expected_version", ""))
    current_version = str(item.get("version", ""))
    source_dir = root / str(item["source"])
    product_path = source_dir / ".xgc2" / "product.yml"
    metadata = load_yaml(product_path)
    changed = False
    changed_paths: set[str] = set()

    if should_consider and expected_version and expected_version != current_version:
        metadata["version"] = expected_version
        changed = True

    apt_versions = item.get("apt_versions")
    if should_consider and isinstance(apt_versions, dict) and len(set(map(str, apt_versions.values()))) > 1:
        release = metadata.setdefault("release", {})
        if not isinstance(release, dict):
            raise ValueError(f"{product_path}: release must be a mapping")
        new_versions = {str(key): str(value) for key, value in apt_versions.items()}
        if release.get("apt_versions") != new_versions:
            release["apt_versions"] = new_versions
            changed = True

    apt = metadata.get("apt")
    if should_consider and isinstance(apt, dict) and isinstance(apt.get("depends"), list):
        old_depends = [str(dependency) for dependency in apt["depends"]]
        new_depends = [
            update_dependency_version(str(dependency), owner_versions)
            for dependency in apt["depends"]
        ]
        if new_depends != apt["depends"]:
            apt["depends"] = new_depends
            changed = True
            changed_paths.update(
                update_script_dependency_versions(
                    source_dir,
                    old_depends,
                    new_depends,
                    apply=apply,
                )
            )

    if changed:
        changed_paths.add(".xgc2/product.yml")
        if apply:
            dump_yaml(product_path, metadata)
    if changed:
        if expected_version and expected_version != current_version:
            print(f"{item['id']}: {current_version} -> {expected_version}")
        else:
            print(f"{item['id']}: dependency minimums updated")
    return changed_paths


def commit_repo(
    repo: Path,
    message: str,
    *,
    stage_paths: list[str],
    push_ref: str | None,
    push: bool,
) -> str:
    if stage_paths:
        run(["git", "add", *stage_paths], cwd=repo)
    diff_result = run(["git", "diff", "--cached", "--quiet"], cwd=repo, check=False)
    if diff_result.returncode != 0:
        run(["git", "commit", "-m", message], cwd=repo)
    sha = git(["rev-parse", "HEAD"], repo, check=True)
    if push:
        ref = push_ref or git(["branch", "--show-current"], repo)
        if not ref:
            raise RuntimeError(f"{repo}: cannot infer push ref")
        run(["git", "push", "origin", f"HEAD:{ref}"], cwd=repo)
    return sha


def nearest_parent_git_repo(root: Path, repo: Path) -> Path | None:
    current = repo.parent
    while current != root and root in current.parents:
        if git(["rev-parse", "--show-toplevel"], current) == current.as_posix():
            return current
        current = current.parent
    return root if repo != root else None


def direct_top_level_gitlink(root: Path, repo: Path) -> Path:
    relative = repo.relative_to(root)
    parts = relative.parts
    for index in range(1, len(parts) + 1):
        candidate = Path(*parts[:index])
        candidate_text = candidate.as_posix()
        lines = git(["ls-files", "-s", "--", candidate_text], root).splitlines()
        if any(
            line.startswith("160000 ") and line.rsplit("\t", 1)[-1] == candidate_text
            for line in lines
        ):
            return root / candidate
    return repo


def write_lock(path: Path, plan: dict[str, Any]) -> None:
    products = plan_items(plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "xgc2.release-lock.v1",
                "products": products,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def commit_top_level(
    root: Path,
    *,
    touched_repos: list[Path],
    tracked_lock: Path | None,
    push: bool,
) -> None:
    top_level_paths = sorted({direct_top_level_gitlink(root, repo) for repo in touched_repos})
    add_paths = [repo.relative_to(root).as_posix() for repo in top_level_paths]
    if tracked_lock is not None:
        add_paths.append(tracked_lock.relative_to(root).as_posix())
    if not add_paths:
        return
    run(["git", "add", *add_paths], cwd=root)
    diff_result = run(["git", "diff", "--cached", "--quiet"], cwd=root, check=False)
    if diff_result.returncode == 0:
        return
    run(["git", "commit", "-m", "chore: update XGC2 release lock"], cwd=root)
    if push:
        branch = git(["branch", "--show-current"], root)
        if not branch:
            raise RuntimeError(f"{root}: cannot infer top-level push branch")
        run(["git", "push", "origin", f"HEAD:{branch}"], cwd=root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--lock-output", default=".work/release-lock.json")
    parser.add_argument("--tracked-lock-output")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    plan_path = (root / args.plan).resolve()
    with plan_path.open("r", encoding="utf-8") as handle:
        plan = json.load(handle)

    touched_repos: dict[Path, list[dict[str, Any]]] = {}
    touched_paths_by_repo: dict[Path, set[str]] = {}
    plan_items_by_source: dict[Path, list[dict[str, Any]]] = {}
    owner_versions = package_owner_versions(plan)
    for item in plan_items(plan):
        plan_items_by_source.setdefault(root / str(item["source"]), []).append(item)
        changed_paths = update_product_metadata(root, item, owner_versions=owner_versions, apply=args.apply)
        if changed_paths:
            source_dir = root / str(item["source"])
            touched_repos.setdefault(source_dir, []).append(item)
            touched_paths_by_repo.setdefault(source_dir, set()).update(changed_paths)

    top_level_touched_repos = sorted(touched_repos)
    if args.apply and args.commit:
        stage_paths_by_repo: dict[Path, set[str]] = {
            repo: set(paths)
            for repo, paths in touched_paths_by_repo.items()
        }
        changed_repos = set(touched_repos)
        for repo in sorted(changed_repos, key=lambda item: len(item.parts), reverse=True):
            parent = nearest_parent_git_repo(root, repo)
            if parent is None or parent == root:
                continue
            stage_paths_by_repo.setdefault(parent, set()).add(
                repo.relative_to(parent).as_posix()
            )

        top_level_touched_repos = sorted(stage_paths_by_repo)
        for repo in sorted(stage_paths_by_repo, key=lambda item: len(item.parts), reverse=True):
            items = touched_repos.get(repo, plan_items_by_source.get(repo, []))
            product_ids = ", ".join(str(item["id"]) for item in items)
            if not product_ids:
                product_ids = repo.relative_to(root).as_posix()
            sha = commit_repo(
                repo,
                f"chore: bump XGC2 release version for {product_ids}",
                stage_paths=sorted(stage_paths_by_repo[repo]),
                push_ref=str(items[0].get("ref", "")) or None,
                push=args.push,
            )
            for item in plan_items_by_source.get(repo, []):
                item["source_sha"] = sha
                item["expected_source_sha"] = sha

    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_lock((root / args.lock_output).resolve(), plan)
    tracked_lock = (root / args.tracked_lock_output).resolve() if args.tracked_lock_output else None
    if tracked_lock is not None:
        write_lock(tracked_lock, plan)
    if args.apply and args.commit:
        commit_top_level(
            root,
            touched_repos=top_level_touched_repos,
            tracked_lock=tracked_lock,
            push=args.push,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
