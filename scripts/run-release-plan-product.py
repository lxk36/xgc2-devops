#!/usr/bin/env python3
"""Trigger and verify one product workflow from an immutable release plan."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


DEFAULT_APT_BASE_URL = "https://xgc2.apt.xiaokang.ink"
DEFAULT_ARCHES = ("amd64", "arm64")
RELEASE_ACTION = "release"
VERIFY_ACTION = "verify"
COMPATIBILITY_VERIFY_ACTION = "compatibility-verify"
TRANSIENT_EXIT_CODE = 75
RESULT_MARKER = "XGC2_RESULT="
STANDARD_WORKFLOW_INPUTS = {
    "expected_version",
    "expected_source_sha",
    "publish_apt",
    "run_cpp_quality",
    "run_source_tests",
    "release_id",
    "release_lock_digest",
    "trusted_ci_run_id",
    "ci_run_id",
    "prepare_action",
    "apt_overlay_url",
    "dependency_set_digest",
}


class ReleaseError(RuntimeError):
    """A deterministic release error that must not be retried automatically."""


class TransientReleaseError(ReleaseError):
    """A network, APT propagation, or publish-lock error safe to retry."""


class CompletedTransientReleaseError(TransientReleaseError):
    """A completed workflow failed transiently and may be dispatched again."""


CENTRAL_CHECKPOINT_PHASES = {
    "workflow_dispatched",
    "artifact_ready",
    "staged",
    "compatibility_verified",
    "production_verified",
}


def load_node_checkpoint(plan_path: Path, product: dict[str, Any]) -> dict[str, Any] | None:
    path = node_checkpoint_path(plan_path, str(product["id"]))
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"{product['id']}: corrupt release checkpoint: {exc}") from exc
    expected = {
        "schema": "xgc2.release-node-checkpoint.v2",
        "product": str(product["id"]),
        "action": str(product.get("action", "")),
        "source_sha": str(product.get("expected_source_sha", "")),
        "release_id": os.environ.get("XGC2_RELEASE_ID", ""),
        "release_lock_digest": os.environ.get("XGC2_RELEASE_LOCK_DIGEST", ""),
        "dependency_set_digest": str(product.get("dependency_set_digest", "")),
        "execution_policy_digest": os.environ.get("XGC2_EXECUTION_POLICY_DIGEST", ""),
    }
    if not isinstance(value, dict) or any(value.get(key) != item for key, item in expected.items()):
        raise ReleaseError(f"{product['id']}: release checkpoint does not match this train")
    if value.get("phase") not in CENTRAL_CHECKPOINT_PHASES:
        raise ReleaseError(f"{product['id']}: invalid release checkpoint phase")
    return value


def write_node_checkpoint(
    plan_path: Path,
    product: dict[str, Any],
    *,
    phase: str,
    run_id: int | None = None,
    receipt: str | None = None,
    artifact_source: str | None = None,
) -> None:
    if phase not in CENTRAL_CHECKPOINT_PHASES:
        raise ValueError(f"invalid release checkpoint phase: {phase}")
    path = node_checkpoint_path(plan_path, str(product["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    value: dict[str, Any] = {
        "schema": "xgc2.release-node-checkpoint.v2",
        "product": str(product["id"]),
        "action": str(product.get("action", "")),
        "source_sha": str(product.get("expected_source_sha", "")),
        "release_id": os.environ.get("XGC2_RELEASE_ID", ""),
        "release_lock_digest": os.environ.get("XGC2_RELEASE_LOCK_DIGEST", ""),
        "dependency_set_digest": str(product.get("dependency_set_digest", "")),
        "execution_policy_digest": os.environ.get("XGC2_EXECUTION_POLICY_DIGEST", ""),
        "phase": phase,
        "updated_at": int(time.time()),
    }
    if run_id is not None:
        value["run_id"] = run_id
    if receipt:
        value["receipt"] = receipt
    if artifact_source:
        value["artifact_source"] = artifact_source
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def node_checkpoint_path(plan_path: Path, product_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", product_id)
    return plan_path.resolve().parent / "release-node-checkpoints" / f"{safe_id}.json"


def ci_reconciliation_checkpoint_path(plan_path: Path, product_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", product_id)
    return plan_path.resolve().parent / "release-ci-reconciliation" / f"{safe_id}.json"


def write_ci_reconciliation_checkpoint(
    plan_path: Path,
    product: dict[str, Any],
    *,
    run_id: int,
    phase: str,
    prior_attempt: int | None = None,
) -> None:
    path = ci_reconciliation_checkpoint_path(plan_path, str(product["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    value: dict[str, Any] = {
        "schema": "xgc2.ci-reconciliation.v1",
        "product": str(product["id"]),
        "source_sha": str(product.get("expected_source_sha", "")),
        "workflow": str(product.get("ci_workflow") or "ci.yml"),
        "run_id": run_id,
        "phase": phase,
        "updated_at": int(time.time()),
    }
    if prior_attempt is not None:
        value["prior_attempt"] = prior_attempt
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_ci_reconciliation_checkpoint(
    plan_path: Path, product: dict[str, Any]
) -> dict[str, Any] | None:
    path = ci_reconciliation_checkpoint_path(plan_path, str(product["id"]))
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "schema": "xgc2.ci-reconciliation.v1",
        "product": str(product["id"]),
        "source_sha": str(product.get("expected_source_sha", "")),
        "workflow": str(product.get("ci_workflow") or "ci.yml"),
    }
    if not isinstance(value, dict) or any(value.get(key) != item for key, item in expected.items()):
        raise ReleaseError(f"{product['id']}: CI reconciliation checkpoint mismatch")
    return value


def load_publish_checkpoint(plan_path: Path, product: dict[str, Any]) -> dict[str, Any] | None:
    path = node_checkpoint_path(plan_path, str(product["id"]))
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"{product['id']}: corrupt publish checkpoint: {exc}") from exc
    expected = {
        "schema": "xgc2.release-node-checkpoint.v1",
        "product": str(product["id"]),
        "source_sha": str(product.get("expected_source_sha", "")),
        "release_lock_digest": os.environ.get("XGC2_RELEASE_LOCK_DIGEST", ""),
    }
    if not isinstance(value, dict) or any(value.get(key) != item for key, item in expected.items()):
        raise ReleaseError(f"{product['id']}: publish checkpoint does not match this release lock")
    if not isinstance(value.get("release_run_id"), int):
        raise ReleaseError(f"{product['id']}: publish checkpoint lacks release_run_id")
    phase = value.get("phase", "workflow_succeeded")
    if phase not in {"dispatched", "workflow_succeeded"}:
        raise ReleaseError(f"{product['id']}: publish checkpoint has invalid phase {phase!r}")
    value["phase"] = phase
    return value


def write_publish_checkpoint(
    plan_path: Path,
    product: dict[str, Any],
    *,
    release_run_id: int,
    release_run_number: int | None,
    phase: str = "workflow_succeeded",
    trusted_ci_run_id: int | None = None,
    dispatched_at: float | None = None,
    release_workflow_seconds: float | None = None,
    publish_seconds: float | None = None,
    ci_artifact_wait_seconds: float | None = None,
) -> None:
    if phase not in {"dispatched", "workflow_succeeded"}:
        raise ValueError(f"invalid release checkpoint phase: {phase}")
    path = node_checkpoint_path(plan_path, str(product["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    value = {
        "schema": "xgc2.release-node-checkpoint.v1",
        "product": str(product["id"]),
        "source_sha": str(product.get("expected_source_sha", "")),
        "release_lock_digest": os.environ.get("XGC2_RELEASE_LOCK_DIGEST", ""),
        "release_run_id": release_run_id,
        "release_run_number": release_run_number,
        "phase": phase,
        "trusted_ci_run_id": trusted_ci_run_id,
        "dispatched_at": dispatched_at if dispatched_at is not None else now,
    }
    if phase == "workflow_succeeded":
        value["completed_at"] = int(now)
    if release_workflow_seconds is not None:
        value["release_workflow_seconds"] = release_workflow_seconds
    if publish_seconds is not None:
        value["publish_seconds"] = publish_seconds
    if ci_artifact_wait_seconds is not None:
        value["ci_artifact_wait_seconds"] = ci_artifact_wait_seconds
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def clear_publish_checkpoint(plan_path: Path, product: dict[str, Any]) -> None:
    """Forget only a confirmed-complete transient run before a safe redispatch."""

    node_checkpoint_path(plan_path, str(product["id"])).unlink(missing_ok=True)


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def command_details(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(
        part for part in (result.stdout.strip(), result.stderr.strip()) if part
    )


def is_transient_message(message: str) -> bool:
    normalized = message.lower()
    deterministic_patterns = (
        "version mismatch",
        "source sha mismatch",
        "sha256 mismatch",
        "manifest identity/hash mismatch",
        "assertion failed",
        "tests failed",
        "test failure",
        "test timed out",
        "compilation terminated",
        "undefined reference",
        "no matching function",
    )
    expected_version_mismatch = re.search(
        r"expected version[^\n]*(?:got|found|actual)", normalized
    )
    if expected_version_mismatch or any(
        pattern in normalized for pattern in deterministic_patterns
    ):
        return False
    patterns = (
        "connection reset",
        "connection was reset",
        "connection refused",
        "connection timed out",
        "network is unreachable",
        "temporary failure",
        "temporarily unavailable",
        "tls handshake timeout",
        "tls eof",
        "ssl_error_syscall",
        "ssl error syscall",
        "unexpected eof while reading",
        "eof occurred in violation of protocol",
        "operation timed out",
        "i/o timeout",
        "context deadline exceeded",
        "could not resolve host",
        "rate limit",
        "http 429",
        "http 502",
        "http 503",
        "http 504",
        "service unavailable",
        "xgc2_transient",
        "publish lock",
        "lock conflict",
        "resource busy",
    )
    return any(pattern in normalized for pattern in patterns)


def find_product(plan: dict[str, Any], product_id: str) -> dict[str, Any]:
    for layer in plan.get("layers", []):
        for item in layer:
            if item.get("id") == product_id:
                return item
    raise ReleaseError(f"{product_id} is not in release plan")


def current_ref_sha(product: dict[str, Any]) -> str:
    repo = str(product["repository"])
    ref = urllib.parse.quote(str(product["ref"]), safe="")
    result = run(
        ["gh", "api", f"repos/{repo}/commits/{ref}", "--jq", ".sha"],
        check=False,
    )
    if result.returncode != 0:
        details = command_details(result)
        error = TransientReleaseError if is_transient_message(details) else ReleaseError
        raise error(f"{product['id']}: cannot resolve {repo}@{product['ref']}: {details}")
    return result.stdout.strip()


def verify_release_lock_is_current(product: dict[str, Any]) -> None:
    expected_source_sha = str(product.get("expected_source_sha", "")).strip()
    if not expected_source_sha:
        return
    actual_source_sha = current_ref_sha(product)
    if actual_source_sha != expected_source_sha:
        raise ReleaseError(
            f"{product['id']}: stale release lock for "
            f"{product['repository']}@{product['ref']}; "
            f"expected {expected_source_sha}, current head is {actual_source_sha}. "
            "Re-run release-orchestrator from the latest xgc2-devops commit."
        )


def active_run_artifact_count(repository: str, run_id: int) -> int:
    result = run(
        [
            "gh",
            "api",
            f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100",
            "--jq",
            "[.artifacts[] | select(.expired == false)] | length",
        ],
        check=False,
    )
    if result.returncode != 0:
        details = command_details(result)
        error = TransientReleaseError if is_transient_message(details) else ReleaseError
        raise error(f"cannot inspect artifacts for {repository} run {run_id}: {details}")
    try:
        return int(result.stdout.strip() or "0")
    except ValueError as exc:
        raise ReleaseError(
            f"invalid artifact count for {repository} run {run_id}: {result.stdout!r}"
        ) from exc


def trusted_ci_artifacts_match(
    product: dict[str, Any], run_id: int, *, artifact_dir: Path | None = None
) -> bool:
    """Download a candidate once and require complete build-manifest coverage.

    When ``artifact_dir`` is supplied, a successfully validated download is
    retained for the staging step. Invalid or partial downloads are removed.
    """

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if artifact_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="xgc2-ci-artifacts-")
        root = Path(temporary.name)
    else:
        root = artifact_dir
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
    try:
        result = run(
            [
                "gh",
                "run",
                "download",
                str(run_id),
                "--repo",
                str(product["repository"]),
                "--dir",
                os.fspath(root),
            ],
            check=False,
        )
        if result.returncode != 0:
            details = command_details(result)
            if "no valid artifacts" in details.lower() or "not found" in details.lower():
                if artifact_dir is not None:
                    shutil.rmtree(root, ignore_errors=True)
                return False
            error = TransientReleaseError if is_transient_message(details) else ReleaseError
            raise error(f"{product['id']}: cannot download CI run {run_id} artifacts: {details}")

        expected_product = str(product["id"])
        expected_source = str(product.get("expected_source_sha", ""))
        expected_version_value = str(product.get("expected_version") or product.get("version", ""))
        distributions = {str(item) for item in product.get("apt_distributions", [])}
        arches = set(DEFAULT_ARCHES)
        coverage: set[tuple[str, str]] = set()
        for path in root.rglob("*.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict) or manifest.get("schema") != "xgc2.build-artifact.v1":
                continue
            ci = manifest.get("ci")
            if not isinstance(ci, dict) or str(ci.get("run_id", "")) != str(run_id):
                continue
            identity = (
                str(manifest.get("product", "")),
                str(manifest.get("source_sha", "")),
                str(manifest.get("version", "")),
            )
            if identity != (expected_product, expected_source, expected_version_value):
                continue
            distribution = str(manifest.get("distribution", ""))
            architecture = str(manifest.get("architecture", ""))
            if distribution not in distributions or architecture not in arches:
                continue
            debs = manifest.get("debs")
            if not isinstance(debs, list) or not debs:
                continue
            if any(
                not isinstance(deb, dict)
                or not all(deb.get(key) not in (None, "") for key in (
                    "file", "package", "version", "architecture", "sha256", "size"
                ))
                for deb in debs
            ):
                continue
            coverage.add((distribution, architecture))
        required = {(distribution, arch) for distribution in distributions for arch in arches}
        if not required.issubset(coverage):
            missing = ", ".join(f"{dist}/{arch}" for dist, arch in sorted(required - coverage))
            print(f"{product['id']}: CI run {run_id} lacks trusted manifest coverage: {missing}")
            if artifact_dir is not None:
                shutil.rmtree(root, ignore_errors=True)
            return False
        return True
    finally:
        if temporary is not None:
            temporary.cleanup()


def find_trusted_ci_run(
    product: dict[str, Any],
    *,
    wait_seconds: int,
    poll_seconds: int,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
    artifact_cache_root: Path | None = None,
) -> int | None:
    """Find an exact successful push CI run for the locked source SHA."""

    repository = str(product["repository"])
    workflow = str(product.get("ci_workflow") or "ci.yml")
    expected_sha = str(product.get("expected_source_sha") or "")
    if not expected_sha:
        raise ReleaseError(f"{product['id']}: CI reuse requires expected_source_sha")
    deadline = now_fn() + wait_seconds
    saw_matching_run = False
    saw_completed_without_artifacts = False
    rejected_run_ids: set[int] = set()
    while True:
        result = run(
            [
                "gh",
                "run",
                "list",
                "--repo",
                repository,
                "--workflow",
                workflow,
                "--event",
                "push",
                "--commit",
                expected_sha,
                "--limit",
                "20",
                "--json",
                "databaseId,status,conclusion,headSha,url,createdAt,event",
            ],
            check=False,
        )
        if result.returncode != 0:
            details = command_details(result)
            if "could not find any workflows" in details.lower() or "http 404" in details.lower():
                print(
                    f"{product['id']}: CI workflow {workflow} is unavailable; "
                    "release will build locally"
                )
                return None
            error = TransientReleaseError if is_transient_message(details) else ReleaseError
            raise error(f"{product['id']}: cannot list target CI runs: {details}")
        try:
            runs = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"{product['id']}: gh returned invalid CI run JSON") from exc
        matching = [
            item
            for item in runs
            if item.get("event") in (None, "", "push")
            and str(item.get("headSha", "")) == expected_sha
            and isinstance(item.get("databaseId"), int)
        ]
        matching.sort(key=lambda item: str(item.get("createdAt", "")), reverse=True)
        if matching:
            saw_matching_run = True
            candidate = next(
                (item for item in matching if int(item["databaseId"]) not in rejected_run_ids),
                None,
            )
            if candidate is None:
                if now_fn() >= deadline:
                    print(
                        f"{product['id']}: matching CI artifacts were unavailable within "
                        f"{wait_seconds}s; release will build locally"
                    )
                    return None
                sleep_fn(min(float(poll_seconds), max(0.0, deadline - now_fn())))
                continue
            run_id = int(candidate["databaseId"])
            status = candidate.get("status")
            conclusion = candidate.get("conclusion")
            if status == "completed":
                if conclusion != "success":
                    raise ReleaseError(
                        f"{product['id']}: matching push CI run {run_id} concluded "
                        f"{conclusion}; refusing to bypass failed CI ({candidate.get('url')})"
                    )
                if (
                    active_run_artifact_count(repository, run_id) > 0
                    and trusted_ci_artifacts_match(
                        product,
                        run_id,
                        **(
                            {"artifact_dir": artifact_cache_root / str(run_id)}
                            if artifact_cache_root is not None
                            else {}
                        ),
                    )
                ):
                    print(f"{product['id']}: reusing trusted push CI run {run_id}")
                    return run_id
                print(
                    f"{product['id']}: successful push CI run {run_id} has no live artifacts; "
                    "waiting before fallback"
                )
                saw_completed_without_artifacts = True
                rejected_run_ids.add(run_id)
        if now_fn() >= deadline:
            reason = (
                "matching CI artifacts were unavailable"
                if saw_completed_without_artifacts
                else "matching CI did not finish"
                if saw_matching_run
                else "no matching push CI appeared"
            )
            print(f"{product['id']}: {reason} within {wait_seconds}s; release will build locally")
            return None
        sleep_fn(min(float(poll_seconds), max(0.0, deadline - now_fn())))


def trigger(
    product: dict[str, Any],
    *,
    quality_required: bool,
    source_tests: bool,
    trusted_ci_run_id: int | None,
) -> int:
    command = [
        "gh",
        "workflow",
        "run",
        str(product["workflow"]),
        "--repo",
        str(product["repository"]),
        "--ref",
        str(product["ref"]),
    ]
    workflow_inputs = set(product.get("workflow_inputs", []))
    if "publish_apt" in workflow_inputs:
        command.extend(["-f", f"publish_apt={str(product.get('action') == RELEASE_ACTION).lower()}"])
    if "expected_version" in workflow_inputs and product.get("expected_version"):
        command.extend(["-f", f"expected_version={product['expected_version']}"])
    if "expected_source_sha" in workflow_inputs and product.get("expected_source_sha"):
        command.extend(["-f", f"expected_source_sha={product['expected_source_sha']}"])
    if "run_cpp_quality" in workflow_inputs:
        command.extend(["-f", f"run_cpp_quality={str(quality_required).lower()}"])
    if "run_source_tests" in workflow_inputs:
        command.extend(["-f", f"run_source_tests={str(source_tests).lower()}"])
    release_id = os.environ.get("XGC2_RELEASE_ID") or os.environ.get("GITHUB_RUN_ID", "")
    lock_digest = os.environ.get("XGC2_RELEASE_LOCK_DIGEST", "")
    if "release_id" in workflow_inputs and release_id:
        command.extend(["-f", f"release_id={release_id}"])
    if "release_lock_digest" in workflow_inputs and lock_digest:
        command.extend(["-f", f"release_lock_digest={lock_digest}"])
    if trusted_ci_run_id is not None:
        ci_input = (
            "trusted_ci_run_id"
            if "trusted_ci_run_id" in workflow_inputs
            else "ci_run_id"
            if "ci_run_id" in workflow_inputs
            else ""
        )
        if ci_input:
            command.extend(["-f", f"{ci_input}={trusted_ci_run_id}"])
    for name, value in sorted(product.get("inputs", {}).items()):
        if name in STANDARD_WORKFLOW_INPUTS:
            continue
        command.extend(["-f", f"{name}={value}"])

    result = run(command, check=False)
    if result.returncode != 0:
        details = command_details(result)
        error = TransientReleaseError if is_transient_message(details) else ReleaseError
        raise error(
            f"{product['id']}: failed to dispatch {product['repository']} "
            f"{product['workflow']} at {product['ref']}: {details}"
        )
    direct_match = re.search(r"/actions/runs/(\d+)", command_details(result))
    if not direct_match:
        raise ReleaseError(
            f"{product['id']}: workflow dispatch succeeded but gh did not return a run URL; "
            "refusing to guess the run by time or branch"
        )
    return int(direct_match.group(1))


def trigger_prepare(
    product: dict[str, Any],
    *,
    quality_required: bool,
    source_tests: bool,
    apt_overlay_url: str,
) -> int:
    """Dispatch a product builder with production publishing forced off."""

    command = [
        "gh",
        "workflow",
        "run",
        str(product["workflow"]),
        "--repo",
        str(product["repository"]),
        "--ref",
        str(product["ref"]),
    ]
    workflow_inputs = set(product.get("workflow_inputs", []))
    needs_overlay = bool(product.get("release_scoped_build")) or product.get("action") == COMPATIBILITY_VERIFY_ACTION
    required = {"prepare_action", "apt_overlay_url", "dependency_set_digest"} if needs_overlay else set()
    missing = sorted(required - workflow_inputs)
    if missing:
        raise ReleaseError(
            f"{product['id']}: release-scoped build workflow lacks standard input(s): "
            + ", ".join(missing)
        )
    standard_values = {
        "expected_version": product.get("expected_version"),
        "expected_source_sha": product.get("expected_source_sha"),
        "prepare_action": product.get("action"),
        "apt_overlay_url": apt_overlay_url,
        "dependency_set_digest": product.get("dependency_set_digest"),
        "run_cpp_quality": str(quality_required).lower(),
        "run_source_tests": str(source_tests).lower(),
    }
    for name, value in standard_values.items():
        if name in workflow_inputs and value not in (None, ""):
            command.extend(["-f", f"{name}={value}"])
    # Legacy workflows remain safe during migration: if they still expose the
    # old switch it is explicitly false.  Central control never sends release
    # credentials or asks a product repository to mutate APT.
    if "publish_apt" in workflow_inputs:
        command.extend(["-f", "publish_apt=false"])
    for name, value in sorted(product.get("inputs", {}).items()):
        if name not in STANDARD_WORKFLOW_INPUTS:
            command.extend(["-f", f"{name}={value}"])
    result = run(command, check=False)
    if result.returncode != 0:
        details = command_details(result)
        error = TransientReleaseError if is_transient_message(details) else ReleaseError
        raise error(f"{product['id']}: failed to dispatch prepare workflow: {details}")
    direct_match = re.search(r"/actions/runs/(\d+)", command_details(result))
    if not direct_match:
        raise ReleaseError(
            f"{product['id']}: prepare dispatch returned no exact run URL/ID; refusing to guess"
        )
    return int(direct_match.group(1))


def download_run_artifacts(product: dict[str, Any], run_id: int, output: Path) -> None:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    result = run(
        [
            "gh",
            "run",
            "download",
            str(run_id),
            "--repo",
            str(product["repository"]),
            "--dir",
            os.fspath(output),
        ],
        check=False,
    )
    if result.returncode != 0:
        details = command_details(result)
        error = TransientReleaseError if is_transient_message(details) else ReleaseError
        raise error(f"{product['id']}: cannot download run {run_id} artifacts: {details}")


def subprocess_checked(command: list[str], *, product_id: str) -> subprocess.CompletedProcess[str]:
    result = run(command, check=False)
    if result.returncode != 0:
        details = command_details(result)
        error = (
            TransientReleaseError
            if result.returncode == TRANSIENT_EXIT_CODE or is_transient_message(details)
            else ReleaseError
        )
        raise error(f"{product_id}: central release command failed: {details}")
    return result


def recover_server_staged_receipt(
    plan_path: Path,
    product: dict[str, Any],
    *,
    release_id: str,
    lock_digest: str,
    overlay_url: str,
    arches: tuple[str, ...],
    apt_timeout_seconds: int,
    poll_seconds: int,
) -> bool:
    """Recover durable server staging after runner loss without rebuilding."""

    publisher = Path(__file__).with_name("release-train-publisher.py")
    result = run(
        [
            sys.executable, os.fspath(publisher), "status", "--release-id", release_id,
            "--release-lock-digest", lock_digest, "--validate-json",
        ],
        check=False,
    )
    details = command_details(result)
    if result.returncode != 0:
        if "unknown release" in details.lower():
            return False
        error = (
            TransientReleaseError
            if result.returncode == TRANSIENT_EXIT_CODE or is_transient_message(details)
            else ReleaseError
        )
        raise error(f"{product['id']}: cannot inspect durable stage: {details}")
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"{product['id']}: durable stage status is invalid JSON") from exc
    if not isinstance(status, dict) or status.get("status") not in {"prepared", "promoted"}:
        return False
    bundles = status.get("bundles")
    distributions = status.get("distributions")
    if not isinstance(bundles, dict) or not isinstance(distributions, dict):
        return False
    expected_identity = {
        "product": str(product["id"]),
        "version": str(product.get("expected_version") or product.get("version", "")),
        "source_sha": str(product.get("expected_source_sha", "")),
    }
    recovered: list[dict[str, Any]] = []
    for bundle_digest, state in bundles.items():
        if not isinstance(state, dict) or not state.get("manifests"):
            continue
        value = state.get("product")
        if not isinstance(value, dict):
            continue
        if any(value.get(key) != expected for key, expected in expected_identity.items()):
            continue
        distribution = str(value.get("distribution", ""))
        distribution_state = distributions.get(distribution)
        if not isinstance(distribution_state, dict) or distribution_state.get("published") is not True:
            continue
        if str(value.get("bundle_digest", "")) != str(bundle_digest):
            raise ReleaseError(f"{product['id']}: durable stage bundle digest mismatch")
        recovered.append(dict(value))
    expected_distributions = set(map(str, product.get("apt_distributions", [])))
    if {str(value.get("distribution", "")) for value in recovered} != expected_distributions:
        return False
    receipt_dir = plan_path.parent / "release-stage-receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt = receipt_dir / f"{product['id']}.json"
    temporary = receipt.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({
        "schema": "xgc2.stage-receipt.v1", "release_id": release_id,
        "release_lock_digest": lock_digest, "product": str(product["id"]),
        "run_id": 0, "products": sorted(recovered, key=lambda value: value["distribution"]),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(receipt)
    verify_apt(
        product,
        apt_base_url=overlay_url,
        manifest_base_url=f"{overlay_url.rstrip('/')}/manifests",
        arches=arches,
        timeout_seconds=apt_timeout_seconds,
        poll_seconds=poll_seconds,
        run_number=None,
        require_current_lock=True,
    )
    write_node_checkpoint(
        plan_path, product, phase="staged", receipt=receipt.name,
        artifact_source="server-recovered",
    )
    return True


def run_completed_successfully(
    run_data: dict[str, Any], *, quality_required: bool
) -> tuple[bool, str]:
    conclusion = run_data.get("conclusion")
    if conclusion == "success":
        return True, "success"
    jobs = run_data.get("jobs")
    if not isinstance(jobs, list):
        return False, f"workflow conclusion is {conclusion}"
    failed = [job for job in jobs if job.get("conclusion") not in ("success", "skipped")]
    if not failed:
        return conclusion == "success", f"workflow conclusion is {conclusion}"
    if not quality_required and all(
        "quality" in str(job.get("name", "")).lower() for job in failed
    ):
        return True, "only optional quality jobs failed"
    failed_names = ", ".join(str(job.get("name", "unknown")) for job in failed)
    return False, f"failed jobs: {failed_names}"


def publish_job_seconds(run_data: dict[str, Any]) -> float | None:
    """Return observed APT publish job time without conflating index visibility.

    Product workflows are not required to expose a publish timing output, but
    GitHub's job metadata includes timestamps.  Only a clearly named APT publish
    job is used; otherwise the metric remains unavailable rather than reporting
    APT propagation time as publish time.
    """

    jobs = run_data.get("jobs")
    if not isinstance(jobs, list):
        return None
    total = 0.0
    matched = False
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = str(job.get("name", "")).lower()
        if "publish" not in name or "apt" not in name:
            continue
        started = job.get("startedAt") or job.get("started_at")
        completed = job.get("completedAt") or job.get("completed_at")
        if not isinstance(started, str) or not isinstance(completed, str):
            continue
        try:
            start_time = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
            completed_time = dt.datetime.fromisoformat(completed.replace("Z", "+00:00"))
        except ValueError:
            continue
        duration = (completed_time - start_time).total_seconds()
        if duration < 0:
            continue
        total += duration
        matched = True
    return round(total, 3) if matched else None


def failed_run_is_transient(product: dict[str, Any], run_id: int) -> bool:
    result = run(
        [
            "gh",
            "run",
            "view",
            str(run_id),
            "--repo",
            str(product["repository"]),
            "--log-failed",
        ],
        check=False,
    )
    return is_transient_message(command_details(result))


def wait_for_run(
    product: dict[str, Any],
    run_id: int,
    *,
    timeout_seconds: int,
    poll_seconds: int,
    quality_required: bool,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    print(f"{product['id']}: waiting for {product['repository']} run {run_id}")
    while time.time() < deadline:
        result = run(
            [
                "gh",
                "run",
                "view",
                str(run_id),
                "--repo",
                str(product["repository"]),
                "--json",
                "status,conclusion,jobs,url,number",
            ],
            check=False,
        )
        if result.returncode != 0:
            details = command_details(result)
            if is_transient_message(details):
                print(f"{product['id']}: transient run-status error: {details}")
                time.sleep(poll_seconds)
                continue
            raise ReleaseError(f"{product['id']}: cannot inspect workflow run {run_id}: {details}")
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"{product['id']}: invalid workflow run JSON") from exc
        if data.get("status") == "completed":
            ok, reason = run_completed_successfully(data, quality_required=quality_required)
            if ok:
                print(f"{product['id']}: workflow completed ({reason})")
                return data
            message = f"{product['id']}: workflow failed ({reason}) {data.get('url')}"
            if failed_run_is_transient(product, run_id):
                raise CompletedTransientReleaseError(message)
            raise ReleaseError(message)
        time.sleep(poll_seconds)
    raise TransientReleaseError(
        f"{product['id']}: workflow run {run_id} timed out; refusing to dispatch a duplicate"
    )


def run_attempt(product: dict[str, Any], run_id: int) -> dict[str, Any]:
    result = run(
        ["gh", "api", f"repos/{product['repository']}/actions/runs/{run_id}"],
        check=False,
    )
    if result.returncode != 0:
        details = command_details(result)
        error = TransientReleaseError if is_transient_message(details) else ReleaseError
        raise error(f"{product['id']}: cannot inspect CI run attempt: {details}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"{product['id']}: invalid CI run attempt JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"{product['id']}: invalid CI run attempt response")
    return value


def wait_for_new_attempt(
    product: dict[str, Any],
    run_id: int,
    *,
    prior_attempt: int,
    timeout_seconds: int,
    poll_seconds: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        value = run_attempt(product, run_id)
        attempt = int(value.get("run_attempt", 0) or 0)
        if attempt > prior_attempt and value.get("status") == "completed":
            if value.get("conclusion") != "success":
                raise ReleaseError(
                    f"{product['id']}: reconciled push CI run {run_id} attempt {attempt} "
                    f"failed with {value.get('conclusion')} ({value.get('html_url')})"
                )
            return value
        time.sleep(poll_seconds)
    raise TransientReleaseError(
        f"{product['id']}: reconciled CI run {run_id} did not complete in {timeout_seconds}s"
    )


def latest_push_ci_run(product: dict[str, Any]) -> dict[str, Any] | None:
    expected_sha = str(product.get("expected_source_sha", ""))
    result = run(
        [
            "gh", "run", "list", "--repo", str(product["repository"]),
            "--workflow", str(product.get("ci_workflow") or "ci.yml"),
            "--event", "push", "--commit", expected_sha, "--limit", "20",
            "--json", "databaseId,status,conclusion,headSha,url,createdAt,event",
        ],
        check=False,
    )
    if result.returncode != 0:
        details = command_details(result)
        error = TransientReleaseError if is_transient_message(details) else ReleaseError
        raise error(f"{product['id']}: cannot list push CI for reconciliation: {details}")
    try:
        values = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"{product['id']}: invalid push CI list JSON") from exc
    matching = [
        value for value in values
        if isinstance(value, dict)
        and value.get("event") in (None, "", "push")
        and str(value.get("headSha", "")) == expected_sha
        and isinstance(value.get("databaseId"), int)
    ]
    matching.sort(key=lambda value: str(value.get("createdAt", "")), reverse=True)
    return matching[0] if matching else None


def dispatch_ci_for_reconciliation(product: dict[str, Any]) -> int:
    # Dispatch is safe only while the named ref still resolves to the locked SHA.
    verify_release_lock_is_current(product)
    result = run(
        [
            "gh", "workflow", "run", str(product.get("ci_workflow") or "ci.yml"),
            "--repo", str(product["repository"]), "--ref", str(product["ref"]),
        ],
        check=False,
    )
    if result.returncode != 0:
        details = command_details(result)
        error = TransientReleaseError if is_transient_message(details) else ReleaseError
        raise error(f"{product['id']}: cannot dispatch missing push CI: {details}")
    match = re.search(r"/actions/runs/(\d+)", command_details(result))
    if not match:
        raise ReleaseError(
            f"{product['id']}: CI dispatch returned no exact run ID; refusing to guess"
        )
    return int(match.group(1))


def reconcile_push_ci(args: argparse.Namespace, product: dict[str, Any], plan_path: Path) -> int:
    checkpoint = load_ci_reconciliation_checkpoint(plan_path, product)
    if checkpoint and checkpoint.get("phase") in {"dispatched", "green"}:
        checkpoint_run_id = int(checkpoint["run_id"])
        exact = run_attempt(product, checkpoint_run_id)
        if exact.get("status") == "completed" and exact.get("conclusion") == "success":
            write_ci_reconciliation_checkpoint(
                plan_path, product, run_id=checkpoint_run_id, phase="green"
            )
            emit_result(
                {
                    "ci_reconciled": True,
                    "ci_run_id": checkpoint_run_id,
                    "ci_action": "checkpoint",
                }
            )
            return 0
        if checkpoint.get("phase") == "green":
            raise ReleaseError(
                f"{product['id']}: previously green reconciliation run is no longer successful"
            )
        wait_for_run(
            product,
            checkpoint_run_id,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
            quality_required=True,
        )
        write_ci_reconciliation_checkpoint(
            plan_path, product, run_id=checkpoint_run_id, phase="green"
        )
        emit_result(
            {
                "ci_reconciled": True,
                "ci_run_id": checkpoint_run_id,
                "ci_action": "checkpoint-wait",
            }
        )
        return 0
    latest = latest_push_ci_run(product)
    if latest and latest.get("status") == "completed" and latest.get("conclusion") == "success":
        run_id = int(latest["databaseId"])
        write_ci_reconciliation_checkpoint(plan_path, product, run_id=run_id, phase="green")
        emit_result({"ci_reconciled": True, "ci_run_id": run_id, "ci_action": "existing"})
        return 0

    if latest is None:
        run_id = dispatch_ci_for_reconciliation(product)
        write_ci_reconciliation_checkpoint(plan_path, product, run_id=run_id, phase="dispatched")
        data = wait_for_run(
            product,
            run_id,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
            quality_required=True,
        )
        write_ci_reconciliation_checkpoint(plan_path, product, run_id=run_id, phase="green")
        emit_result({"ci_reconciled": True, "ci_run_id": run_id, "ci_action": "dispatched"})
        return 0

    run_id = int(latest["databaseId"])
    if latest.get("status") != "completed":
        wait_for_run(
            product,
            run_id,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
            quality_required=True,
        )
        write_ci_reconciliation_checkpoint(plan_path, product, run_id=run_id, phase="green")
        emit_result({"ci_reconciled": True, "ci_run_id": run_id, "ci_action": "waited"})
        return 0

    if checkpoint and checkpoint.get("run_id") == run_id and checkpoint.get("phase") == "rerun":
        prior_attempt = int(checkpoint.get("prior_attempt", 0))
    else:
        attempt_data = run_attempt(product, run_id)
        prior_attempt = int(attempt_data.get("run_attempt", 1) or 1)
        result = run(
            ["gh", "run", "rerun", str(run_id), "--repo", str(product["repository"])],
            check=False,
        )
        if result.returncode != 0:
            details = command_details(result)
            error = TransientReleaseError if is_transient_message(details) else ReleaseError
            raise error(f"{product['id']}: cannot rerun failed push CI {run_id}: {details}")
        write_ci_reconciliation_checkpoint(
            plan_path,
            product,
            run_id=run_id,
            phase="rerun",
            prior_attempt=prior_attempt,
        )
    wait_for_new_attempt(
        product,
        run_id,
        prior_attempt=prior_attempt,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )
    write_ci_reconciliation_checkpoint(plan_path, product, run_id=run_id, phase="green")
    emit_result({"ci_reconciled": True, "ci_run_id": run_id, "ci_action": "rerun"})
    return 0


def apt_stanzas(base_url: str, distribution: str, arch: str) -> list[dict[str, str]]:
    url = f"{base_url.rstrip('/')}/dists/{distribution}/main/binary-{arch}/Packages"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in {408, 429, 502, 503, 504}:
            raise TransientReleaseError(f"transient APT index response for {url}: {exc}") from exc
        raise ReleaseError(f"APT index request failed for {url}: {exc}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError) as exc:
        raise TransientReleaseError(f"cannot read APT index {url}: {exc}") from exc
    stanzas: list[dict[str, str]] = []
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line or line.startswith(" "):
                continue
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
        if fields:
            stanzas.append(fields)
    return stanzas


def expected_version(
    product: dict[str, Any], distribution: str, run_number: int | None
) -> str | None:
    apt_versions = product.get("apt_versions")
    if isinstance(apt_versions, dict) and distribution in apt_versions:
        return str(apt_versions[distribution])
    template = product.get("apt_version_template")
    if template:
        if run_number is None:
            return None
        return str(template).format(
            distribution=distribution,
            run_number=run_number,
            version=product.get("version", ""),
        )
    return str(product.get("expected_version") or product.get("version", "")) or None


def expected_versions(product: dict[str, Any], run_number: int | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for distribution in product.get("apt_distributions", []):
        version = expected_version(product, str(distribution), run_number)
        if version:
            result[str(distribution)] = version
    return result


def package_architectures(product: dict[str, Any], package: str) -> tuple[str, ...]:
    raw = product.get("apt_package_architectures", {})
    if not isinstance(raw, dict):
        raise ReleaseError(f"{product['id']}: invalid apt package architecture map")
    values = raw.get(package, DEFAULT_ARCHES)
    if (
        not isinstance(values, (list, tuple))
        or not values
        or any(str(value) not in DEFAULT_ARCHES for value in values)
    ):
        raise ReleaseError(f"{product['id']}: invalid architectures for {package}")
    arches = tuple(arch for arch in DEFAULT_ARCHES if arch in set(map(str, values)))
    if package in set(map(str, product.get("apt_install", []))) and set(arches) != set(
        DEFAULT_ARCHES
    ):
        raise ReleaseError(f"{product['id']}: install package {package} must be dual-arch")
    return arches


def package_distributions(product: dict[str, Any], package: str) -> tuple[str, ...]:
    all_distributions = tuple(map(str, product.get("apt_distributions", [])))
    raw = product.get("apt_package_distributions", {})
    if not isinstance(raw, dict):
        raise ReleaseError(f"{product['id']}: invalid apt package distribution map")
    values = raw.get(package, all_distributions)
    if (
        not isinstance(values, (list, tuple))
        or not values
        or any(str(value) not in all_distributions for value in values)
    ):
        raise ReleaseError(f"{product['id']}: invalid distributions for {package}")
    distributions = tuple(
        value for value in all_distributions if value in set(map(str, values))
    )
    return distributions


def manifest_url(
    product: dict[str, Any],
    *,
    manifest_base_url: str,
    distribution: str,
    arch: str,
    package: str,
    version: str,
) -> str:
    return (
        f"{manifest_base_url.rstrip('/')}/{product['id']}/{distribution}/{arch}/"
        f"{package}_{version}.json"
    )


def read_release_manifest(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = response.read().decode("utf-8", errors="strict")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code in {408, 429, 502, 503, 504}:
            raise TransientReleaseError(f"transient manifest response for {url}: {exc}") from exc
        raise ReleaseError(f"cannot read release manifest {url}: {exc}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError) as exc:
        raise TransientReleaseError(f"cannot read release manifest {url}: {exc}") from exc
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"release manifest is corrupt at {url}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReleaseError(f"release manifest is not an object at {url}")
    if data.get("schema") != "xgc2.release-artifact.v1":
        raise ReleaseError(f"unsupported release manifest schema at {url}")
    return data


def release_manifest_matches(
    manifest: dict[str, Any],
    *,
    product: dict[str, Any],
    distribution: str,
    arch: str,
    package: str,
    version: str,
    apt_sha256: str,
    require_current_lock: bool,
) -> bool:
    expected_lock_digest = os.environ.get("XGC2_RELEASE_LOCK_DIGEST", "")
    identity = {
        "product": str(product["id"]),
        "source_sha": str(product.get("expected_source_sha", "")),
        "version": str(product.get("expected_version") or product.get("version", "")),
        "distribution": distribution,
        "architecture": arch,
    }
    if any(str(manifest.get(key, "")) != value for key, value in identity.items()):
        return False
    if (
        require_current_lock
        and expected_lock_digest
        and str(manifest.get("release_lock_digest", "")) != expected_lock_digest
    ):
        return False
    if not str(manifest.get("build_manifest_digest", "")):
        return False
    debs = manifest.get("debs")
    if not isinstance(debs, list):
        raise ReleaseError(f"{product['id']}: release manifest debs must be an array")
    for deb in debs:
        if not isinstance(deb, dict):
            continue
        if (
            str(deb.get("package", "")) == package
            and str(deb.get("version", "")) == version
            and str(deb.get("architecture", "")) in {arch, "all"}
            and str(deb.get("sha256", "")) == apt_sha256
        ):
            return True
    return False


def package_release_visible(
    product: dict[str, Any],
    *,
    apt_base_url: str,
    manifest_base_url: str,
    distribution: str,
    arch: str,
    package: str,
    version: str,
    require_current_lock: bool,
    strict_manifest_mismatch: bool,
    apt_index: list[dict[str, str]] | None = None,
) -> bool:
    stanzas = (
        apt_index
        if apt_index is not None
        else apt_stanzas(apt_base_url, distribution, arch)
    )
    stanza = next(
        (
            item
            for item in stanzas
            if item.get("Package") == package and item.get("Version") == version
        ),
        None,
    )
    if stanza is None:
        return False
    apt_sha256 = str(stanza.get("SHA256", ""))
    if not apt_sha256:
        raise ReleaseError(
            f"{product['id']}: APT stanza lacks SHA256 for {package}={version} ({arch})"
        )
    url = manifest_url(
        product,
        manifest_base_url=manifest_base_url,
        distribution=distribution,
        arch=arch,
        package=package,
        version=version,
    )
    manifest = read_release_manifest(url)
    if manifest is None:
        return False
    matches = release_manifest_matches(
        manifest,
        product=product,
        distribution=distribution,
        arch=arch,
        package=package,
        version=version,
        apt_sha256=apt_sha256,
        require_current_lock=require_current_lock,
    )
    if not matches and strict_manifest_mismatch:
        raise ReleaseError(
            f"{product['id']}: visible release manifest identity/hash mismatch for "
            f"{distribution}/{arch}:{package}={version}"
        )
    return matches


def fast_pass_ready(
    product: dict[str, Any],
    *,
    apt_base_url: str,
    manifest_base_url: str,
    arches: tuple[str, ...],
) -> bool:
    if not str(product.get("expected_source_sha", "")):
        return False
    versions = expected_versions(product, None)
    packages = product.get("apt_packages", [])
    if not versions or not packages:
        return False
    apt_indexes: dict[tuple[str, str], list[dict[str, str]]] = {}
    for distribution, version in sorted(versions.items()):
        for arch in arches:
            key = (distribution, arch)
            apt_indexes[key] = apt_stanzas(apt_base_url, distribution, arch)
            for package in packages:
                if (
                    distribution not in package_distributions(product, str(package))
                    or arch not in package_architectures(product, str(package))
                ):
                    continue
                if not package_release_visible(
                    product,
                    apt_base_url=apt_base_url,
                    manifest_base_url=manifest_base_url,
                    distribution=distribution,
                    arch=arch,
                    package=str(package),
                    version=version,
                    require_current_lock=True,
                    strict_manifest_mismatch=False,
                    apt_index=apt_indexes[key],
                ):
                    return False
    return True


def verify_apt(
    product: dict[str, Any],
    *,
    apt_base_url: str,
    manifest_base_url: str,
    arches: tuple[str, ...],
    timeout_seconds: int,
    poll_seconds: int,
    run_number: int | None,
    require_current_lock: bool,
) -> None:
    if product.get("skip_apt_verify"):
        print(f"{product['id']}: apt verification skipped by product metadata")
        return
    packages = product.get("apt_packages", [])
    distributions = product.get("apt_distributions", [])
    if not packages or not distributions:
        return
    pending: set[tuple[str, str, str, str]] = set()
    for distribution in distributions:
        version = expected_version(product, str(distribution), run_number)
        if not version:
            raise ReleaseError(f"{product['id']}: missing expected apt version for {distribution}")
        for arch in arches:
            for package in packages:
                if (
                    str(distribution) not in package_distributions(product, str(package))
                    or arch not in package_architectures(product, str(package))
                ):
                    continue
                pending.add((str(distribution), arch, str(package), version))

    deadline = time.time() + timeout_seconds
    last_transient = ""
    while pending and time.time() < deadline:
        apt_indexes: dict[tuple[str, str], list[dict[str, str]]] = {}
        for distribution, arch in sorted({(item[0], item[1]) for item in pending}):
            try:
                apt_indexes[(distribution, arch)] = apt_stanzas(
                    apt_base_url, distribution, arch
                )
            except TransientReleaseError as exc:
                last_transient = str(exc)
                print(f"{product['id']}: APT visibility retry: {exc}")
        for item in list(pending):
            distribution, arch, package, version = item
            apt_index = apt_indexes.get((distribution, arch))
            if apt_index is None:
                continue
            try:
                if package_release_visible(
                    product,
                    apt_base_url=apt_base_url,
                    manifest_base_url=manifest_base_url,
                    distribution=distribution,
                    arch=arch,
                    package=package,
                    version=version,
                    require_current_lock=require_current_lock,
                    strict_manifest_mismatch=True,
                    apt_index=apt_index,
                ):
                    pending.remove(item)
            except TransientReleaseError as exc:
                last_transient = str(exc)
                print(f"{product['id']}: APT visibility retry: {exc}")
        if pending:
            time.sleep(poll_seconds)
    if pending:
        missing = ", ".join(
            f"{dist}/{arch}:{package}={version}"
            for dist, arch, package, version in sorted(pending)
        )
        suffix = f"; last transient error: {last_transient}" if last_transient else ""
        raise TransientReleaseError(
            f"{product['id']}: APT index/manifest not visible for {missing}{suffix}"
        )
    print(f"{product['id']}: APT indexes and release manifests are visible for all arches")


def emit_result(data: dict[str, Any]) -> None:
    print(RESULT_MARKER + json.dumps(data, separators=(",", ":"), sort_keys=True))


def execute_central(args: argparse.Namespace) -> int:
    """Prepare one node for a centralized, all-or-nothing release train."""

    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        raise ReleaseError("GH_TOKEN or GITHUB_TOKEN is required")
    release_id = os.environ.get("XGC2_RELEASE_ID", "")
    lock_digest = os.environ.get("XGC2_RELEASE_LOCK_DIGEST", "")
    if not release_id or not lock_digest:
        raise ReleaseError("XGC2_RELEASE_ID and XGC2_RELEASE_LOCK_DIGEST are required")
    plan_path = Path(args.plan).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    product = find_product(plan, args.product)
    if args.reconcile_ci:
        return reconcile_push_ci(args, product, plan_path)
    arches = tuple(args.apt_arch or DEFAULT_ARCHES)
    work_root = plan_path.parent
    overlay_url = args.apt_overlay_url or (
        f"{args.apt_base_url.rstrip('/')}/staging/{urllib.parse.quote(release_id, safe='')}"
    )
    if args.verify_lock_only:
        verify_release_lock_is_current(product)
        emit_result({"source_lock_current": True})
        return 0
    metric: dict[str, Any] = {
        "fast_pass": False,
        "reused_ci_artifact": False,
        "ci_run_id": None,
        "release_run_id": None,
        "ci_artifact_wait_seconds": 0.0,
        "release_workflow_seconds": 0.0,
        "apt_visibility_seconds": 0.0,
        "reuse_seconds": 0.0,
        "build_seconds": 0.0,
        "publish_seconds": 0.0,
        "stage_seconds": 0.0,
        "artifact_download_seconds": 0.0,
    }
    checkpoint = load_node_checkpoint(plan_path, product)
    if args.verify_production:
        if product.get("action") != RELEASE_ACTION:
            emit_result(metric)
            return 0
        started = time.monotonic()
        verify_apt(
            product,
            apt_base_url=args.apt_base_url,
            manifest_base_url=args.manifest_base_url,
            arches=arches,
            timeout_seconds=args.apt_timeout_seconds,
            poll_seconds=args.poll_seconds,
            run_number=None,
            require_current_lock=True,
        )
        metric["apt_visibility_seconds"] = round(time.monotonic() - started, 3)
        write_node_checkpoint(plan_path, product, phase="production_verified")
        metric["prepare_checkpoint"] = "production_verified"
        emit_result(metric)
        return 0

    if checkpoint and checkpoint["phase"] == "production_verified":
        metric["fast_pass"] = True
        metric["checkpoint"] = "production_verified"
        metric["prepare_checkpoint"] = "production_verified"
        emit_result(metric)
        return 0
    if product.get("action") == VERIFY_ACTION:
        started = time.monotonic()
        verify_apt(
            product,
            apt_base_url=args.apt_base_url,
            manifest_base_url=args.manifest_base_url,
            arches=arches,
            timeout_seconds=args.apt_timeout_seconds,
            poll_seconds=args.poll_seconds,
            run_number=None,
            require_current_lock=False,
        )
        metric["apt_visibility_seconds"] = round(time.monotonic() - started, 3)
        write_node_checkpoint(plan_path, product, phase="compatibility_verified")
        metric["prepare_checkpoint"] = "compatibility_verified"
        emit_result(metric)
        return 0
    if checkpoint and checkpoint["phase"] in {"staged", "compatibility_verified"}:
        if checkpoint["phase"] == "staged":
            status_result = subprocess_checked(
                [
                    sys.executable,
                    os.fspath(Path(__file__).with_name("release-train-publisher.py")),
                    "status",
                    "--release-id",
                    release_id,
                    "--release-lock-digest",
                    lock_digest,
                    "--validate-json",
                ],
                product_id=str(product["id"]),
            )
            try:
                status = json.loads(status_result.stdout)
            except json.JSONDecodeError as exc:
                raise ReleaseError(f"{product['id']}: stage status is not valid JSON") from exc
            receipt_path = plan_path.parent / "release-stage-receipts" / f"{product['id']}.json"
            if not receipt_path.is_file():
                raise ReleaseError(f"{product['id']}: staged checkpoint is missing its receipt")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            expected_products = [
                item for item in receipt.get("products", []) if isinstance(item, dict)
            ]
            expected_digests = {str(item.get("bundle_digest", "")) for item in expected_products}
            bundles = status.get("bundles") if isinstance(status, dict) else None
            distributions = status.get("distributions") if isinstance(status, dict) else None
            if (
                not expected_digests
                or not isinstance(bundles, dict)
                or not isinstance(distributions, dict)
                or status.get("status") not in {"prepared", "promoted"}
                or any(
                    not isinstance(bundles.get(str(item.get("bundle_digest", ""))), dict)
                    or not bundles[str(item.get("bundle_digest", ""))].get("manifests")
                    or bundles[str(item.get("bundle_digest", ""))].get("product")
                    != {
                        key: item[key]
                        for key in (
                            "product", "distribution", "version", "source_sha",
                            "bundle_digest", "build_manifest_digests", "debs",
                        )
                    }
                    or not isinstance(distributions.get(str(item.get("distribution", ""))), dict)
                    or distributions[str(item.get("distribution", ""))].get("published") is not True
                    for item in expected_products
                )
            ):
                raise ReleaseError(
                    f"{product['id']}: server stage status does not contain checkpointed bundles"
                )
            visibility_started = time.monotonic()
            verify_apt(
                product,
                apt_base_url=overlay_url,
                manifest_base_url=f"{overlay_url.rstrip('/')}/manifests",
                arches=arches,
                timeout_seconds=args.apt_timeout_seconds,
                poll_seconds=args.poll_seconds,
                run_number=None,
                require_current_lock=True,
            )
            metric["apt_visibility_seconds"] = round(
                time.monotonic() - visibility_started, 3
            )
        metric["fast_pass"] = True
        metric["checkpoint"] = checkpoint["phase"]
        metric["prepare_checkpoint"] = checkpoint["phase"]
        if isinstance(checkpoint.get("run_id"), int):
            metric["release_run_id"] = checkpoint["run_id"]
        emit_result(metric)
        return 0

    if (
        checkpoint is None
        and product.get("action") == RELEASE_ACTION
        and recover_server_staged_receipt(
            plan_path,
            product,
            release_id=release_id,
            lock_digest=lock_digest,
            overlay_url=overlay_url,
            arches=arches,
            apt_timeout_seconds=args.apt_timeout_seconds,
            poll_seconds=args.poll_seconds,
        )
    ):
        metric["fast_pass"] = True
        metric["server_stage_recovered"] = True
        metric["prepare_checkpoint"] = "staged"
        emit_result(metric)
        return 0

    verify_release_lock_is_current(product)
    run_id: int | None = None
    artifact_source = "fallback"
    workflow_data: dict[str, Any] | None = None
    if checkpoint and checkpoint["phase"] in {"workflow_dispatched", "artifact_ready"}:
        stored_run_id = checkpoint.get("run_id")
        if not isinstance(stored_run_id, int):
            raise ReleaseError(f"{product['id']}: checkpoint lacks exact run id")
        run_id = stored_run_id
        artifact_source = str(checkpoint.get("artifact_source", "fallback"))
        if checkpoint["phase"] == "workflow_dispatched":
            started = time.monotonic()
            workflow_data = wait_for_run(
                product,
                run_id,
                timeout_seconds=args.timeout_seconds,
                poll_seconds=args.poll_seconds,
                quality_required=args.quality_required,
            )
            metric["release_workflow_seconds"] = round(time.monotonic() - started, 3)
            write_node_checkpoint(
                plan_path,
                product,
                phase="artifact_ready",
                run_id=run_id,
                artifact_source=artifact_source,
            )
    elif product.get("action") == RELEASE_ACTION and args.reuse_ci_artifacts and not product.get(
        "release_scoped_build", False
    ):
        started = time.monotonic()
        run_id = find_trusted_ci_run(
            product,
            wait_seconds=args.ci_wait_seconds,
            poll_seconds=args.poll_seconds,
            artifact_cache_root=(
                work_root / "release-run-artifacts" / str(product["id"])
            ),
        )
        metric["ci_artifact_wait_seconds"] = round(time.monotonic() - started, 3)
        if run_id is not None:
            artifact_source = "push-ci"
            metric["ci_run_id"] = run_id
            metric["reused_ci_artifact"] = True
            write_node_checkpoint(
                plan_path,
                product,
                phase="artifact_ready",
                run_id=run_id,
                artifact_source=artifact_source,
            )

    if run_id is None:
        run_id = trigger_prepare(
            product,
            quality_required=args.quality_required,
            source_tests=args.source_tests,
            apt_overlay_url=overlay_url,
        )
        artifact_source = "fallback"
        metric["release_run_id"] = run_id
        write_node_checkpoint(
            plan_path,
            product,
            phase="workflow_dispatched",
            run_id=run_id,
            artifact_source=artifact_source,
        )
        started = time.monotonic()
        workflow_data = wait_for_run(
            product,
            run_id,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
            quality_required=args.quality_required,
        )
        metric["release_workflow_seconds"] = round(time.monotonic() - started, 3)
        metric["build_seconds"] = metric["release_workflow_seconds"]
        write_node_checkpoint(
            plan_path,
            product,
            phase="artifact_ready",
            run_id=run_id,
            artifact_source=artifact_source,
        )
    elif artifact_source == "push-ci":
        metric["ci_run_id"] = run_id
        metric["reused_ci_artifact"] = True
        metric["reuse_seconds"] = metric["ci_artifact_wait_seconds"]

    if product.get("action") == COMPATIBILITY_VERIFY_ACTION:
        write_node_checkpoint(
            plan_path,
            product,
            phase="compatibility_verified",
            run_id=run_id,
            artifact_source=artifact_source,
        )
        metric["prepare_checkpoint"] = "compatibility_verified"
        emit_result(metric)
        return 0

    artifact_dir = work_root / "release-run-artifacts" / str(product["id"]) / str(run_id)
    if not (artifact_source == "push-ci" and artifact_dir.is_dir()):
        download_started = time.monotonic()
        download_run_artifacts(product, run_id, artifact_dir)
        metric["artifact_download_seconds"] = round(
            time.monotonic() - download_started, 3
        )
        if artifact_source == "push-ci":
            metric["reuse_seconds"] = round(
                metric["reuse_seconds"] + metric["artifact_download_seconds"], 3
            )
    bundle_dir = work_root / "release-bundles" / str(product["id"])
    receipt_dir = work_root / "release-stage-receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt = receipt_dir / f"{product['id']}.json"
    stage_tool = Path(__file__).with_name("stage-product-release.py")
    stage_started = time.monotonic()
    subprocess_checked(
        [
            sys.executable,
            os.fspath(stage_tool),
            "prepare",
            "--plan",
            os.fspath(plan_path),
            "--product",
            str(product["id"]),
            "--artifact-dir",
            os.fspath(artifact_dir),
            "--run-id",
            str(run_id),
            "--release-id",
            release_id,
            "--release-lock-digest",
            lock_digest,
            "--published-at",
            os.environ.get("XGC2_RELEASE_CREATED_AT", ""),
            "--output-dir",
            os.fspath(bundle_dir),
            "--receipt",
            os.fspath(receipt),
        ],
        product_id=str(product["id"]),
    )
    receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
    publisher = Path(__file__).with_name("release-train-publisher.py")
    for staged_product in receipt_value.get("products", []):
        subprocess_checked(
            [
                sys.executable,
                os.fspath(publisher),
                "stage",
                "--release-id",
                release_id,
                "--release-lock-digest",
                lock_digest,
                "--distribution",
                str(staged_product["distribution"]),
                "--bundle",
                str(staged_product["bundle_dir"]),
            ],
            product_id=str(product["id"]),
        )
    write_node_checkpoint(
        plan_path,
        product,
        phase="staged",
        run_id=run_id,
        receipt=receipt.name,
        artifact_source=artifact_source,
    )
    metric["stage_seconds"] = round(time.monotonic() - stage_started, 3)
    visibility_started = time.monotonic()
    verify_apt(
        product,
        apt_base_url=overlay_url,
        manifest_base_url=f"{overlay_url.rstrip('/')}/manifests",
        arches=arches,
        timeout_seconds=args.apt_timeout_seconds,
        poll_seconds=args.poll_seconds,
        run_number=None,
        require_current_lock=True,
    )
    metric["apt_visibility_seconds"] = round(
        time.monotonic() - visibility_started, 3
    )
    metric["prepare_checkpoint"] = "staged"
    emit_result(metric)
    return 0


def execute(args: argparse.Namespace) -> int:
    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        raise ReleaseError("GH_TOKEN or GITHUB_TOKEN is required")
    plan_path = Path(args.plan)
    with plan_path.open("r", encoding="utf-8") as handle:
        plan = json.load(handle)
    product = find_product(plan, args.product)
    arches = tuple(args.apt_arch or DEFAULT_ARCHES)
    metric: dict[str, Any] = {
        "fast_pass": False,
        "reused_ci_artifact": False,
        "ci_run_id": None,
        "release_run_id": None,
        "ci_artifact_wait_seconds": 0.0,
        "release_workflow_seconds": 0.0,
        "apt_visibility_seconds": 0.0,
        "reuse_seconds": 0.0,
        "build_seconds": 0.0,
        "publish_seconds": None,
    }

    if product.get("action") == VERIFY_ACTION:
        print(f"{product['id']}: verify-only target; no workflow dispatch")
        started = time.monotonic()
        if not args.skip_apt_verify:
            verify_apt(
                product,
                apt_base_url=args.apt_base_url,
                manifest_base_url=args.manifest_base_url,
                arches=arches,
                timeout_seconds=args.apt_timeout_seconds,
                poll_seconds=args.poll_seconds,
                run_number=None,
                require_current_lock=False,
            )
        metric["apt_visibility_seconds"] = round(time.monotonic() - started, 3)
        metric["publish_seconds"] = 0.0
        emit_result(metric)
        return 0

    verify_release_lock_is_current(product)
    checkpoint = load_publish_checkpoint(plan_path, product)
    if checkpoint is not None:
        run_id = int(checkpoint["release_run_id"])
        metric["release_run_id"] = run_id
        trusted_ci_run_id = checkpoint.get("trusted_ci_run_id")
        if isinstance(trusted_ci_run_id, int):
            metric["ci_run_id"] = trusted_ci_run_id
            metric["reused_ci_artifact"] = True
        metric["ci_artifact_wait_seconds"] = float(
            checkpoint.get("ci_artifact_wait_seconds", 0.0)
        )
        release_run_number = checkpoint.get("release_run_number")
        if checkpoint["phase"] == "dispatched":
            print(
                f"{product['id']}: resuming exact release run {run_id}; "
                "a duplicate will not be dispatched"
            )
            try:
                data = wait_for_run(
                    product,
                    run_id,
                    timeout_seconds=args.timeout_seconds,
                    poll_seconds=args.poll_seconds,
                    quality_required=args.quality_required,
                )
            except CompletedTransientReleaseError:
                # The exact run is known to be complete, so a scheduler retry may
                # safely create a replacement rather than re-waiting a dead run.
                clear_publish_checkpoint(plan_path, product)
                raise
            release_run_number = data.get("number")
            dispatched_at = checkpoint.get("dispatched_at")
            if isinstance(dispatched_at, (int, float)):
                metric["release_workflow_seconds"] = round(
                    max(0.0, time.time() - float(dispatched_at)), 3
                )
            metric["publish_seconds"] = publish_job_seconds(data)
            write_publish_checkpoint(
                plan_path,
                product,
                release_run_id=run_id,
                release_run_number=(
                    release_run_number if isinstance(release_run_number, int) else None
                ),
                phase="workflow_succeeded",
                trusted_ci_run_id=(
                    trusted_ci_run_id if isinstance(trusted_ci_run_id, int) else None
                ),
                dispatched_at=(
                    float(dispatched_at)
                    if isinstance(dispatched_at, (int, float))
                    else None
                ),
                release_workflow_seconds=metric["release_workflow_seconds"],
                publish_seconds=metric["publish_seconds"],
                ci_artifact_wait_seconds=metric["ci_artifact_wait_seconds"],
            )
            metric["resumed_release_run"] = True
        else:
            print(
                f"{product['id']}: resuming APT visibility verification after release run "
                f"{run_id}"
            )
            metric["release_workflow_seconds"] = float(
                checkpoint.get("release_workflow_seconds", 0.0)
            )
            stored_publish = checkpoint.get("publish_seconds")
            metric["publish_seconds"] = (
                float(stored_publish) if isinstance(stored_publish, (int, float)) else None
            )
            metric["resumed_publish_verification"] = True
        if metric["reused_ci_artifact"]:
            metric["reuse_seconds"] = round(
                metric["ci_artifact_wait_seconds"] + metric["release_workflow_seconds"], 3
            )
        else:
            metric["build_seconds"] = metric["release_workflow_seconds"]
        verify_started = time.monotonic()
        if not args.skip_apt_verify:
            verify_apt(
                product,
                apt_base_url=args.apt_base_url,
                manifest_base_url=args.manifest_base_url,
                arches=arches,
                timeout_seconds=args.apt_timeout_seconds,
                poll_seconds=args.poll_seconds,
                run_number=(
                    release_run_number if isinstance(release_run_number, int) else None
                ),
                require_current_lock=True,
            )
        metric["apt_visibility_seconds"] = round(time.monotonic() - verify_started, 3)
        emit_result(metric)
        return 0
    if getattr(args, "verify_existing_release", False):
        if args.skip_apt_verify:
            raise ReleaseError(
                f"{product['id']}: resume verification cannot skip APT/manifest checks"
            )
        print(
            f"{product['id']}: re-verifying prior success against the current release lock"
        )
        verify_started = time.monotonic()
        verify_apt(
            product,
            apt_base_url=args.apt_base_url,
            manifest_base_url=args.manifest_base_url,
            arches=arches,
            timeout_seconds=args.apt_timeout_seconds,
            poll_seconds=args.poll_seconds,
            run_number=None,
            require_current_lock=True,
        )
        metric["apt_visibility_seconds"] = round(time.monotonic() - verify_started, 3)
        metric["publish_seconds"] = 0.0
        metric["resume_verified"] = True
        emit_result(metric)
        return 0
    if not args.no_fast_pass and fast_pass_ready(
        product,
        apt_base_url=args.apt_base_url,
        manifest_base_url=args.manifest_base_url,
        arches=arches,
    ):
        print(f"{product['id']}: FAST-PASS package, hash, source SHA, and release lock match")
        metric["fast_pass"] = True
        metric["publish_seconds"] = 0.0
        emit_result(metric)
        return 0

    trusted_ci_run_id: int | None = None
    workflow_inputs = set(product.get("workflow_inputs", []))
    supports_ci_reuse = bool({"trusted_ci_run_id", "ci_run_id"} & workflow_inputs)
    if args.reuse_ci_artifacts and supports_ci_reuse:
        started = time.monotonic()
        trusted_ci_run_id = find_trusted_ci_run(
            product,
            wait_seconds=args.ci_wait_seconds,
            poll_seconds=args.poll_seconds,
        )
        metric["ci_artifact_wait_seconds"] = round(time.monotonic() - started, 3)
        metric["ci_run_id"] = trusted_ci_run_id
        metric["reused_ci_artifact"] = trusted_ci_run_id is not None

    run_id = trigger(
        product,
        quality_required=args.quality_required,
        source_tests=args.source_tests,
        trusted_ci_run_id=trusted_ci_run_id,
    )
    metric["release_run_id"] = run_id
    print(f"{product['id']}: triggered run {run_id}")
    dispatched_at = time.time()
    write_publish_checkpoint(
        plan_path,
        product,
        release_run_id=run_id,
        release_run_number=None,
        phase="dispatched",
        trusted_ci_run_id=trusted_ci_run_id,
        dispatched_at=dispatched_at,
        ci_artifact_wait_seconds=metric["ci_artifact_wait_seconds"],
    )
    try:
        data = wait_for_run(
            product,
            run_id,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
            quality_required=args.quality_required,
        )
    except CompletedTransientReleaseError:
        clear_publish_checkpoint(plan_path, product)
        raise
    metric["release_workflow_seconds"] = round(
        max(0.0, time.time() - dispatched_at), 3
    )
    metric["publish_seconds"] = publish_job_seconds(data)
    release_run_number = data.get("number")
    write_publish_checkpoint(
        plan_path,
        product,
        release_run_id=run_id,
        release_run_number=release_run_number if isinstance(release_run_number, int) else None,
        phase="workflow_succeeded",
        trusted_ci_run_id=trusted_ci_run_id,
        dispatched_at=dispatched_at,
        release_workflow_seconds=metric["release_workflow_seconds"],
        publish_seconds=metric["publish_seconds"],
        ci_artifact_wait_seconds=metric["ci_artifact_wait_seconds"],
    )
    if trusted_ci_run_id is None:
        metric["build_seconds"] = metric["release_workflow_seconds"]
    else:
        metric["reuse_seconds"] = round(
            metric["ci_artifact_wait_seconds"] + metric["release_workflow_seconds"], 3
        )

    verify_started = time.monotonic()
    if not args.skip_apt_verify:
        run_number = release_run_number
        verify_apt(
            product,
            apt_base_url=args.apt_base_url,
            manifest_base_url=args.manifest_base_url,
            arches=arches,
            timeout_seconds=args.apt_timeout_seconds,
            poll_seconds=args.poll_seconds,
            run_number=run_number if isinstance(run_number, int) else None,
            require_current_lock=True,
        )
    metric["apt_visibility_seconds"] = round(time.monotonic() - verify_started, 3)
    emit_result(metric)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--quality-required", action="store_true")
    parser.add_argument("--source-tests", action="store_true")
    parser.add_argument("--reuse-ci-artifacts", action="store_true")
    parser.add_argument(
        "--central-prepare",
        action="store_true",
        help="prepare/stage artifacts for the xgc2-devops centralized publisher",
    )
    parser.add_argument(
        "--verify-production",
        action="store_true",
        help="verify this release target after the single global promotion",
    )
    parser.add_argument(
        "--verify-lock-only",
        action="store_true",
        help="verify the locked repository ref SHA without dispatching or publishing",
    )
    parser.add_argument(
        "--reconcile-ci",
        action="store_true",
        help="require an exact-source successful CI run after production promotion",
    )
    parser.add_argument("--apt-overlay-url", default="")
    parser.add_argument(
        "--verify-existing-release",
        action="store_true",
        help="Strictly re-verify a resumed success and never dispatch a new workflow",
    )
    parser.add_argument("--skip-apt-verify", action="store_true")
    parser.add_argument("--no-fast-pass", action="store_true")
    parser.add_argument("--apt-base-url", default=DEFAULT_APT_BASE_URL)
    parser.add_argument(
        "--manifest-base-url",
        default=f"{DEFAULT_APT_BASE_URL}/manifests",
        help="Base URL for release manifest checks",
    )
    parser.add_argument("--apt-arch", action="append", default=[])
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--ci-wait-seconds", type=int, default=1800)
    parser.add_argument("--apt-timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-seconds", type=int, default=15)
    args = parser.parse_args()
    try:
        if (
            args.central_prepare
            or args.verify_production
            or args.verify_lock_only
            or args.reconcile_ci
        ):
            return execute_central(args)
        return execute(args)
    except TransientReleaseError as exc:
        print(f"TRANSIENT: {exc}", file=sys.stderr)
        return TRANSIENT_EXIT_CODE
    except (ReleaseError, KeyError, ValueError, OSError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
