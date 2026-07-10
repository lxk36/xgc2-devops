from __future__ import annotations

import importlib.util
import hashlib
import inspect
import json
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


planner = load_script("orchestrate-apt-release.py")
scheduler = load_script("schedule-release-plan.py")
runner = load_script("run-release-plan-product.py")
manifest_tool = load_script("create-release-manifest.py")
plan_validator = load_script("validate-release-plan.py")
workflow_audit = load_script("audit-product-workflows.py")


class FakeClock:
    def __init__(self, start: float = 1_000.0):
        self.value = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class WorkflowAuditTests(unittest.TestCase):
    def test_release_boolean_input_types_are_explicit(self):
        workflow = """
on:
  workflow_dispatch:
    inputs:
      publish_apt:
        default: false
        type: boolean
      run_cpp_quality:
        default: false
        type: boolean
      run_source_tests:
        default: false
"""
        self.assertEqual(
            {"publish_apt": "boolean", "run_cpp_quality": "boolean"},
            workflow_audit.workflow_input_types(workflow),
        )
        self.assertEqual(
            {"run_source_tests"},
            workflow_audit.non_boolean_optional_release_inputs(workflow),
        )

    def test_release_boolean_input_type_audit_accepts_all_boolean_inputs(self):
        workflow = """
on:
  workflow_dispatch:
    inputs:
      publish_apt:
        default: false
        type: boolean
      run_cpp_quality:
        default: false
        type: boolean
      run_source_tests:
        default: false
        type: boolean
"""
        self.assertEqual(
            set(),
            workflow_audit.non_boolean_optional_release_inputs(workflow),
        )

    def test_host_manifest_directory_accepts_shell_parameter_forms(self):
        for command in (
            'install -d "$GITHUB_WORKSPACE/.ci/build-manifests"',
            'install -d "${GITHUB_WORKSPACE}/.ci/build-manifests"',
            "mkdir -p .ci/build-manifests",
        ):
            with self.subTest(command=command):
                self.assertTrue(workflow_audit.host_manifest_directory_precreated(command))

    def test_host_manifest_directory_rejects_unrelated_directories(self):
        self.assertFalse(
            workflow_audit.host_manifest_directory_precreated(
                'install -d "$GITHUB_WORKSPACE/.ci/debs"'
            )
        )


