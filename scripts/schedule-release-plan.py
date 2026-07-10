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
import datetime as dt
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


def validate_plan_lock_equivalence(plan: dict[str, Any], lock: dict[str, Any]) -> None:
    """Require the immutable lock to be an exact flattened view of the plan."""

    if lock.get("schema") != "xgc2.release-lock.v2":
        raise ValueError("release lock schema must be xgc2.release-lock.v2")
    lock_products = lock.get("products")
    expected = [item for layer in plan.get("layers", []) for item in layer]
    if not isinstance(lock_products, list) or lock_products != expected:
        raise ValueError("release plan and release lock product contents are not equivalent")


def execution_policy_value(
    *, quality_required: bool, source_tests: bool, reuse_ci_artifacts: bool
) -> dict[str, bool]:
    return {
        "quality_required": bool(quality_required),
        "source_tests": bool(source_tests),
        "reuse_ci_artifacts": bool(reuse_ci_artifacts),
    }


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
    release_id: str = "test-release",
    execution_policy_digest: str = "",
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
        "schema": "xgc2.release-state.v2",
        "release_id": release_id,
        "plan_digest": plan_digest,
        "release_lock_digest": release_lock_digest,
        "execution_policy_digest": execution_policy_digest,
        "started_at": now,
        "created_at": os.environ.get("XGC2_RELEASE_CREATED_AT")
        or dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "phase": "preparing",
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
        raise ValueError("resume state schema does not match xgc2.release-state.v2")
    if previous.get("release_id") != release_id:
        raise ValueError("resume state belongs to a different release id")
    if previous.get("plan_digest") != plan_digest:
        raise ValueError("resume state belongs to a different release plan")
    if previous.get("release_lock_digest") != release_lock_digest:
        raise ValueError("resume state belongs to a different release lock")
    if previous.get("execution_policy_digest") != execution_policy_digest:
        raise ValueError("resume state belongs to a different execution policy")
    previous_products = previous.get("products")
    if not isinstance(previous_products, dict):
        raise ValueError("resume state products must be an object")
    state["started_at"] = previous.get("started_at", now)
    state["created_at"] = previous.get("created_at", state["created_at"])
    state["resume_phase"] = previous.get("phase")
    if previous.get("train_digest"):
        state["train_digest"] = previous["train_digest"]
    if previous.get("promoted_at"):
        state["promoted_at"] = previous["promoted_at"]
        state["resume_was_promoted"] = True
    for product_id, data in previous_products.items():
        if product_id in items and isinstance(data, dict) and data.get("status") == "success":
            resumed = state["products"][product_id]
            resumed.update(
                {
                    "resumed": True,
                    "resume_verification_required": previous.get("phase")
                    in {"promoted", "verifying", "succeeded"},
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
    central_prepare: bool = True,
    verify_production: bool = False,
    verify_lock_only: bool = False,
    reconcile_ci: bool = False,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    command = [sys.executable, str(runner), "--plan", str(plan_path), "--product", product_id]
    if quality_required:
        command.append("--quality-required")
    if source_tests:
        command.append("--source-tests")
    if reuse_ci_artifacts:
        command.append("--reuse-ci-artifacts")
    if verify_lock_only:
        command.append("--verify-lock-only")
    if reconcile_ci:
        command.append("--reconcile-ci")
    if timeout_seconds is not None:
        command.extend(["--timeout-seconds", str(timeout_seconds)])
    if central_prepare:
        command.append("--central-prepare")
    if verify_production or (central_prepare and resume_verify):
        command.append("--verify-production")
    elif resume_verify:
        command.append("--verify-existing-release")
    apt_base_url = os.environ.get("XGC2_APT_BASE_URL", "")
    manifest_base_url = os.environ.get("XGC2_MANIFEST_BASE_URL", "")
    if apt_base_url:
        command.extend(["--apt-base-url", apt_base_url])
    if manifest_base_url:
        command.extend(["--manifest-base-url", manifest_base_url])
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


def last_json_object(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
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
    release_id: str = "test-release",
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
    if not release_id:
        raise ValueError("release id is required")
    items = plan_items(plan)
    validate_dependencies(items)
    policy = execution_policy_value(
        quality_required=quality_required,
        source_tests=source_tests,
        reuse_ci_artifacts=reuse_ci_artifacts,
    )
    policy_digest = canonical_digest(policy)
    os.environ["XGC2_EXECUTION_POLICY_DIGEST"] = policy_digest
    state = initial_state(
        items,
        previous,
        plan_digest=canonical_digest(plan),
        release_lock_digest=release_lock_digest,
        release_id=release_id,
        execution_policy_digest=policy_digest,
        now_fn=now_fn,
    )
    state["execution_policy"] = policy
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
                    "stage_seconds",
                    "stage_queue_seconds",
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
    state["phase"] = (
        "prepared"
        if all(data["status"] == "success" for data in state["products"].values())
        else "failed"
    )
    write_state(state_path, state)
    return state


def run_global_command(
    command: list[str],
    *,
    retry_delays: tuple[float, ...] = TRANSIENT_RETRY_DELAYS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> subprocess.CompletedProcess[str]:
    """Retry only an explicitly transient (exit 75) train-level operation."""

    attempts = 0
    while True:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
        if result.returncode == 0:
            return result
        if result.returncode != TRANSIENT_EXIT_CODE or attempts >= len(retry_delays):
            raise RuntimeError(
                f"release train command failed with exit {result.returncode}: {result.stdout[-4000:]}"
            )
        delay = float(retry_delays[attempts])
        attempts += 1
        print(
            f"release train transient failure; retry {attempts}/{len(retry_delays)} "
            f"in {delay:g}s",
            flush=True,
        )
        sleep_fn(delay)


def verify_promoted_product(
    runner: Path,
    plan_path: Path,
    product_id: str,
    *,
    quality_required: bool,
    source_tests: bool,
    retry_delays: tuple[float, ...] = TRANSIENT_RETRY_DELAYS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    retries = 0
    while True:
        result = normalize_result(
            run_product(
                runner,
                plan_path,
                product_id,
                quality_required=quality_required,
                source_tests=source_tests,
                reuse_ci_artifacts=False,
                central_prepare=True,
                verify_production=True,
            )
        )
        if result["status"] != "transient" or retries >= len(retry_delays):
            result["transient_retries"] = retries
            return result
        delay = float(retry_delays[retries])
        retries += 1
        sleep_fn(delay)


def verify_plan_freshness(
    plan: dict[str, Any],
    *,
    runner: Path,
    plan_path: Path,
    max_parallel: int,
    retry_delays: tuple[float, ...] = TRANSIENT_RETRY_DELAYS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Recheck every locked source immediately before the atomic promotion."""

    def verify_one(product_id: str) -> dict[str, Any]:
        retries = 0
        while True:
            result = normalize_result(
                run_product(
                    runner,
                    plan_path,
                    product_id,
                    quality_required=False,
                    source_tests=False,
                    reuse_ci_artifacts=False,
                    verify_lock_only=True,
                )
            )
            if result["status"] != "transient" or retries >= len(retry_delays):
                return result
            sleep_fn(float(retry_delays[retries]))
            retries += 1

    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as executor:
        futures = {
            executor.submit(verify_one, product_id): product_id
            for product_id in sorted(plan_items(plan))
        }
        for future in concurrent.futures.as_completed(futures):
            product_id = futures[future]
            result = normalize_result(future.result())
            if result["status"] != "success":
                failures.append(
                    f"{product_id} (exit {result['returncode']}): {result['output'][-1000:]}"
                )
    if failures:
        raise RuntimeError(
            "release plan became stale before promotion:\n" + "\n".join(sorted(failures))
        )


def reconcile_plan_ci(
    plan: dict[str, Any],
    state: dict[str, Any],
    *,
    runner: Path,
    plan_path: Path,
    state_path: Path,
    max_parallel: int,
    timeout_seconds: int = 21600,
    retry_delays: tuple[float, ...] = TRANSIENT_RETRY_DELAYS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Require exact-source CI success after new production dependencies exist."""

    def reconcile_one(product_id: str) -> dict[str, Any]:
        retries = 0
        while True:
            result = normalize_result(
                run_product(
                    runner,
                    plan_path,
                    product_id,
                    quality_required=True,
                    source_tests=False,
                    reuse_ci_artifacts=False,
                    reconcile_ci=True,
                    timeout_seconds=timeout_seconds,
                )
            )
            if result["status"] != "transient" or retries >= len(retry_delays):
                result["transient_retries"] = retries
                return result
            sleep_fn(float(retry_delays[retries]))
            retries += 1

    state["phase"] = "reconciling-ci"
    for product_id in plan_items(plan):
        state["products"][product_id]["ci_reconciliation_status"] = "running"
    write_state(state_path, state)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as executor:
        futures = {
            executor.submit(reconcile_one, product_id): product_id
            for product_id in sorted(plan_items(plan))
        }
        for future in concurrent.futures.as_completed(futures):
            product_id = futures[future]
            result = future.result()
            output = str(result.get("output", ""))
            if output:
                print(output, end="" if output.endswith("\n") else "\n", flush=True)
            node = state["products"][product_id]
            metadata = result_metadata(output)
            node["ci_reconciliation_status"] = (
                "success" if result["status"] == "success" else "failed"
            )
            node["ci_reconciliation_retries"] = result.get("transient_retries", 0)
            node.update(metadata)
            if result["status"] != "success":
                node["status"] = "failed"
                node["reason"] = "post-promotion current-source CI reconciliation failed"
                node["output_tail"] = output[-4000:]
            write_state(state_path, state)


def finalize_release_train(
    plan: dict[str, Any],
    state: dict[str, Any],
    *,
    plan_path: Path,
    state_path: Path,
    runner: Path,
    release_lock_digest: str,
    release_id: str,
    max_parallel: int,
    quality_required: bool,
    source_tests: bool,
    retry_delays: tuple[float, ...] = TRANSIENT_RETRY_DELAYS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Promote all prepared products once, then verify production visibility."""

    if state.get("phase") != "prepared":
        raise ValueError("release train can only promote a fully prepared plan")
    already_promoted = bool(state.get("resume_was_promoted")) or state.get("resume_phase") in {
        "promoted", "verifying", "succeeded"
    }
    if not already_promoted:
        root = plan_path.parent
        train_path = root / "release-train.json"
        stage_tool = Path(__file__).with_name("stage-product-release.py")
        publisher = Path(__file__).with_name("release-train-publisher.py")
        plan_digest = canonical_digest(plan)
        state["phase"] = "freshness-check"
        write_state(state_path, state)
        verify_plan_freshness(
            plan,
            runner=runner,
            plan_path=plan_path,
            max_parallel=max_parallel,
            retry_delays=retry_delays,
            sleep_fn=sleep_fn,
        )
        run_global_command(
            [
                sys.executable,
                str(stage_tool),
                "train",
                "--plan",
                str(plan_path),
                "--receipt-dir",
                str(root / "release-stage-receipts"),
                "--release-id",
                release_id,
                "--release-lock-digest",
                release_lock_digest,
                "--plan-digest",
                plan_digest,
                "--output",
                str(train_path),
            ],
            retry_delays=(),
            sleep_fn=sleep_fn,
        )
        train = json.loads(train_path.read_text(encoding="utf-8"))
        state["train_digest"] = canonical_digest(train)
        state["phase"] = "promoting"
        write_state(state_path, state)
        promotion_started = time.monotonic()
        promotion_result = run_global_command(
            [
                sys.executable,
                str(publisher),
                "promote",
                "--release-id",
                release_id,
                "--release-lock-digest",
                release_lock_digest,
                "--train",
                str(train_path),
            ],
            retry_delays=retry_delays,
            sleep_fn=sleep_fn,
        )
        state["promotion_seconds"] = round(time.monotonic() - promotion_started, 3)
        promotion_receipt = last_json_object(promotion_result.stdout)
        server_promoted_at = promotion_receipt.get("promoted_at")
        if not server_promoted_at:
            raise RuntimeError("promotion succeeded without a server promoted_at receipt")
        state["promoted_at"] = str(server_promoted_at)
    state["phase"] = "promoted"
    write_state(state_path, state)

    release_ids = sorted(
        product_id
        for product_id, item in plan_items(plan).items()
        if item.get("action") == "release"
    )
    verify_ids = [
        product_id
        for product_id in release_ids
        if state["products"][product_id].get("prepare_checkpoint") != "production_verified"
    ]
    state["phase"] = "verifying"
    for product_id in verify_ids:
        state["products"][product_id]["status"] = "verifying"
    write_state(state_path, state)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as executor:
        futures = {
            executor.submit(
                verify_promoted_product,
                runner,
                plan_path,
                product_id,
                quality_required=quality_required,
                source_tests=source_tests,
                retry_delays=retry_delays,
                sleep_fn=sleep_fn,
            ): product_id
            for product_id in verify_ids
        }
        for future in concurrent.futures.as_completed(futures):
            product_id = futures[future]
            result = future.result()
            output = str(result.get("output", ""))
            if output:
                print(output, end="" if output.endswith("\n") else "\n", flush=True)
            node = state["products"][product_id]
            metadata = result_metadata(output)
            node.update(metadata)
            node["production_verify_retries"] = result.get("transient_retries", 0)
            node["status"] = "success" if result["status"] == "success" else "failed"
            if result["status"] != "success":
                node["reason"] = "production verification failed"
                node["output_tail"] = output[-4000:]
            write_state(state_path, state)
    if all(data["status"] == "success" for data in state["products"].values()):
        reconcile_plan_ci(
            plan,
            state,
            runner=runner,
            plan_path=plan_path,
            state_path=state_path,
            max_parallel=max_parallel,
            retry_delays=retry_delays,
            sleep_fn=sleep_fn,
        )
    state["phase"] = (
        "succeeded"
        if all(data["status"] == "success" for data in state["products"].values())
        else "failed"
    )
    state["completed_at"] = int(time.time())
    write_state(state_path, state)
    return state


def append_summary(state: dict[str, Any], path: Path) -> None:
    lines = [
        "## Release scheduler metrics",
        "",
        "| Product | Status | Wait (s) | Runner (s) | CI reuse (s) | "
        "Build/workflow (s) | Stage queue (s) | Stage (s) | Publish (s) | "
        "APT visibility (s) | Retries |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for product_id, data in sorted(state["products"].items()):
        publish = data.get("publish_seconds")
        publish_display = publish if isinstance(publish, (int, float)) else "n/a"
        lines.append(
            (
                "| {product} | {status} | {wait} | {runner} | {reuse} | {build} | "
                "{stage_queue} | {stage} | {publish} | {visibility} | {retries} |"
            ).format(
                product=product_id,
                status=data.get("status", ""),
                wait=data.get("wait_seconds", 0),
                runner=data.get("runner_seconds", 0),
                reuse=data.get("reuse_seconds", data.get("ci_artifact_wait_seconds", 0)),
                build=data.get("build_seconds", 0),
                stage_queue=data.get("stage_queue_seconds", 0),
                stage=data.get("stage_seconds", 0),
                publish=publish_display,
                visibility=data.get("apt_visibility_seconds", 0),
                retries=data.get("transient_retries", 0),
            )
        )
    with path.open("a", encoding="utf-8") as handle:
        if isinstance(state.get("promotion_seconds"), (int, float)):
            lines.extend(["", f"Global promotion: {state['promotion_seconds']} seconds."])
        handle.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--state", default=".work/release-state.json")
    parser.add_argument("--resume-state")
    parser.add_argument("--release-id", default=os.environ.get("XGC2_RELEASE_ID", ""))
    parser.add_argument("--max-parallel", type=int, default=4)
    parser.add_argument("--quality-required", action="store_true")
    parser.add_argument("--source-tests", action="store_true")
    parser.add_argument("--reuse-ci-artifacts", action="store_true")
    args = parser.parse_args()
    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required")
    if not args.release_id:
        raise SystemExit("--release-id or XGC2_RELEASE_ID is required")
    plan_path = Path(args.plan).resolve()
    lock_path = Path(args.lock).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    validate_plan_lock_equivalence(plan, lock)
    release_lock_digest = file_digest(lock_path)
    expected_lock_digest = os.environ.get("XGC2_RELEASE_LOCK_DIGEST", "")
    if expected_lock_digest and expected_lock_digest != release_lock_digest:
        raise SystemExit("release lock file digest does not match XGC2_RELEASE_LOCK_DIGEST")
    previous = None
    if args.resume_state:
        previous = json.loads(Path(args.resume_state).read_text(encoding="utf-8"))
        if previous.get("release_id") != args.release_id:
            raise SystemExit("resume release id does not match --release-id")
    os.environ["XGC2_RELEASE_ID"] = args.release_id
    os.environ["XGC2_RELEASE_CREATED_AT"] = (
        str(previous.get("created_at"))
        if previous and previous.get("created_at")
        else dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    )
    state = schedule(
        plan,
        plan_path=plan_path,
        state_path=Path(args.state).resolve(),
        runner=Path(__file__).with_name("run-release-plan-product.py"),
        max_parallel=args.max_parallel,
        quality_required=args.quality_required,
        source_tests=args.source_tests,
        release_lock_digest=release_lock_digest,
        release_id=args.release_id,
        reuse_ci_artifacts=args.reuse_ci_artifacts,
        previous=previous,
    )
    if state.get("phase") == "prepared":
        try:
            state = finalize_release_train(
                plan,
                state,
                plan_path=plan_path,
                state_path=Path(args.state).resolve(),
                runner=Path(__file__).with_name("run-release-plan-product.py"),
                release_lock_digest=release_lock_digest,
                release_id=args.release_id,
                max_parallel=args.max_parallel,
                quality_required=args.quality_required,
                source_tests=args.source_tests,
            )
        except Exception as exc:
            state["phase"] = "failed"
            state["release_train_error"] = str(exc)[-4000:]
            state["completed_at"] = int(time.time())
            write_state(Path(args.state).resolve(), state)
            raise
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        append_summary(state, Path(summary))
    failures = {
        key: value for key, value in state["products"].items() if value["status"] != "success"
    }
    if failures or state.get("phase") != "succeeded":
        print(json.dumps(failures, indent=2, sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
