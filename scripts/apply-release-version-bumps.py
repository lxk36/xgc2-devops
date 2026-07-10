#!/usr/bin/env python3
"""Apply version bumps from an XGC2 release plan."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


RELEASE_ACTION = "release"
SCRIPT_DEPENDENCY_PATHS = (
    ".xgc2/scripts/package_debs.sh",
    ".xgc2/scripts/check_package_compliance.sh",
)


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
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


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def deterministic_commit_environment(plan_digest: str) -> dict[str, str]:
    # Bind commit timestamps to immutable plan content so an interrupted train
    # can reproduce byte-identical commits from the same pinned gitlinks.
    seconds = int(plan_digest[:8], 16) % (40 * 365 * 24 * 60 * 60)
    timestamp = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=seconds)
    value = timestamp.isoformat().replace("+00:00", "Z")
    return {**os.environ, "GIT_AUTHOR_DATE": value, "GIT_COMMITTER_DATE": value}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def transaction_preview(root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for item in plan_items(plan):
        if item.get("action") != RELEASE_ACTION:
            continue
        key = (str(item.get("repository", "")), str(item.get("ref", "")))
        entry = {
            "repository": key[0],
            "ref": key[1],
            "path": str(item.get("source", "")),
            "base": str(item.get("expected_source_sha", "")),
            "target": None,
            "tree": None,
            "status": "planned",
        }
        previous = entries.setdefault(key, entry)
        if previous["base"] != entry["base"] or previous["path"] != entry["path"]:
            raise ValueError(f"conflicting release transaction identity for {key[0]}@{key[1]}")
    return {
        "schema": "xgc2.release-push-transaction.v1",
        "initial_plan_digest": canonical_digest(plan),
        "entries": [entries[key] for key in sorted(entries)],
    }


def refresh_dependency_digests(plan: dict[str, Any]) -> None:
    by_id = {str(item["id"]): item for item in plan_items(plan)}
    for item in by_id.values():
        policies = item.get("dependency_policy")
        policies = policies if isinstance(policies, dict) else {}
        dependency_set = []
        for provider_id in sorted(map(str, item.get("dependencies", []))):
            provider = by_id[provider_id]
            dependency_set.append(
                {
                    "id": provider_id,
                    "action": provider.get("action"),
                    "source_sha": provider.get("expected_source_sha"),
                    "version": provider.get("expected_version"),
                    "policy": policies.get(provider_id, "order"),
                }
            )
        item["dependency_set_digest"] = canonical_digest(dependency_set)


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
    match = re.match(r"^(?P<package>\S+)(?:\s+\([^)]*\))?(?P<remainder>.*)$", dependency)
    if not match:
        return dependency
    return f"{package} (>= {version}){match.group('remainder')}"


def split_dependency(dependency: str) -> tuple[str, str]:
    parts = dependency.split(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], f" {parts[1]}"


def dependency_replacements(old_dependency: str, new_dependency: str) -> dict[str, str]:
    # Alternatives describe resolver choice, and products may select one member
    # explicitly per distribution (for example libgcc1 on bionic). Injecting the
    # full metadata alternative at a bare branch assignment is both invalid and
    # non-idempotent. Alternative changes need an explicit distribution-aware
    # migration, never the generic relationship rewriter.
    if "|" in old_dependency or "|" in new_dependency:
        return {}
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
    replacements: list[tuple[str, str]] = []
    for old_dependency, new_dependency in zip(old_depends, new_depends):
        replacements.extend(dependency_replacements(old_dependency, new_dependency).items())
    scripts_dir = source_dir / ".xgc2" / "scripts"
    candidate_paths = (
        sorted(path for path in scripts_dir.rglob("*.sh") if path.is_file())
        if scripts_dir.exists()
        else []
    )
    for path in candidate_paths:
        relative_path = path.relative_to(source_dir).as_posix()
        old_text = path.read_text(encoding="utf-8")
        new_text = old_text
        for old_text_dependency, new_text_dependency in replacements:
            old_package, _old_suffix = split_dependency(old_text_dependency)
            new_package, new_suffix = split_dependency(new_text_dependency)
            # Replace the complete Debian relationship atomically. Matching
            # repeated relations also repairs legacy `pkg (>= new) (>= old)`
            # corruption instead of retaining the stale suffix.
            relation = r"(?:\s+\(\s*(?:<<|<=|=|>=|>>)\s*[^)]+\))*"
            pattern = re.compile(
                rf"(?<![A-Za-z0-9+_.-]){re.escape(old_package)}{relation}"
                r"(?![A-Za-z0-9+_.-])"
            )
            assignment = re.compile(
                rf"^\s*(?P<variable>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
                rf"(?P<quote>['\"]?){re.escape(old_package)}{relation}(?P=quote)\s*"
                r"(?:#.*)?$"
            )
            variables = {
                match.group("variable"): match.group("variable").endswith("_dep")
                for line in new_text.splitlines()
                if (match := assignment.fullmatch(line)) is not None
            }
            # Compliance scripts often assert `${pkg}` relations while the
            # package-name assignment lives in package_debs.sh. Discover the
            # variable identity across the product's complete script set.
            for candidate in candidate_paths:
                candidate_text = (
                    new_text if candidate == path else candidate.read_text(encoding="utf-8")
                )
                for candidate_line in candidate_text.splitlines():
                    candidate_match = assignment.fullmatch(candidate_line)
                    if candidate_match:
                        variable = candidate_match.group("variable")
                        variables[variable] = variable.endswith("_dep")
            semantic_file = path.name in {
                "package_debs.sh", "check_package_compliance.sh"
            }
            new_version_match = re.search(
                r"\(\s*(?:<<|<=|=|>=|>>)\s*([^)\s]+)\s*\)", new_suffix
            )
            new_version = new_version_match.group(1) if new_version_match else ""
            updated_lines: list[str] = []
            in_control_heredoc = False
            control_continuation = False
            for line in new_text.splitlines(keepends=True):
                logical_line = line.rstrip("\r\n")
                if "DEBIAN/control" in logical_line and "<<" in logical_line:
                    in_control_heredoc = True
                semantic_control = (
                    in_control_heredoc
                    or control_continuation
                    or "write_control" in logical_line
                    or re.search(r"\bDepends\s*[:=]", logical_line) is not None
                )
                control_continuation = semantic_control and logical_line.rstrip().endswith("\\")
                if in_control_heredoc and logical_line.strip() in {"EOF", "CONTROL"}:
                    in_control_heredoc = False
                semantic_relation = (
                    semantic_file
                    or "grep" in logical_line
                    or semantic_control
                )

                def replace_literal(match: re.Match[str]) -> str:
                    # Variables and command arguments hold package names, not
                    # Debian control relations. This also repairs an earlier
                    # polluted `apt-get ... pkg (>= version)` rewrite.
                    direct_assignment = assignment.fullmatch(logical_line)
                    assignment_variable = (
                        direct_assignment.group("variable") if direct_assignment else None
                    )
                    if assignment_variable is None:
                        prefix = logical_line[: match.start()]
                        asserted_assignment = re.search(
                            r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"]?$", prefix
                        )
                        if asserted_assignment:
                            assignment_variable = asserted_assignment.group(1)
                    if assignment_variable is not None:
                        return (
                            f"{new_package}{new_suffix}"
                            if assignment_variable.endswith("_dep")
                            else new_package
                        )
                    if not semantic_relation:
                        return new_package
                    has_relation = re.search(
                        r"\(\s*(?:<<|<=|=|>=|>>)\s*[^)]+\)", match.group(0)
                    ) is not None
                    if (
                        not has_relation
                        and path.name != "package_debs.sh"
                        and not semantic_control
                    ):
                        return new_package
                    return f"{new_package}{new_suffix}"

                rewritten = pattern.sub(replace_literal, line)
                for variable, variable_contains_relation in sorted(variables.items()):
                    variable_relation = re.compile(
                        rf"(?P<variable>\$\{{{re.escape(variable)}\}}|\${re.escape(variable)})"
                        r"(?:\s+\(\s*(?:<<|<=|=|>=|>>)\s*[^)]+\))+"
                    )
                    rewritten = variable_relation.sub(
                        lambda match: (
                            match.group("variable")
                            if variable_contains_relation
                            else f"{match.group('variable')}{new_suffix}"
                            if semantic_relation
                            else match.group("variable")
                        ),
                        rewritten,
                    )
                if (
                    new_version
                    and "dpkg --compare-versions" in rewritten
                    and re.search(
                        rf"(?<![A-Za-z0-9+_.-]){re.escape(new_package)}"
                        r"(?![A-Za-z0-9+_.-])",
                        rewritten,
                    )
                ):
                    rewritten = re.sub(
                        r"(?P<prefix>\b(?:ge|gt|eq)\s+)(?P<quote>['\"])[^'\"]+(?P=quote)",
                        lambda match: (
                            f"{match.group('prefix')}{match.group('quote')}"
                            f"{new_version}{match.group('quote')}"
                        ),
                        rewritten,
                    )
                updated_lines.append(rewritten)
            new_text = "".join(updated_lines)
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
    update_dependencies: bool,
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
        compliance_path = source_dir / ".xgc2" / "scripts" / "check_package_compliance.sh"
        if compliance_path.exists():
            old_text = compliance_path.read_text(encoding="utf-8")
            new_text = old_text.replace(
                f"^version: {current_version}$", f"^version: {expected_version}$"
            )
            if new_text != old_text:
                changed_paths.add(".xgc2/scripts/check_package_compliance.sh")
                if apply:
                    compliance_path.write_text(new_text, encoding="utf-8")

    runtime_manifest_path = source_dir / "manifest" / "px4_runtime.yaml"
    if should_consider and expected_version and runtime_manifest_path.exists():
        runtime_manifest = load_yaml(runtime_manifest_path)
        if str(runtime_manifest.get("debian_version", "")) != expected_version:
            runtime_manifest["debian_version"] = expected_version
            changed_paths.add("manifest/px4_runtime.yaml")
            if apply:
                dump_yaml(runtime_manifest_path, runtime_manifest)

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
    if (
        should_consider
        and update_dependencies
        and isinstance(apt, dict)
        and isinstance(apt.get("depends"), list)
    ):
        old_depends = [str(dependency) for dependency in apt["depends"]]
        new_depends = [
            update_dependency_version(str(dependency), owner_versions)
            for dependency in apt["depends"]
        ]
        changed_paths.update(
            update_script_dependency_versions(
                source_dir,
                old_depends,
                new_depends,
                apply=apply,
            )
        )
        if new_depends != apt["depends"]:
            apt["depends"] = new_depends
            changed = True

    if changed:
        changed_paths.add(".xgc2/product.yml")
        if apply:
            dump_yaml(product_path, metadata)
    if changed:
        if expected_version and expected_version != current_version:
            print(f"{item['id']}: {current_version} -> {expected_version}")
        else:
            print(f"{item['id']}: dependency minimums updated")

    release_set_path = source_dir / ".xgc2" / "release-set.yml"
    if should_consider and release_set_path.exists():
        release_set = load_yaml(release_set_path)
        entries = release_set.get("packages", {})
        release_set_changed = False
        if isinstance(entries, dict):
            for entry in entries.values():
                if not isinstance(entry, dict):
                    continue
                apt_package = str(entry.get("apt", ""))
                planned = owner_versions.get(apt_package) if update_dependencies else None
                if entry.get("local") and expected_version:
                    planned = expected_version
                if planned and str(entry.get("version", "")) != planned:
                    entry["version"] = planned
                    release_set_changed = True
        if release_set_changed:
            changed_paths.add(".xgc2/release-set.yml")
            if apply:
                dump_yaml(release_set_path, release_set)
            print(f"{item['id']}: release-set versions updated")
    return changed_paths


def commit_repo(
    repo: Path,
    message: str,
    *,
    stage_paths: list[str],
    commit_env: dict[str, str] | None = None,
) -> str:
    if stage_paths:
        run(["git", "add", *stage_paths], cwd=repo)
    diff_result = run(["git", "diff", "--cached", "--quiet"], cwd=repo, check=False)
    if diff_result.returncode != 0:
        run(["git", "commit", "-m", message], cwd=repo, env=commit_env)
    sha = git(["rev-parse", "HEAD"], repo, check=True)
    return sha


def remote_ref_head(repo: Path, ref: str) -> str:
    result = run(
        ["git", "ls-remote", "--exit-code", "origin", f"refs/heads/{ref}"],
        cwd=repo,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"{repo}: cannot resolve origin branch {ref}: {result.stderr.strip()}")
    return result.stdout.split()[0]


def push_transaction(entries: list[dict[str, Any]], path: Path) -> None:
    # Validate every remote before the first write. Each actual push also uses
    # an exact force-with-lease to close the preflight/push race.
    for entry in entries:
        repo = Path(str(entry.pop("_repo_path")))
        entry["_repo_path"] = repo.as_posix()
        remote = remote_ref_head(repo, str(entry["ref"]))
        entry["remote_before"] = remote
        if remote == entry["target"]:
            entry["status"] = "already-pushed"
        elif remote == entry["base"]:
            entry["status"] = "ready"
        else:
            raise RuntimeError(
                f"{repo}: origin/{entry['ref']} is {remote}, expected base "
                f"{entry['base']} or exact target {entry['target']}"
            )
    atomic_json(path, {
        "schema": "xgc2.release-push-transaction.v1",
        "initial_plan_digest": entries[0]["initial_plan_digest"] if entries else "",
        "final_plan_digest": entries[0]["final_plan_digest"] if entries else "",
        "entries": [{key: value for key, value in entry.items() if not key.startswith("_")}
                    for entry in entries],
    })
    for entry in entries:
        if entry["status"] == "already-pushed":
            continue
        repo = Path(str(entry["_repo_path"]))
        remote = str(entry["remote_before"])
        ref = str(entry["ref"])
        result = run(
            [
                "git", "push", f"--force-with-lease=refs/heads/{ref}:{remote}",
                "origin", f"HEAD:refs/heads/{ref}",
            ],
            cwd=repo,
            check=False,
        )
        if result.returncode != 0:
            entry["status"] = "push-failed"
            entry["error"] = (result.stderr or result.stdout)[-2000:]
            atomic_json(path, {
                "schema": "xgc2.release-push-transaction.v1",
                "initial_plan_digest": entry["initial_plan_digest"],
                "final_plan_digest": entry["final_plan_digest"],
                "entries": [{key: value for key, value in item.items() if not key.startswith("_")}
                            for item in entries],
            })
            raise RuntimeError(f"{repo}: transaction push failed: {entry['error']}")
        actual = remote_ref_head(repo, ref)
        if actual != entry["target"]:
            raise RuntimeError(f"{repo}: remote did not reach transaction target {entry['target']}")
        entry["status"] = "pushed"
        entry["remote_after"] = actual
        atomic_json(path, {
            "schema": "xgc2.release-push-transaction.v1",
            "initial_plan_digest": entry["initial_plan_digest"],
            "final_plan_digest": entry["final_plan_digest"],
            "entries": [{key: value for key, value in item.items() if not key.startswith("_")}
                        for item in entries],
        })


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
                "schema": "xgc2.release-lock.v2",
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
    commit_env: dict[str, str] | None = None,
) -> str:
    top_level_paths = sorted({direct_top_level_gitlink(root, repo) for repo in touched_repos})
    add_paths = [repo.relative_to(root).as_posix() for repo in top_level_paths]
    if tracked_lock is not None:
        add_paths.append(tracked_lock.relative_to(root).as_posix())
    if not add_paths:
        return git(["rev-parse", "HEAD"], root, check=True)
    run(["git", "add", *add_paths], cwd=root)
    diff_result = run(["git", "diff", "--cached", "--quiet"], cwd=root, check=False)
    if diff_result.returncode == 0:
        return git(["rev-parse", "HEAD"], root, check=True)
    run(
        ["git", "commit", "-m", "chore: update XGC2 release lock"],
        cwd=root,
        env=commit_env,
    )
    return git(["rev-parse", "HEAD"], root, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--lock-output", default=".work/release-lock.json")
    parser.add_argument("--tracked-lock-output")
    parser.add_argument(
        "--transaction-output", default=".work/release-push-transaction.json"
    )
    parser.add_argument("--transaction-preview-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--push", action="store_true")
    parser.add_argument(
        "--skip-dependency-updates",
        action="store_true",
        help="bump local product/release-set versions without changing internal dependency minimums",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    plan_path = (root / args.plan).resolve()
    with plan_path.open("r", encoding="utf-8") as handle:
        plan = json.load(handle)
    initial_plan_digest = canonical_digest(plan)
    transaction_path = (root / args.transaction_output).resolve()
    if args.transaction_preview_only:
        atomic_json(transaction_path, transaction_preview(root, plan))
        return 0

    original_source_shas = {
        (root / str(item["source"])).resolve(): str(item.get("expected_source_sha", ""))
        for item in plan_items(plan)
    }
    original_root_sha = git(["rev-parse", "HEAD"], root, check=True)
    original_root_ref = git(["branch", "--show-current"], root)
    original_root_remote = (
        remote_ref_head(root, original_root_ref)
        if args.push and original_root_ref
        else ""
    )
    commit_env = deterministic_commit_environment(initial_plan_digest)

    touched_repos: dict[Path, list[dict[str, Any]]] = {}
    touched_paths_by_repo: dict[Path, set[str]] = {}
    plan_items_by_source: dict[Path, list[dict[str, Any]]] = {}
    owner_versions = package_owner_versions(plan)
    for item in plan_items(plan):
        plan_items_by_source.setdefault(root / str(item["source"]), []).append(item)
        changed_paths = update_product_metadata(
            root,
            item,
            owner_versions=owner_versions,
            update_dependencies=not args.skip_dependency_updates,
            apply=args.apply,
        )
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
        # Read every remote before creating commits. Validation is repeated
        # against base-or-target after all commits exist, enabling idempotent
        # recovery from a partial prior transaction.
        preflight_remote_heads: dict[Path, str] = {}
        for repo in sorted(stage_paths_by_repo, key=lambda item: len(item.parts), reverse=True):
            items = touched_repos.get(repo, plan_items_by_source.get(repo, []))
            ref = str(items[0].get("ref", "")) if items else ""
            base = original_source_shas.get(repo.resolve(), "")
            if not ref or not base:
                raise RuntimeError(f"{repo}: missing planned ref/base for release transaction")
            local = git(["rev-parse", "HEAD"], repo, check=True)
            if local != base:
                raise RuntimeError(f"{repo}: local HEAD {local} differs from planned base {base}")
            preflight_remote_heads[repo] = remote_ref_head(repo, ref) if args.push else ""
        for repo in sorted(stage_paths_by_repo, key=lambda item: len(item.parts), reverse=True):
            items = touched_repos.get(repo, plan_items_by_source.get(repo, []))
            product_ids = ", ".join(str(item["id"]) for item in items)
            if not product_ids:
                product_ids = repo.relative_to(root).as_posix()
            sha = commit_repo(
                repo,
                f"chore: bump XGC2 release version for {product_ids}",
                stage_paths=sorted(stage_paths_by_repo[repo]),
                commit_env=commit_env,
            )
            for item in plan_items_by_source.get(repo, []):
                item["source_sha"] = sha
                item["expected_source_sha"] = sha

    refresh_dependency_digests(plan)
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_lock((root / args.lock_output).resolve(), plan)
    tracked_lock = (root / args.tracked_lock_output).resolve() if args.tracked_lock_output else None
    if tracked_lock is not None:
        write_lock(tracked_lock, plan)
    if args.apply and args.commit:
        root_target = commit_top_level(
            root,
            touched_repos=top_level_touched_repos,
            tracked_lock=tracked_lock,
            commit_env=commit_env,
        )
        final_plan_digest = canonical_digest(plan)
        transaction_entries: list[dict[str, Any]] = []
        for repo in sorted(
            top_level_touched_repos, key=lambda item: len(item.parts), reverse=True
        ):
            items = touched_repos.get(repo, plan_items_by_source.get(repo, []))
            if not items:
                raise RuntimeError(f"{repo}: no product identity for transaction")
            ref = str(items[0].get("ref", ""))
            target = git(["rev-parse", "HEAD"], repo, check=True)
            transaction_entries.append(
                {
                    "repository": str(items[0].get("repository", "")),
                    "ref": ref,
                    "path": repo.relative_to(root).as_posix(),
                    "base": original_source_shas[repo.resolve()],
                    "target": target,
                    "tree": git(["rev-parse", "HEAD^{tree}"], repo, check=True),
                    "status": "committed",
                    "preflight_remote": preflight_remote_heads.get(repo, ""),
                    "initial_plan_digest": initial_plan_digest,
                    "final_plan_digest": final_plan_digest,
                    "_repo_path": repo.resolve().as_posix(),
                }
            )
        root_ref = original_root_ref
        if not root_ref:
            raise RuntimeError(f"{root}: cannot infer top-level push branch")
        transaction_entries.append(
            {
                "repository": git(["config", "--get", "remote.origin.url"], root),
                "ref": root_ref,
                "path": ".",
                "base": original_root_sha,
                "target": root_target,
                "tree": git(["rev-parse", "HEAD^{tree}"], root, check=True),
                "status": "committed",
                "preflight_remote": original_root_remote,
                "initial_plan_digest": initial_plan_digest,
                "final_plan_digest": final_plan_digest,
                "_repo_path": root.as_posix(),
            }
        )
        atomic_json(
            transaction_path,
            {
                "schema": "xgc2.release-push-transaction.v1",
                "initial_plan_digest": initial_plan_digest,
                "final_plan_digest": final_plan_digest,
                "entries": [
                    {key: value for key, value in entry.items() if not key.startswith("_")}
                    for entry in transaction_entries
                ],
            },
        )
        if args.push:
            push_transaction(transaction_entries, transaction_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