class DagTests(unittest.TestCase):
    def test_diamond_layers(self):
        downstream = {"a": {"b", "c"}, "b": {"d"}, "c": {"d"}, "d": set()}
        self.assertEqual(planner.topo_layers(set(downstream), downstream), [["a"], ["b", "c"], ["d"]])

    def test_independent_branch_is_not_globally_blocked(self):
        plan = {
            "layers": [[
                {"id": "bad", "dependencies": []},
                {"id": "free", "dependencies": []},
            ], [{"id": "child", "dependencies": ["bad"]}]]
        }
        outcomes = {
            "bad": {
                "status": "failed",
                "returncode": 1,
                "output": "compile failed",
                "duration_seconds": 2.0,
            },
            "free": {
                "status": "success",
                "returncode": 0,
                "output": "ok",
                "duration_seconds": 3.0,
            },
        }
        called: list[str] = []

        def fake_run_product(*args, **kwargs):
            del kwargs
            product_id = args[2]
            called.append(product_id)
            return outcomes[product_id]

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            scheduler, "run_product", side_effect=fake_run_product
        ), mock.patch.object(scheduler.time, "sleep") as sleep:
            state = scheduler.schedule(
                plan,
                plan_path=Path(directory) / "plan.json",
                state_path=Path(directory) / "state.json",
                runner=Path("runner.py"),
                max_parallel=2,
                quality_required=False,
                source_tests=False,
                release_lock_digest="lock-1",
                retry_delays=(0, 0, 0),
            )
        self.assertEqual(state["products"]["bad"]["status"], "failed")
        self.assertEqual(state["products"]["free"]["status"], "success")
        self.assertEqual(state["products"]["child"]["status"], "blocked")
        self.assertCountEqual(called, ["bad", "free"])
        self.assertEqual(state["products"]["bad"]["attempts"], 1)
        self.assertEqual(state["products"]["bad"]["transient_retries"], 0)
        sleep.assert_not_called()

    def test_cycle_is_rejected(self):
        downstream = {"a": {"b"}, "b": {"a"}}
        with self.assertRaisesRegex(ValueError, "cycle"):
            planner.topo_layers(set(downstream), downstream)

    def test_resume_requeues_successful_nodes_for_same_plan_and_lock_verification(self):
        items = {"a": {"id": "a"}, "b": {"id": "b"}}
        previous = {
            "schema": "xgc2.release-state.v1",
            "plan_digest": "plan-1",
            "release_lock_digest": "lock-1",
            "products": {
                "a": {"status": "success", "attempts": 1},
                "b": {"status": "failed", "attempts": 1},
            },
        }
        state = scheduler.initial_state(
            items,
            previous,
            plan_digest="plan-1",
            release_lock_digest="lock-1",
        )
        self.assertEqual(state["products"]["a"]["status"], "pending")
        self.assertTrue(state["products"]["a"]["resume_verification_required"])
        self.assertTrue(state["products"]["a"]["resumed"])
        self.assertEqual(state["products"]["b"]["status"], "pending")

    def test_resume_rejects_different_plan_digest(self):
        items = {"a": {"id": "a"}}
        previous = {
            "schema": "xgc2.release-state.v1",
            "plan_digest": "old-plan",
            "release_lock_digest": "same-lock",
            "products": {"a": {"status": "success"}},
        }
        with self.assertRaisesRegex(ValueError, "release plan"):
            scheduler.initial_state(
                items,
                previous,
                plan_digest="new-plan",
                release_lock_digest="same-lock",
            )

    def test_resume_rejects_different_release_lock_digest(self):
        items = {"a": {"id": "a"}}
        previous = {
            "schema": "xgc2.release-state.v1",
            "plan_digest": "same-plan",
            "release_lock_digest": "old-lock",
            "products": {"a": {"status": "success"}},
        }
        with self.assertRaisesRegex(ValueError, "release lock"):
            scheduler.initial_state(
                items,
                previous,
                plan_digest="same-plan",
                release_lock_digest="new-lock",
            )

    def test_resume_rejects_legacy_state_without_digest_binding(self):
        items = {"a": {"id": "a"}}
        previous = {"products": {"a": {"status": "success"}}}
        with self.assertRaisesRegex(ValueError, "schema|digest"):
            scheduler.initial_state(
                items,
                previous,
                plan_digest="plan-1",
                release_lock_digest="lock-1",
            )

    def test_resumed_success_must_reverify_before_releasing_downstream(self):
        plan = {
            "layers": [
                [{"id": "upstream", "dependencies": []}],
                [{"id": "downstream", "dependencies": ["upstream"]}],
            ]
        }
        previous = {
            "schema": "xgc2.release-state.v1",
            "plan_digest": scheduler.canonical_digest(plan),
            "release_lock_digest": "lock-1",
            "products": {
                "upstream": {"status": "success", "release_run_id": 101},
                "downstream": {"status": "success", "release_run_id": 202},
            },
        }

        def failed_verification(*args, **kwargs):
            self.assertEqual(args[2], "upstream")
            self.assertTrue(kwargs["resume_verify"])
            return {
                "status": "failed",
                "returncode": 1,
                "output": "release manifest identity/hash mismatch",
                "duration_seconds": 0.1,
            }

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            scheduler, "run_product", side_effect=failed_verification
        ) as run_product:
            state = scheduler.schedule(
                plan,
                plan_path=Path(directory) / "plan.json",
                state_path=Path(directory) / "state.json",
                runner=Path("runner.py"),
                max_parallel=2,
                quality_required=False,
                source_tests=False,
                release_lock_digest="lock-1",
                previous=previous,
                retry_delays=(0, 0, 0),
            )

        self.assertEqual(run_product.call_count, 1)
        self.assertEqual(state["products"]["upstream"]["status"], "failed")
        self.assertEqual(state["products"]["downstream"]["status"], "blocked")
        self.assertEqual(state["products"]["downstream"]["blocked_by"], ["upstream"])


