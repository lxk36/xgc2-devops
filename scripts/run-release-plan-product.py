#!/usr/bin/env python3
"""Trigger and wait for one product workflow from a release plan."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_APT_BASE_URL = "https://xgc2.apt.xiaokang.ink"
DEFAULT_ARCHES = ("amd64", "arm64")


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def find_product(plan: dict[str, Any], product_id: str) -> dict[str, Any]:
    for layer in plan.get("layers", []):
        for item in layer:
            if item.get("id") == product_id:
                return item
    raise KeyError(f"{product_id} is not in release plan")


def trigger(product: dict[str, Any], *, quality_required: bool, source_tests: bool) -> int:
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
        command.extend(["-f", "publish_apt=true"])
    if "run_cpp_quality" in workflow_inputs:
        command.extend(["-f", f"run_cpp_quality={str(quality_required).lower()}"])
    if "run_source_tests" in workflow_inputs:
        command.extend(["-f", f"run_source_tests={str(source_tests).lower()}"])
    for name, value in sorted(product.get("inputs", {}).items()):
        command.extend(["-f", f"{name}={value}"])

    triggered_after = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10))
    run(command, check=True)
    return find_workflow_run(product, triggered_after)


def find_workflow_run(product: dict[str, Any], triggered_after: str) -> int:
    for _ in range(30):
        result = run(
            [
                "gh",
                "run",
                "list",
                "--repo",
                str(product["repository"]),
                "--workflow",
                str(product["workflow"]),
                "--event",
                "workflow_dispatch",
                "--limit",
                "20",
                "--json",
                "databaseId,createdAt,headBranch,status,conclusion,url",
            ],
            check=False,
        )
        if result.returncode == 0:
            runs = json.loads(result.stdout or "[]")
            for item in runs:
                if str(item.get("createdAt", "")) < triggered_after:
                    continue
                if item.get("headBranch") not in (None, "", product["ref"]):
                    continue
                run_id = item.get("databaseId")
                if isinstance(run_id, int):
                    return run_id
        time.sleep(5)
    raise RuntimeError(f"{product['id']}: could not find dispatched workflow run")


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
    if not quality_required and all("quality" in str(job.get("name", "")).lower() for job in failed):
        return True, "only optional quality jobs failed"
    failed_names = ", ".join(str(job.get("name", "unknown")) for job in failed)
    return False, f"failed jobs: {failed_names}"


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
        if result.returncode == 0:
            data = json.loads(result.stdout or "{}")
            if data.get("status") == "completed":
                ok, reason = run_completed_successfully(data, quality_required=quality_required)
                if ok:
                    print(f"{product['id']}: workflow completed ({reason})")
                    return data
                raise RuntimeError(f"{product['id']}: workflow failed ({reason}) {data.get('url')}")
        time.sleep(poll_seconds)
    raise TimeoutError(f"{product['id']}: workflow run {run_id} timed out")


def apt_stanzas(base_url: str, distribution: str, arch: str) -> list[dict[str, str]]:
    url = f"{base_url.rstrip('/')}/dists/{distribution}/main/binary-{arch}/Packages"
    with urllib.request.urlopen(url, timeout=30) as response:
        text = response.read().decode("utf-8", errors="replace")
    stanzas: list[dict[str, str]] = []
    for block in text.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if ":" not in line or line.startswith(" "):
                continue
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
        if fields:
            stanzas.append(fields)
    return stanzas


def expected_version(product: dict[str, Any], distribution: str, run_number: int | None) -> str | None:
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
    return str(product.get("version", "")) or None


def verify_apt(
    product: dict[str, Any],
    *,
    base_url: str,
    arches: tuple[str, ...],
    timeout_seconds: int,
    poll_seconds: int,
    run_number: int | None,
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
            raise RuntimeError(f"{product['id']}: missing expected apt version for {distribution}")
        for arch in arches:
            for package in packages:
                pending.add((str(distribution), arch, str(package), version))

    deadline = time.time() + timeout_seconds
    while pending and time.time() < deadline:
        for item in list(pending):
            distribution, arch, package, version = item
            try:
                if any(
                    stanza.get("Package") == package and stanza.get("Version") == version
                    for stanza in apt_stanzas(base_url, distribution, arch)
                ):
                    pending.remove(item)
            except Exception as exc:  # noqa: BLE001 - retry transient APT index errors.
                print(f"{product['id']}: apt check retry after {type(exc).__name__}: {exc}")
        if pending:
            time.sleep(poll_seconds)

    if pending:
        missing = ", ".join(
            f"{dist}/{arch}:{package}={version}"
            for dist, arch, package, version in sorted(pending)
        )
        raise TimeoutError(f"{product['id']}: expected apt version(s) not visible for {missing}")
    print(f"{product['id']}: apt index contains expected version(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--quality-required", action="store_true")
    parser.add_argument("--source-tests", action="store_true")
    parser.add_argument("--skip-apt-verify", action="store_true")
    parser.add_argument("--apt-base-url", default=DEFAULT_APT_BASE_URL)
    parser.add_argument("--apt-arch", action="append", default=[])
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--apt-timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-seconds", type=int, default=15)
    args = parser.parse_args()

    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required")

    with Path(args.plan).open("r", encoding="utf-8") as handle:
        plan = json.load(handle)
    product = find_product(plan, args.product)
    run_id = trigger(
        product,
        quality_required=args.quality_required,
        source_tests=args.source_tests,
    )
    print(f"{product['id']}: triggered run {run_id}")
    data = wait_for_run(
        product,
        run_id,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
        quality_required=args.quality_required,
    )
    if not args.skip_apt_verify:
        run_number = data.get("number")
        verify_apt(
            product,
            base_url=args.apt_base_url,
            arches=tuple(args.apt_arch or DEFAULT_ARCHES),
            timeout_seconds=args.apt_timeout_seconds,
            poll_seconds=args.poll_seconds,
            run_number=run_number if isinstance(run_number, int) else None,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
