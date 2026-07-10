#!/usr/bin/env python3
"""Execute a release plan as a dependency-aware, resumable work queue."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


TERMINAL = {"success", "failed", "blocked"}
TRANSIENT_EXIT_CODE = 75
TRANSIENT_RETRY_DELAYS = (15.0, 30.0, 60.0)
RESULT_MARKER = "XGC2_RESULT="


def canonical_digest(value: Any) -> str:
    """Return a stable SHA-256 digest for a JSON-compatible value."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan_items(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for layer in plan.get("layers", []):
        for item in layer:
            product_id = str(item["id"])
            if product_id in items:
                raise ValueError(f"duplicate product in release plan: {product_id}")
            items[product_id] = item
    return items


def validate_dependencies(items: dict[str, dict[str, Any]]) -> None:
    for product_id, item in items.items():
        for dependency in item.get("dependencies", []):
            if dependency not in items:
                raise ValueError(f"{product_id}: unknown dependency {dependency}")


def initial_state(
    items: dict[str, dict[str, Any]],
    previous: dict[str, Any] | None,
    *,
    plan_digest: str,
    release_lock_digest: str,
    now_fn: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Create state and re-queue prior successes for strict release verification.

    A matching plan/lock proves that the resume evidence belongs to this release,
    but it does not prove that the corresponding APT indexes and manifests are
    still visible.  Prior successes therefore have to pass the product runner's
    verify-only path before they may satisfy a dependency in this invocation.
    """

    now = int(now_fn())
    state: dict[str, Any] = {
        "schema": "xgc2.release-state.v1",
        "plan_digest": plan_digest,
        "release_lock_digest": release_lock_digest,
        "started_at": now,
        "products": {
            product_id: {
                "status": "pending",
                "queued_at": now,
                "attempts": 0,
                "transient_retries": 0,
            }
            for product_id in items
        },
    }
    if not previous:
        return state
    if previous.get("schema") != state["schema"]:
        raise ValueError("resume state schema does not match xgc2.release-state.v1")
    if previous.get("plan_digest") != plan_digest:
        raise ValueError("resume state belongs to a different release plan")
    if previous.get("release_lock_digest") != release_lock_digest:
        raise ValueError("resume state belongs to a different release lock")
    previous_products = previous.get("products")
    if not isinstance(previous_products, dict):
        raise ValueError("resume state products must be an object")
    for product_id, data in previous_products.items():
        if product_id in items and isinstance(data, dict) and data.get("status") == "success":
            resumed = state["products"][product_id]
            resumed.update(
                {
                    "resumed": True,
                    "resume_verification_required": True,
                    "previous_release_run_id": data.get("release_run_id"),
                }
            )
    return state


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_product(
    runner: Path,
    plan_path: Path,
    product_id: str,
    *,
    quality_required: bool,
    source_tests: bool,
    reuse_ci_artifacts: bool,
    resume_verify: bool = False,
) -> dict[str, Any]:
    command = [sys.executable, str(runner), "--plan", str(plan_path), "--product", product_id]
    if quality_required:
        command.append("--quality-required")
    if source_tests:
        command.append("--source-tests")
    if reuse_ci_artifacts:
        command.append("--reuse-ci-artifacts")
    if resume_verify:
        command.append("--verify-existing-release")
    started = time.monotonic()
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return {
        "status": (
            "success"
            if result.returncode == 0
            else "transient"
            if result.returncode == TRANSIENT_EXIT_CODE
            else "failed"
        ),
        "returncode": result.returncode,
        "output": result.stdout,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def normalize_result(value: Any) -> dict[str, Any]:
    """Normalize worker results while retaining compatibility with older tests."""

    if isinstance(value, tuple) and len(value) == 2:
        status, output = value
        return {
            "status": str(status),
            "returncode": 0 if status == "success" else 1,
            "output": str(output),
            "duration_seconds": 0.0,
        }
    if not isinstance(value, dict):
        raise TypeError(f"release runner returned unsupported result: {type(value).__name__}")
    result = dict(value)
    returncode = int(result.get("returncode", 0 if result.get("status") == "success" else 1))
    status = str(result.get("status", ""))
    if returncode == TRANSIENT_EXIT_CODE:
        status = "transient"
    if status not in {"success", "transient", "failed"}:
        status = "success" if returncode == 0 else "failed"
    result.update(
        {
            "status": status,
            "returncode": returncode,
            "output": str(result.get("output", "")),
            "duration_seconds": float(result.get("duration_seconds", 0.0)),
        }
    )
    return result


def result_metadata(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        if not line.startswith(RESULT_MARKER):
            continue
        try:
            data = json.loads(line[len(RESULT_MARKER) :])
        except json.JSONDecodeError:
            return {"result_marker_error": "invalid JSON"}
        if isinstance(data, dict):
            return data
    return {}


def schedule(
    plan: dict[str, Any],
    *,
    plan_path: Path,
    state_path: Path,
    runner: Path,
    max_parallel: int,
    quality_required: bool,
    source_tests: bool,
    release_lock_digest: str,
    reuse_ci_artifacts: bool = True,
    previous: dict[str, Any] | None = None,
    retry_delays: tuple[float, ...] = TRANSIENT_RETRY_DELAYS,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if not 1 <= max_parallel <= 4:
        raise ValueError("max_parallel must be between 1 and 4")
    if not release_lock_digest:
        raise ValueError("release lock digest is required")
    items = plan_items(plan)
    validate_dependencies(items)
    state = initial_state(
        items,
        previous,
        plan_digest=canonical_digest(plan),
        release_lock_digest=release_lock_digest,
        now_fn=now_fn,
    )
    write_state(state_path, state)
    running: dict[concurrent.futures.Future[dict[str, Any]], str] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as executor:
        while True:
            now = now_fn()
            statuses = {key: value["status"] for key, value in state["products"].items()}
            for product_id, item in items.items():
                if statuses[product_id] not in {"pending", "retry_wait"}:
                    continue
                failed_upstream = [
                    dependency
                    for dependency in item.get("dependencies", [])
                    if statuses[dependency] in {"failed", "blocked"}
                ]
                if failed_upstream:
                    product_state = state["products"][product_id]
                    product_state.update(
                        {
                            "status": "blocked",
                            "reason": "upstream release failed",
                            "blocked_by": sorted(failed_upstream),
                            "completed_at": int(now),
                            "wait_seconds": round(now - product_state["queued_at"], 3),
                        }
                    )

            statuses = {key: value["status"] for key, value in state["products"].items()}
            ready = [
                product_id
                for product_id, item in sorted(items.items())
                if statuses[product_id] in {"pending", "retry_wait"}
                and float(state["products"][product_id].get("next_retry_at", 0)) <= now
                and all(statuses[dep] == "success" for dep in item.get("dependencies", []))
            ]
            while ready and len(running) < max_parallel:
                product_id = ready.pop(0)
                product_state = state["products"][product_id]
                product_state["status"] = "running"
                product_state["started_at"] = int(now_fn())
                product_state.setdefault("first_started_at", product_state["started_at"])
                product_state["attempts"] = int(product_state.get("attempts", 0)) + 1
                product_state.pop("next_retry_at", None)
                future = executor.submit(
                    run_product,
                    runner,
                    plan_path,
                    product_id,
                    quality_required=quality_required,
                    source_tests=source_tests,
                    reuse_ci_artifacts=reuse_ci_artifacts,
                    resume_verify=bool(product_state.get("resume_verification_required")),
                )
                running[future] = product_id
                print(
                    f"scheduler: started {product_id} attempt {product_state['attempts']}",
                    flush=True,
                )
            write_state(state_path, state)

            if not running:
                if all(data["status"] in TERMINAL for data in state["products"].values()):
                    break
                retry_times = [
                    float(data["next_retry_at"])
                    for data in state["products"].values()
                    if data["status"] == "retry_wait" and "next_retry_at" in data
                ]
                if retry_times:
                    delay = max(0.0, min(retry_times) - now_fn())
                    if delay:
                        sleep_fn(delay)
                    continue
                pending = [
                    key
                    for key, data in state["products"].items()
                    if data["status"] not in TERMINAL
                ]
                raise RuntimeError(f"scheduler deadlock among: {', '.join(sorted(pending))}")

            retry_times = [
                float(data["next_retry_at"])
                for data in state["products"].values()
                if data["status"] == "retry_wait" and "next_retry_at" in data
            ]
            retry_timeout = (
                max(0.0, min(retry_times) - now_fn()) if retry_times else None
            )
            done, _ = concurrent.futures.wait(
                running,
                timeout=retry_timeout,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                product_id = running.pop(future)
                result = normalize_result(future.result())
                output = result["output"]
                print(output, end="" if output.endswith("\n") else "\n", flush=True)
                product_state = state["products"][product_id]
                completed_at = now_fn()
                attempt = {
                    "attempt": product_state["attempts"],
                    "started_at": product_state["started_at"],
                    "completed_at": int(completed_at),
                    "duration_seconds": round(result["duration_seconds"], 3),
                    "returncode": result["returncode"],
                    "status": result["status"],
                    "output_tail": output[-4000:],
                }
                product_state.setdefault("attempt_history", []).append(attempt)
                product_state["runner_seconds"] = round(
                    sum(float(item["duration_seconds"]) for item in product_state["attempt_history"]),
                    3,
                )
                metadata = result_metadata(output)
                if metadata:
                    attempt["result"] = metadata
                additive_metrics = {
                    "ci_artifact_wait_seconds",
                    "release_workflow_seconds",
                    "apt_visibility_seconds",
                    "reuse_seconds",
                    "build_seconds",
                    "publish_seconds",
                }
                for key, value in metadata.items():
                    if key in additive_metrics and isinstance(value, (int, float)):
                        product_state[key] = round(
                            float(product_state.get(key, 0.0)) + float(value), 3
                        )
                    else:
                        product_state[key] = value

                if result["status"] == "transient" and int(
                    product_state.get("transient_retries", 0)
                ) < len(retry_delays):
                    retry_index = int(product_state.get("transient_retries", 0))
                    delay = float(retry_delays[retry_index])
                    product_state.update(
                        {
                            "status": "retry_wait",
                            "transient_retries": retry_index + 1,
                            "next_retry_at": completed_at + delay,
                            "last_error": output[-4000:],
                        }
                    )
                    print(
                        f"scheduler: {product_id} transient failure; retry "
                        f"{retry_index + 1}/{len(retry_delays)} in {delay:g}s",
                        flush=True,
                    )
                    continue

                terminal_status = "success" if result["status"] == "success" else "failed"
                product_state.update(
                    {
                        "status": terminal_status,
                        "completed_at": int(completed_at),
                        "wait_seconds": round(
                            float(product_state["first_started_at"])
                            - float(product_state["queued_at"]),
                            3,
                        ),
                        "output_tail": output[-4000:],
                        "returncode": result["returncode"],
                    }
                )
                if result["status"] == "transient":
                    product_state["reason"] = "transient retries exhausted"
                print(f"scheduler: {product_id} -> {terminal_status}", flush=True)

    state["completed_at"] = int(now_fn())
    write_state(state_path, state)
    return state


def append_summary(state: dict[str, Any], path: Path) -> None:
    lines = [
        "## Release scheduler metrics",
        "",
        "| Product | Status | Wait (s) | Runner (s) | CI reuse (s) | "
        "Build/workflow (s) | Publish (s) | APT visibility (s) | Retries |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for product_id, data in sorted(state["products"].items()):
        publish = data.get("publish_seconds")
        publish_display = publish if isinstance(publish, (int, float)) else "n/a"
        lines.append(
            (
                "| {product} | {status} | {wait} | {runner} | {reuse} | {build} | "
                "{publish} | {visibility} | {retries} |"
            ).format(
                product=product_id,
                status=data.get("status", ""),
                wait=data.get("wait_seconds", 0),
                runner=data.get("runner_seconds", 0),
                reuse=data.get("reuse_seconds", data.get("ci_artifact_wait_seconds", 0)),
                build=data.get("build_seconds", 0),
                publish=publish_display,
                visibility=data.get("apt_visibility_seconds", 0),
                retries=data.get("transient_retries", 0),
            )
        )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--state", default=".work/release-state.json")
    parser.add_argument("--resume-state")
    parser.add_argument("--max-parallel", type=int, default=4)
    parser.add_argument("--quality-required", action="store_true")
    parser.add_argument("--source-tests", action="store_true")
    parser.add_argument("--reuse-ci-artifacts", action="store_true")
    args = parser.parse_args()
    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required")
    plan_path = Path(args.plan).resolve()
    lock_path = Path(args.lock).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    release_lock_digest = file_digest(lock_path)
    expected_lock_digest = os.environ.get("XGC2_RELEASE_LOCK_DIGEST", "")
    if expected_lock_digest and expected_lock_digest != release_lock_digest:
        raise SystemExit("release lock file digest does not match XGC2_RELEASE_LOCK_DIGEST")
    previous = None
    if args.resume_state:
        previous = json.loads(Path(args.resume_state).read_text(encoding="utf-8"))
    state = schedule(
        plan,
        plan_path=plan_path,
        state_path=Path(args.state).resolve(),
        runner=Path(__file__).with_name("run-release-plan-product.py"),
        max_parallel=args.max_parallel,
        quality_required=args.quality_required,
        source_tests=args.source_tests,
        release_lock_digest=release_lock_digest,
        reuse_ci_artifacts=args.reuse_ci_artifacts,
        previous=previous,
    )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        append_summary(state, Path(summary))
    failures = {
        key: value for key, value in state["products"].items() if value["status"] != "success"
    }
    if failures:
        print(json.dumps(failures, indent=2, sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