class SchedulerResilienceTests(unittest.TestCase):
    def test_canonical_digest_is_key_order_independent(self):
        left = {"schema": "test", "nested": {"b": 2, "a": 1}}
        right = {"nested": {"a": 1, "b": 2}, "schema": "test"}
        self.assertEqual(scheduler.canonical_digest(left), scheduler.canonical_digest(right))
        self.assertNotEqual(
            scheduler.canonical_digest(left),
            scheduler.canonical_digest({"schema": "test", "nested": {"a": 9, "b": 2}}),
        )

    def test_default_retry_schedule_is_15_30_60_seconds(self):
        parameter = inspect.signature(scheduler.schedule).parameters["retry_delays"]
        self.assertEqual(parameter.default, (15, 30, 60))

    def test_more_than_four_parallel_products_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "between 1 and 4"):
                scheduler.schedule(
                    {"layers": []},
                    plan_path=Path(directory) / "plan.json",
                    state_path=Path(directory) / "state.json",
                    runner=Path("runner.py"),
                    max_parallel=5,
                    quality_required=False,
                    source_tests=False,
                    release_lock_digest="lock-1",
                )

    def test_worker_classifies_only_exit_75_as_transient(self):
        expected = {0: "success", 1: "failed", 2: "failed", 75: "transient"}
        for returncode, status in expected.items():
            with self.subTest(returncode=returncode), mock.patch.object(
                scheduler.subprocess,
                "run",
                return_value=mock.Mock(returncode=returncode, stdout="runner output"),
            ):
                result = scheduler.run_product(
                    Path("runner.py"),
                    Path("plan.json"),
                    "product",
                    quality_required=False,
                    source_tests=False,
                    reuse_ci_artifacts=True,
                )
            self.assertEqual(result["status"], status)
            self.assertEqual(result["returncode"], returncode)

    def test_transient_exit_is_retried_three_times_and_metrics_are_recorded(self):
        plan = {"layers": [[{"id": "leaf", "dependencies": []}]]}
        results = deque(
            [
                {
                    "status": "transient",
                    "returncode": 75,
                    "output": "temporary network error",
                    "duration_seconds": 1.0,
                },
                {
                    "status": "transient",
                    "returncode": 75,
                    "output": "APT index is not visible yet",
                    "duration_seconds": 2.0,
                },
                {
                    "status": "transient",
                    "returncode": 75,
                    "output": "publish lock busy",
                    "duration_seconds": 3.0,
                },
                {
                    "status": "success",
                    "returncode": 0,
                    "output": (
                        "published\n"
                        'XGC2_RESULT={"build_seconds": 4.5, "reuse_seconds": 0.0, '
                        '"publish_seconds": 1.25, "reused_ci_artifact": false}'
                    ),
                    "duration_seconds": 4.0,
                },
            ]
        )
        clock = FakeClock()

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            scheduler, "run_product", side_effect=lambda *args, **kwargs: results.popleft()
        ) as run_product:
            state = scheduler.schedule(
                plan,
                plan_path=Path(directory) / "plan.json",
                state_path=Path(directory) / "state.json",
                runner=Path("runner.py"),
                max_parallel=1,
                quality_required=False,
                source_tests=False,
                release_lock_digest="lock-1",
                retry_delays=(15, 30, 60),
                sleep_fn=clock.sleep,
                now_fn=clock.time,
            )

        node = state["products"]["leaf"]
        self.assertEqual(run_product.call_count, 4)
        self.assertEqual(clock.sleeps, [15, 30, 60])
        self.assertEqual(node["status"], "success")
        self.assertEqual(node["attempts"], 4)
        self.assertEqual(node["transient_retries"], 3)
        self.assertEqual(node["runner_seconds"], 10.0)
        self.assertEqual(node["build_seconds"], 4.5)
        self.assertEqual(node["reuse_seconds"], 0.0)
        self.assertEqual(node["publish_seconds"], 1.25)
        self.assertFalse(node["reused_ci_artifact"])
        for field in ("queued_at", "first_started_at", "completed_at", "wait_seconds"):
            self.assertIn(field, node)

    def test_transient_failure_stops_after_three_retries(self):
        plan = {"layers": [[{"id": "leaf", "dependencies": []}]]}
        transient = {
            "status": "transient",
            "returncode": 75,
            "output": "temporary network error",
            "duration_seconds": 0.25,
        }
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            scheduler, "run_product", return_value=transient
        ) as run_product:
            state = scheduler.schedule(
                plan,
                plan_path=Path(directory) / "plan.json",
                state_path=Path(directory) / "state.json",
                runner=Path("runner.py"),
                max_parallel=1,
                quality_required=False,
                source_tests=False,
                release_lock_digest="lock-1",
                retry_delays=(15, 30, 60),
                sleep_fn=clock.sleep,
                now_fn=clock.time,
            )

        node = state["products"]["leaf"]
        self.assertEqual(run_product.call_count, 4)
        self.assertEqual(clock.sleeps, [15, 30, 60])
        self.assertEqual(node["status"], "failed")
        self.assertEqual(node["attempts"], 4)
        self.assertEqual(node["transient_retries"], 3)
        self.assertEqual(node["returncode"], 75)


