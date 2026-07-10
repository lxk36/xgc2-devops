#!/usr/bin/env python3
"""Trigger and verify one product workflow from an immutable release plan."""

from __future__ import annotations

import argparse
import json
import os
import re
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
}


class ReleaseError(RuntimeError):
    """A deterministic release error that must not be retried automatically."""


class TransientReleaseError(ReleaseError):
    """A network, APT propagation, or publish-lock error safe to retry."""


def node_checkpoint_path(plan_path: Path, product_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", product_id)
    return plan_path.resolve().parent / "release-node-checkpoints" / f"{safe_id}.json"


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
    return value


def write_publish_checkpoint(
    plan_path: Path,
    product: dict[str, Any],
    *,
    release_run_id: int,
    release_run_number: int | None,
) -> None:
    path = node_checkpoint_path(plan_path, str(product["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "schema": "xgc2.release-node-checkpoint.v1",
        "product": str(product["id"]),
        "source_sha": str(product.get("expected_source_sha", "")),
        "release_lock_digest": os.environ.get("XGC2_RELEASE_LOCK_DIGEST", ""),
        "release_run_id": release_run_id,
        "release_run_number": release_run_number,
        "completed_at": int(time.time()),
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


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
    patterns = (
        "connection reset",
        "connection refused",
        "connection timed out",
        "network is unreachable",
        "temporary failure",
        "temporarily unavailable",
        "tls handshake timeout",
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


def trusted_ci_artifacts_match(product: dict[str, Any], run_id: int) -> bool:
    """Download a candidate run once and require complete build-manifest coverage."""

    with tempfile.TemporaryDirectory(prefix="xgc2-ci-artifacts-") as directory:
        result = run(
            [
                "gh",
                "run",
                "download",
                str(run_id),
                "--repo",
                str(product["repository"]),
                "--dir",
                directory,
            ],
            check=False,
        )
        if result.returncode != 0:
            details = command_details(result)
            if "no valid artifacts" in details.lower() or "not found" in details.lower():
                return False
            error = TransientReleaseError if is_transient_message(details) else ReleaseError
            raise error(f"{product['id']}: cannot download CI run {run_id} artifacts: {details}")

        expected_product = str(product["id"])
        expected_source = str(product.get("expected_source_sha", ""))
        expected_version_value = str(product.get("expected_version") or product.get("version", ""))
        distributions = {str(item) for item in product.get("apt_distributions", [])}
        arches = set(DEFAULT_ARCHES)
        coverage: set[tuple[str, str]] = set()
        root = Path(directory)
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
            return False
        return True


def find_trusted_ci_run(
    product: dict[str, Any],
    *,
    wait_seconds: int,
    poll_seconds: int,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
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
                    and trusted_ci_artifacts_match(product, run_id)
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
                raise TransientReleaseError(message)
            raise ReleaseError(message)
        time.sleep(poll_seconds)
    raise ReleaseError(
        f"{product['id']}: workflow run {run_id} timed out; refusing to dispatch a duplicate"
    )


def apt_stanzas(base_url: str, distribution: str, arch: str) -> list[dict[str, str]]:
    url = f"{base_url.rstrip('/')}/dists/{distribution}/main/binary-{arch}/Packages"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in {429, 502, 503, 504}:
            raise TransientReleaseError(f"transient APT index response for {url}: {exc}") from exc
        raise ReleaseError(f"APT index request failed for {url}: {exc}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
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
        if exc.code in {429, 502, 503, 504}:
            raise TransientReleaseError(f"transient manifest response for {url}: {exc}") from exc
        raise ReleaseError(f"cannot read release manifest {url}: {exc}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
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
) -> bool:
    stanza = next(
        (
            item
            for item in apt_stanzas(apt_base_url, distribution, arch)
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
    return all(
        package_release_visible(
            product,
            apt_base_url=apt_base_url,
            manifest_base_url=manifest_base_url,
            distribution=distribution,
            arch=arch,
            package=str(package),
            version=version,
            require_current_lock=True,
            strict_manifest_mismatch=False,
        )
        for distribution, version in sorted(versions.items())
        for arch in arches
        for package in packages
    )


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
                pending.add((str(distribution), arch, str(package), version))

    deadline = time.time() + timeout_seconds
    last_transient = ""
    while pending and time.time() < deadline:
        for item in list(pending):
            distribution, arch, package, version = item
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
        "publish_seconds": 0.0,
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
        metric["publish_seconds"] = metric["apt_visibility_seconds"]
        emit_result(metric)
        return 0

    verify_release_lock_is_current(product)
    checkpoint = load_publish_checkpoint(plan_path, product)
    if checkpoint is not None:
        print(
            f"{product['id']}: resuming APT visibility verification after release run "
            f"{checkpoint['release_run_id']}"
        )
        metric["release_run_id"] = checkpoint["release_run_id"]
        metric["resumed_publish_verification"] = True
        verify_started = time.monotonic()
        if not args.skip_apt_verify:
            run_number = checkpoint.get("release_run_number")
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
        metric["publish_seconds"] = metric["apt_visibility_seconds"]
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

    workflow_started = time.monotonic()
    run_id = trigger(
        product,
        quality_required=args.quality_required,
        source_tests=args.source_tests,
        trusted_ci_run_id=trusted_ci_run_id,
    )
    metric["release_run_id"] = run_id
    print(f"{product['id']}: triggered run {run_id}")
    data = wait_for_run(
        product,
        run_id,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
        quality_required=args.quality_required,
    )
    metric["release_workflow_seconds"] = round(time.monotonic() - workflow_started, 3)
    release_run_number = data.get("number")
    write_publish_checkpoint(
        plan_path,
        product,
        release_run_id=run_id,
        release_run_number=release_run_number if isinstance(release_run_number, int) else None,
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
    metric["publish_seconds"] = metric["apt_visibility_seconds"]
    emit_result(metric)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--quality-required", action="store_true")
    parser.add_argument("--source-tests", action="store_true")
    parser.add_argument("--reuse-ci-artifacts", action="store_true")
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
        return execute(args)
    except TransientReleaseError as exc:
        print(f"TRANSIENT: {exc}", file=sys.stderr)
        return TRANSIENT_EXIT_CODE
    except (ReleaseError, KeyError, ValueError, OSError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