class TrustedCiSelectionTests(unittest.TestCase):
    @staticmethod
    def product() -> dict[str, object]:
        return {
            "id": "xgc2-test",
            "repository": "xiaokang-robotics/xgc2-test",
            "ci_workflow": "ci.yml",
            "expected_source_sha": "a" * 40,
            "expected_version": "1.2.3-4",
            "apt_distributions": ["focal"],
        }

    @staticmethod
    def gh_result(runs: list[dict[str, object]]):
        return mock.Mock(returncode=0, stdout=json.dumps(runs), stderr="")

    def test_exact_sha_successful_push_with_live_artifact_returns_run_id(self):
        product = self.product()
        runs = [
            {
                "databaseId": 12345,
                "status": "completed",
                "conclusion": "success",
                "headSha": product["expected_source_sha"],
                "event": "push",
                "createdAt": "2026-07-10T01:00:00Z",
                "url": "https://github.com/example/actions/runs/12345",
            }
        ]
        with mock.patch.object(runner, "run", return_value=self.gh_result(runs)) as gh_run:
            with mock.patch.object(
                runner, "active_run_artifact_count", return_value=2
            ) as count:
                with mock.patch.object(
                    runner, "trusted_ci_artifacts_match", return_value=True
                ):
                    run_id = runner.find_trusted_ci_run(
                        product,
                        wait_seconds=0,
                        poll_seconds=1,
                    )

        self.assertEqual(run_id, 12345)
        count.assert_called_once_with(product["repository"], 12345)
        command = gh_run.call_args.args[0]
        self.assertIn("--event", command)
        self.assertEqual(command[command.index("--event") + 1], "push")
        self.assertEqual(
            command[command.index("--commit") + 1], product["expected_source_sha"]
        )
        self.assertEqual(command[command.index("--workflow") + 1], "ci.yml")

    def test_wrong_sha_and_manual_runs_are_ignored(self):
        product = self.product()
        runs = [
            {
                "databaseId": 11,
                "status": "completed",
                "conclusion": "success",
                "headSha": "b" * 40,
                "event": "push",
                "createdAt": "2026-07-10T02:00:00Z",
            },
            {
                "databaseId": 12,
                "status": "completed",
                "conclusion": "success",
                "headSha": product["expected_source_sha"],
                "event": "workflow_dispatch",
                "createdAt": "2026-07-10T03:00:00Z",
            },
        ]
        with mock.patch.object(runner, "run", return_value=self.gh_result(runs)):
            with mock.patch.object(runner, "active_run_artifact_count") as count:
                run_id = runner.find_trusted_ci_run(
                    product,
                    wait_seconds=0,
                    poll_seconds=1,
                )

        self.assertIsNone(run_id)
        count.assert_not_called()
    def test_completed_matching_failed_ci_is_deterministic_failure(self):
        product = self.product()
        runs = [
            {
                "databaseId": 99,
                "status": "completed",
                "conclusion": "failure",
                "headSha": product["expected_source_sha"],
                "event": "push",
                "createdAt": "2026-07-10T03:00:00Z",
                "url": "https://github.com/example/actions/runs/99",
            }
        ]
        with mock.patch.object(runner, "run", return_value=self.gh_result(runs)):
            with mock.patch.object(runner, "active_run_artifact_count") as count:
                with self.assertRaisesRegex(
                    runner.ReleaseError, "refusing to bypass failed CI"
                ):
                    runner.find_trusted_ci_run(
                        product,
                        wait_seconds=0,
                        poll_seconds=1,
                    )

        count.assert_not_called()

    def test_successful_ci_with_expired_artifacts_waits_then_falls_back(self):
        product = self.product()
        runs = [
            {
                "databaseId": 77,
                "status": "completed",
                "conclusion": "success",
                "headSha": product["expected_source_sha"],
                "event": "push",
                "createdAt": "2026-07-10T03:00:00Z",
            }
        ]
        clock = FakeClock()
        with mock.patch.object(runner, "run", return_value=self.gh_result(runs)) as gh_run:
            with mock.patch.object(
                runner, "active_run_artifact_count", return_value=0
            ) as count:
                run_id = runner.find_trusted_ci_run(
                    product,
                    wait_seconds=2,
                    poll_seconds=1,
                    sleep_fn=clock.sleep,
                    now_fn=clock.time,
                )

        self.assertIsNone(run_id)
        self.assertEqual(clock.sleeps, [1.0, 1.0])
        self.assertEqual(gh_run.call_count, 3)
        # Completed artifacts are immutable; reject the run once, then wait
        # only for a newer rerun at the same source SHA.
        self.assertEqual(count.call_count, 1)

    def test_missing_matching_ci_returns_none(self):
        product = self.product()
        with mock.patch.object(runner, "run", return_value=self.gh_result([])):
            with mock.patch.object(runner, "active_run_artifact_count") as count:
                run_id = runner.find_trusted_ci_run(
                    product,
                    wait_seconds=0,
                    poll_seconds=1,
                )

        self.assertIsNone(run_id)
        count.assert_not_called()


class PublishCheckpointTests(unittest.TestCase):
    @staticmethod
    def product() -> dict[str, object]:
        return {
            "id": "xgc2-test",
            "action": "release",
            "repository": "example/xgc2-test",
            "ref": "main",
            "workflow": "release.yml",
            "workflow_inputs": [],
            "expected_source_sha": "a" * 40,
            "expected_version": "1.2.3-4",
            "apt_versions": {"focal": "1.2.3-4~focal"},
            "apt_distributions": ["focal"],
            "apt_packages": ["libxgc2-test"],
        }

    @staticmethod
    def args(plan_path: Path, **overrides):
        values = {
            "plan": str(plan_path),
            "product": "xgc2-test",
            "apt_arch": [],
            "skip_apt_verify": False,
            "no_fast_pass": False,
            "verify_existing_release": False,
            "reuse_ci_artifacts": False,
            "quality_required": False,
            "source_tests": False,
            "apt_base_url": "https://apt.example",
            "manifest_base_url": "https://apt.example/manifests",
            "apt_timeout_seconds": 1,
            "ci_wait_seconds": 0,
            "timeout_seconds": 1,
            "poll_seconds": 0,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def write_plan(self, directory: str) -> Path:
        path = Path(directory) / "release-plan.json"
        path.write_text(json.dumps({"layers": [[self.product()]]}), encoding="utf-8")
        return path

    def test_checkpoint_binds_source_and_lock_for_visibility_only_retry(self):
        product = {
            "id": "xgc2-test",
            "expected_source_sha": "a" * 40,
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            runner.os.environ,
            {"XGC2_RELEASE_LOCK_DIGEST": "b" * 64},
            clear=False,
        ):
            plan_path = Path(directory) / "release-plan.json"
            plan_path.write_text("{}\n", encoding="utf-8")
            runner.write_publish_checkpoint(
                plan_path,
                product,
                release_run_id=12345,
                release_run_number=67,
            )
            checkpoint = runner.load_publish_checkpoint(plan_path, product)
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint["release_run_id"], 12345)
            self.assertEqual(checkpoint["release_run_number"], 67)
            self.assertEqual(checkpoint["phase"], "workflow_succeeded")

            with mock.patch.dict(
                runner.os.environ,
                {"XGC2_RELEASE_LOCK_DIGEST": "c" * 64},
                clear=False,
            ):
                with self.assertRaisesRegex(runner.ReleaseError, "does not match"):
                    runner.load_publish_checkpoint(plan_path, product)

    def test_exact_run_id_is_checkpointed_before_waiting(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            runner.os.environ,
            {"GH_TOKEN": "test", "XGC2_RELEASE_LOCK_DIGEST": "b" * 64},
            clear=False,
        ):
            plan_path = self.write_plan(directory)
            with mock.patch.object(runner, "verify_release_lock_is_current"), mock.patch.object(
                runner, "fast_pass_ready", return_value=False
            ), mock.patch.object(runner, "trigger", return_value=12345), mock.patch.object(
                runner,
                "wait_for_run",
                side_effect=runner.TransientReleaseError("workflow run timed out"),
            ):
                with self.assertRaises(runner.TransientReleaseError):
                    runner.execute(self.args(plan_path))

            checkpoint = runner.load_publish_checkpoint(plan_path, self.product())
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint["phase"], "dispatched")
            self.assertEqual(checkpoint["release_run_id"], 12345)

    def test_resume_waits_checkpointed_run_and_never_dispatches_again(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            runner.os.environ,
            {"GH_TOKEN": "test", "XGC2_RELEASE_LOCK_DIGEST": "b" * 64},
            clear=False,
        ):
            plan_path = self.write_plan(directory)
            runner.write_publish_checkpoint(
                plan_path,
                self.product(),
                release_run_id=12345,
                release_run_number=None,
                phase="dispatched",
                trusted_ci_run_id=678,
                dispatched_at=runner.time.time(),
            )
            completed = {"number": 77, "conclusion": "success", "jobs": []}
            with mock.patch.object(runner, "verify_release_lock_is_current"), mock.patch.object(
                runner, "trigger"
            ) as trigger, mock.patch.object(
                runner, "wait_for_run", return_value=completed
            ) as wait, mock.patch.object(runner, "verify_apt") as verify:
                self.assertEqual(runner.execute(self.args(plan_path)), 0)

            trigger.assert_not_called()
            wait.assert_called_once()
            self.assertEqual(wait.call_args.args[1], 12345)
            verify.assert_called_once()
            checkpoint = runner.load_publish_checkpoint(plan_path, self.product())
            self.assertEqual(checkpoint["phase"], "workflow_succeeded")
            self.assertEqual(checkpoint["release_run_number"], 77)

    def test_completed_transient_run_is_cleared_for_safe_scheduler_redispatch(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            runner.os.environ,
            {"GH_TOKEN": "test", "XGC2_RELEASE_LOCK_DIGEST": "b" * 64},
            clear=False,
        ):
            plan_path = self.write_plan(directory)
            runner.write_publish_checkpoint(
                plan_path,
                self.product(),
                release_run_id=12345,
                release_run_number=None,
                phase="dispatched",
            )
            with mock.patch.object(runner, "verify_release_lock_is_current"), mock.patch.object(
                runner,
                "wait_for_run",
                side_effect=runner.CompletedTransientReleaseError("publish lock conflict"),
            ):
                with self.assertRaises(runner.CompletedTransientReleaseError):
                    runner.execute(self.args(plan_path))

            self.assertFalse(runner.node_checkpoint_path(plan_path, "xgc2-test").exists())

    def test_resume_verification_without_checkpoint_never_dispatches(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            runner.os.environ,
            {"GH_TOKEN": "test", "XGC2_RELEASE_LOCK_DIGEST": "b" * 64},
            clear=False,
        ):
            plan_path = self.write_plan(directory)
            with mock.patch.object(runner, "verify_release_lock_is_current"), mock.patch.object(
                runner, "trigger"
            ) as trigger, mock.patch.object(
                runner,
                "verify_apt",
                side_effect=runner.ReleaseError("release manifest identity/hash mismatch"),
            ):
                with self.assertRaisesRegex(runner.ReleaseError, "identity/hash mismatch"):
                    runner.execute(
                        self.args(plan_path, verify_existing_release=True)
                    )

            trigger.assert_not_called()


class TransientClassificationTests(unittest.TestCase):
    def test_common_tls_and_transport_failures_are_transient(self):
        messages = (
            "OpenSSL SSL_ERROR_SYSCALL in connection to api.github.com:443",
            "TLS EOF while reading response",
            "unexpected eof while reading",
            "curl: (28) Operation timed out after 30001 milliseconds",
            "read tcp: connection reset by peer",
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertTrue(runner.is_transient_message(message))

    def test_deterministic_build_and_identity_errors_stay_non_transient(self):
        messages = (
            "compiler error: no matching function for call",
            "expected version 1.2.3-4, got 1.2.3-3",
            "source SHA mismatch",
            "release manifest deb SHA256 mismatch",
            "unit test assertion failed",
            "unit test assertion failed after operation timed out",
            "compilation terminated after connection reset by peer",
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertFalse(runner.is_transient_message(message))

    def test_workflow_timeout_is_retryable_because_exact_run_is_known(self):
        with self.assertRaises(runner.TransientReleaseError):
            runner.wait_for_run(
                {"id": "xgc2-test", "repository": "example/xgc2-test"},
                12345,
                timeout_seconds=0,
                poll_seconds=0,
                quality_required=False,
            )


class AptVisibilityEfficiencyTests(unittest.TestCase):
    def test_packages_index_is_fetched_once_per_distribution_arch_each_round(self):
        product = {
            "id": "xgc2-test",
            "expected_version": "1.2.3-4",
            "apt_versions": {"focal": "1.2.3-4~focal"},
            "apt_distributions": ["focal"],
            "apt_packages": ["libxgc2-one", "libxgc2-two"],
        }
        fake_index = [
            {"Package": package, "Version": "1.2.3-4~focal", "SHA256": "d" * 64}
            for package in product["apt_packages"]
        ]
        with mock.patch.object(
            runner, "apt_stanzas", return_value=fake_index
        ) as apt_stanzas, mock.patch.object(
            runner, "package_release_visible", return_value=True
        ) as visible:
            runner.verify_apt(
                product,
                apt_base_url="https://apt.example",
                manifest_base_url="https://apt.example/manifests",
                arches=("amd64", "arm64"),
                timeout_seconds=1,
                poll_seconds=0,
                run_number=None,
                require_current_lock=True,
            )

        self.assertEqual(apt_stanzas.call_count, 2)
        self.assertEqual(visible.call_count, 4)
        self.assertTrue(
            all(call.kwargs["apt_index"] is fake_index for call in visible.call_args_list)
        )

    def test_publish_job_metric_does_not_include_apt_visibility(self):
        data = {
            "jobs": [
                {
                    "name": "publish-apt",
                    "startedAt": "2026-07-10T01:00:00Z",
                    "completedAt": "2026-07-10T01:00:12Z",
                },
                {
                    "name": "build amd64",
                    "startedAt": "2026-07-10T00:00:00Z",
                    "completedAt": "2026-07-10T00:10:00Z",
                },
            ]
        }
        self.assertEqual(runner.publish_job_seconds(data), 12.0)
        self.assertIsNone(runner.publish_job_seconds({"jobs": data["jobs"][1:]}))


class ArtifactManifestTests(unittest.TestCase):
    SOURCE_SHA = "a" * 40
    LOCK_DIGEST = "b" * 64

    @staticmethod
    def fake_deb_metadata(path: Path) -> dict[str, object]:
        return {
            "file": path.name,
            "package": "libxgc2-test",
            "version": "1.2.3-4~focal",
            "architecture": "amd64",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }

    def test_build_manifest_has_ci_identity_and_no_release_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deb_dir = root / "debs"
            deb_dir.mkdir()
            (deb_dir / "libxgc2-test.deb").write_bytes(b"trusted-deb")
            output = root / "build.json"
            args = SimpleNamespace(
                product="xgc2-test",
                version="1.2.3-4",
                source_sha=self.SOURCE_SHA,
                distribution="focal",
                architecture="amd64",
                deb_dir=str(deb_dir),
                ci_run_id="12345",
                ci_workflow="ci",
                ci_workflow_ref="owner/repo/.github/workflows/ci.yml@refs/heads/main",
                output=str(output),
            )
            with mock.patch.object(
                manifest_tool, "deb_metadata", side_effect=self.fake_deb_metadata
            ):
                self.assertEqual(manifest_tool.create_build(args), 0)

            value = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(value["schema"], "xgc2.build-artifact.v1")
            self.assertEqual(value["source_sha"], self.SOURCE_SHA)
            self.assertEqual(value["ci"]["run_id"], "12345")
            self.assertNotIn("release_id", value)
            self.assertNotIn("release_lock_digest", value)
            self.assertEqual(value["debs"][0]["sha256"], hashlib.sha256(b"trusted-deb").hexdigest())

    def test_release_manifest_binds_build_digest_lock_and_deb_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deb_dir = root / "debs"
            deb_dir.mkdir()
            deb = deb_dir / "libxgc2-test.deb"
            deb.write_bytes(b"trusted-deb")
            metadata = self.fake_deb_metadata(deb)
            build = {
                "schema": "xgc2.build-artifact.v1",
                "product": "xgc2-test",
                "version": "1.2.3-4",
                "source_sha": self.SOURCE_SHA,
                "distribution": "focal",
                "architecture": "amd64",
                "ci": {
                    "run_id": "12345",
                    "workflow": "ci",
                    "workflow_ref": "owner/repo/.github/workflows/ci.yml@refs/heads/main",
                },
                "debs": [metadata],
            }
            build_path = root / "build.json"
            build_path.write_text(json.dumps(build, sort_keys=True), encoding="utf-8")
            output_dir = root / "manifests"
            args = SimpleNamespace(
                build_manifest=str(build_path),
                deb_dir=str(deb_dir),
                release_id="release-678",
                release_lock_digest=self.LOCK_DIGEST,
                output_dir=str(output_dir),
            )
            with mock.patch.object(
                manifest_tool, "deb_metadata", side_effect=self.fake_deb_metadata
            ):
                self.assertEqual(manifest_tool.create_release(args), 0)

            output = (
                output_dir
                / "manifests"
                / "xgc2-test"
                / "focal"
                / "amd64"
                / "libxgc2-test_1.2.3-4~focal.json"
            )
            value = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(value["schema"], "xgc2.release-artifact.v1")
            self.assertEqual(value["release_id"], "release-678")
            self.assertEqual(value["release_lock_digest"], self.LOCK_DIGEST)
            self.assertEqual(value["build_manifest_digest"], manifest_tool.sha256(build_path))
            self.assertTrue((output_dir / "build-manifests" / "build.json").is_file())
            self.assertTrue((output_dir / "libxgc2-test.deb").is_file())
            self.assertEqual(value["debs"], [metadata])
            self.assertTrue(value["published_at"].endswith("Z"))

    def test_release_creation_rejects_tampered_deb(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deb_dir = root / "debs"
            deb_dir.mkdir()
            deb = deb_dir / "libxgc2-test.deb"
            deb.write_bytes(b"original")
            metadata = self.fake_deb_metadata(deb)
            build = {
                "schema": "xgc2.build-artifact.v1",
                "product": "xgc2-test",
                "version": "1.2.3-4",
                "source_sha": self.SOURCE_SHA,
                "distribution": "focal",
                "architecture": "amd64",
                "ci": {
                    "run_id": "12345",
                    "workflow": "ci",
                    "workflow_ref": "owner/repo/.github/workflows/ci.yml@refs/heads/main",
                },
                "debs": [metadata],
            }
            build_path = root / "build.json"
            build_path.write_text(json.dumps(build), encoding="utf-8")
            deb.write_bytes(b"tampered")
            args = SimpleNamespace(
                build_manifest=str(build_path),
                deb_dir=str(deb_dir),
                release_id="release-678",
                release_lock_digest=self.LOCK_DIGEST,
                output_dir=str(root / "manifests"),
            )
            with mock.patch.object(
                manifest_tool, "deb_metadata", side_effect=self.fake_deb_metadata
            ):
                with self.assertRaisesRegex(ValueError, "metadata or SHA256 mismatch"):
                    manifest_tool.create_release(args)


class FastPassManifestTests(unittest.TestCase):
    def setUp(self):
        self.product = {
            "id": "xgc2-test",
            "expected_source_sha": "a" * 40,
            "expected_version": "1.2.3-4",
            "apt_versions": {"focal": "1.2.3-4~focal"},
            "apt_distributions": ["focal"],
            "apt_packages": ["libxgc2-test"],
        }
        self.manifest = {
            "schema": "xgc2.release-artifact.v1",
            "product": "xgc2-test",
            "source_sha": "a" * 40,
            "version": "1.2.3-4",
            "distribution": "focal",
            "architecture": "amd64",
            "release_lock_digest": "b" * 64,
            "build_manifest_digest": "c" * 64,
            "debs": [
                {
                    "file": "libxgc2-test.deb",
                    "package": "libxgc2-test",
                    "version": "1.2.3-4~focal",
                    "architecture": "amd64",
                    "sha256": "d" * 64,
                    "size": 123,
                }
            ],
        }

    def test_manifest_match_binds_source_lock_and_apt_sha256(self):
        with mock.patch.dict(
            runner.os.environ, {"XGC2_RELEASE_LOCK_DIGEST": "b" * 64}, clear=False
        ):
            self.assertTrue(
                runner.release_manifest_matches(
                    self.manifest,
                    product=self.product,
                    distribution="focal",
                    arch="amd64",
                    package="libxgc2-test",
                    version="1.2.3-4~focal",
                    apt_sha256="d" * 64,
                    require_current_lock=True,
                )
            )
            for field, bad_value in (
                ("source_sha", "e" * 40),
                ("release_lock_digest", "e" * 64),
            ):
                with self.subTest(field=field):
                    tampered = {**self.manifest, field: bad_value}
                    self.assertFalse(
                        runner.release_manifest_matches(
                            tampered,
                            product=self.product,
                            distribution="focal",
                            arch="amd64",
                            package="libxgc2-test",
                            version="1.2.3-4~focal",
                            apt_sha256="d" * 64,
                            require_current_lock=True,
                        )
                    )
            self.assertFalse(
                runner.release_manifest_matches(
                    self.manifest,
                    product=self.product,
                    distribution="focal",
                    arch="amd64",
                    package="libxgc2-test",
                    version="1.2.3-4~focal",
                    apt_sha256="e" * 64,
                    require_current_lock=True,
                )
            )

    def test_strict_visibility_rejects_existing_mismatched_manifest(self):
        mismatched = {**self.manifest, "source_sha": "e" * 40}
        with mock.patch.object(
            runner,
            "apt_stanzas",
            return_value=[
                {
                    "Package": "libxgc2-test",
                    "Version": "1.2.3-4~focal",
                    "SHA256": "d" * 64,
                }
            ],
        ), mock.patch.object(runner, "read_release_manifest", return_value=mismatched):
            with self.assertRaisesRegex(runner.ReleaseError, "identity/hash mismatch"):
                runner.package_release_visible(
                    self.product,
                    apt_base_url="https://apt.example",
                    manifest_base_url="https://apt.example/manifests",
                    distribution="focal",
                    arch="amd64",
                    package="libxgc2-test",
                    version="1.2.3-4~focal",
                    require_current_lock=True,
                    strict_manifest_mismatch=True,
                )

    def test_verify_only_accepts_manifest_from_an_older_release_lock(self):
        plan = {
            "layers": [
                [
                    {
                        **self.product,
                        "action": "verify",
                    }
                ]
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            args = SimpleNamespace(
                plan=str(plan_path),
                product="xgc2-test",
                apt_arch=[],
                skip_apt_verify=False,
                apt_base_url="https://apt.example",
                manifest_base_url="https://apt.example/manifests",
                apt_timeout_seconds=1,
                poll_seconds=1,
            )
            with mock.patch.dict(runner.os.environ, {"GH_TOKEN": "test"}, clear=False):
                with mock.patch.object(runner, "verify_apt") as verify:
                    self.assertEqual(runner.execute(args), 0)

        self.assertFalse(verify.call_args.kwargs["require_current_lock"])

    def test_fast_pass_requires_every_architecture(self):
        visibility = {
            ("amd64", "libxgc2-test"): True,
            ("arm64", "libxgc2-test"): False,
        }

        def visible(_product, **kwargs):
            return visibility[(kwargs["arch"], kwargs["package"])]

        with mock.patch.object(
            runner, "apt_stanzas", return_value=[]
        ), mock.patch.object(runner, "package_release_visible", side_effect=visible):
            self.assertFalse(
                runner.fast_pass_ready(
                    self.product,
                    apt_base_url="https://apt.example",
                    manifest_base_url="https://apt.example/manifests",
                    arches=("amd64", "arm64"),
                )
            )
            visibility[("arm64", "libxgc2-test")] = True
            self.assertTrue(
                runner.fast_pass_ready(
                    self.product,
                    apt_base_url="https://apt.example",
                    manifest_base_url="https://apt.example/manifests",
                    arches=("amd64", "arm64"),
                )
            )


class ReleasePlanValidationTests(unittest.TestCase):
    @staticmethod
    def plan_item(source: str) -> dict[str, object]:
        return {
            "id": "xgc2-consumer",
            "action": "release",
            "source": source,
            "version": "1.0.0-1",
            "expected_version": "1.0.0-1",
            "apt_versions": {"focal": "1.0.0-1"},
            "apt_packages": ["ros-noetic-xgc2-consumer"],
            "apt_install": ["ros-noetic-xgc2-consumer"],
        }

    @staticmethod
    def write_consumer(root: Path, *, requires: bool) -> Path:
        source = root / "consumer"
        scripts = source / ".xgc2" / "scripts"
        scripts.mkdir(parents=True)
        requirement = "\n  requires:\n  - xgc2-provider" if requires else ""
        (source / ".xgc2" / "product.yml").write_text(
            "\n".join(
                (
                    "schema: xgc2.product.v1",
                    "id: xgc2-consumer",
                    "name: Consumer",
                    "version: 1.0.0-1",
                    "kind: ros1-apt",
                    "apt:",
                    "  distribution: focal",
                    "  packages:",
                    "  - ros-noetic-xgc2-consumer",
                    "  depends: []",
                    f"release:{requirement}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        (scripts / "install_deps.sh").write_text(
            "apt-get install -y ros-noetic-xgc2-provider\n", encoding="utf-8"
        )
        return source

    @staticmethod
    def catalog() -> dict[str, object]:
        return {
            "products": [
                {
                    "id": "xgc2-provider",
                    "version": "2.0.0-1",
                    "apt": {
                        "packages": ["ros-noetic-xgc2-provider"],
                        "install": ["ros-noetic-xgc2-provider"],
                    },
                }
            ]
        }

    def test_hidden_dependency_owner_is_found_outside_selected_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_consumer(root, requires=False)
            plan = {"layers": [[self.plan_item(source.relative_to(root).as_posix())]]}
            errors = plan_validator.validate(root, plan, catalog=self.catalog())

        self.assertTrue(
            any("apt.depends/release.requires" in error for error in errors), errors
        )

    def test_release_requires_satisfies_installation_order_constraint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_consumer(root, requires=True)
            plan = {"layers": [[self.plan_item(source.relative_to(root).as_posix())]]}
            errors = plan_validator.validate(root, plan, catalog=self.catalog())

        self.assertEqual(errors, [])

    def test_distribution_version_must_match_product_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_consumer(root, requires=True)
            item = self.plan_item(source.relative_to(root).as_posix())
            item["apt_versions"] = {"focal": "0.9.0-9~focal"}
            errors = plan_validator.validate(
                root,
                {"layers": [[item]]},
                catalog=self.catalog(),
                allow_planned_updates=True,
            )

        self.assertTrue(any("does not match product version" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
