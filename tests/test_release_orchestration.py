from __future__ import annotations

import importlib.util
import hashlib
import io
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import deque
from dataclasses import replace
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
catalog_collector = load_script("collect-products.py")
scheduler = load_script("schedule-release-plan.py")
runner = load_script("run-release-plan-product.py")
manifest_tool = load_script("create-release-manifest.py")
stage_tool = load_script("stage-product-release.py")
publisher = load_script("release-train-publisher.py")
plan_validator = load_script("validate-release-plan.py")
workflow_audit = load_script("audit-product-workflows.py")
version_bumper = load_script("apply-release-version-bumps.py")


GITHUB_ACTIONS_EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)


def bash_syntax_check_github_run_block(script: str) -> subprocess.CompletedProcess[str]:
    # GitHub expands expressions before invoking the generated shell script.
    # A plain, shell-safe token preserves quoting/word placement for syntax
    # checking without asking Bash to parse the Actions expression language.
    expanded = GITHUB_ACTIONS_EXPRESSION.sub("__XGC2_GITHUB_EXPRESSION__", script)
    return subprocess.run(
        ["bash", "-n"],
        input=expanded,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


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
    def test_duplicate_apt_overlay_prefix_is_forbidden(self):
        pattern = next(
            pattern
            for code, pattern, _message in workflow_audit.FORBIDDEN_PRODUCT_PATTERNS
            if code == "duplicate-apt-overlay-prefix"
        )
        broken = (
            'sed "s#https://xgc2.apt.xiaokang.ink#${XGC2_APT_OVERLAY_URL%/}#g; '
            's#${XGC2_APT_BASE_URL:-https://xgc2.apt.xiaokang.ink}#'
            '${XGC2_APT_OVERLAY_URL%/}#g"'
        )
        fixed = (
            'sed "s#${XGC2_APT_BASE_URL:-https://xgc2.apt.xiaokang.ink}#'
            '${XGC2_APT_OVERLAY_URL%/}#g"'
        )
        self.assertIsNotNone(pattern.search(broken))
        self.assertIsNone(pattern.search(fixed))

    def test_central_workflow_run_blocks_are_valid_bash(self):
        import yaml

        workflow_paths = (
            ROOT / ".github" / "workflows" / "catalog.yml",
            ROOT / ".github" / "workflows" / "release-orchestrator.yml",
        )
        checked = 0
        for workflow_path in workflow_paths:
            workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
            for job_name, job in workflow["jobs"].items():
                for step_index, step in enumerate(job.get("steps", []), start=1):
                    script = step.get("run")
                    if not isinstance(script, str):
                        continue
                    checked += 1
                    result = bash_syntax_check_github_run_block(script)
                    label = step.get("name", f"step {step_index}")
                    self.assertEqual(
                        0,
                        result.returncode,
                        f"{workflow_path.name}:{job_name}:{label}: {result.stderr}",
                    )
        self.assertGreater(checked, 0)

    def test_central_ci_never_enables_implicit_dependency_policies(self):
        for workflow_name in ("catalog.yml", "release-orchestrator.yml"):
            workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("--allow-implicit-dependency-policy", workflow)

    def test_run_block_syntax_check_understands_python_heredocs_and_bad_delimiters(self):
        valid_python_heredoc = """\
python3 - <<'PY'
import json
if True:
    print(json.dumps({"ok": True}))
PY
"""
        self.assertEqual(
            0,
            bash_syntax_check_github_run_block(valid_python_heredoc).returncode,
        )

        indented_delimiter = """\
if [[ -s release-state.json ]]; then
  python3 - <<'PY'
  import json
  PY
fi
"""
        result = bash_syntax_check_github_run_block(indented_delimiter)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("here-document", result.stderr)

    def test_orchestrator_never_interpolates_dispatch_inputs_inside_shell(self):
        import yaml

        workflow = yaml.safe_load(
            (ROOT / ".github" / "workflows" / "release-orchestrator.yml").read_text(
                encoding="utf-8"
            )
        )
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                self.assertNotIn("${{ inputs.", str(step.get("run", "")))

    def test_resume_state_is_preserved_across_plan_and_execute_jobs(self):
        workflow_text = (
            ROOT / ".github" / "workflows" / "release-orchestrator.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "cp .work/resume/release-state.json .work/release-resume-state.json",
            workflow_text,
        )
        self.assertIn(".work/release-resume-state.json", workflow_text)
        self.assertIn(
            "args+=(--resume-state .work/release-resume-state.json)",
            workflow_text,
        )

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

    def test_build_manifest_schema_audit_rejects_legacy_generator(self):
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory)
            tool = source_dir / ".xgc2" / "scripts" / "xgc2_artifact_manifest.py"
            tool.parent.mkdir(parents=True)
            tool.write_text(
                'SCHEMA = "xgc2.artifact-manifest.v1"\n', encoding="utf-8"
            )
            self.assertFalse(
                workflow_audit.build_manifest_tool_uses_current_schema(source_dir)
            )
            tool.write_text(
                'SCHEMA = "xgc2.build-artifact.v1"\n'
                '# xgc2.artifact-manifest.v1 must remain rejected\n',
                encoding="utf-8",
            )
            self.assertFalse(
                workflow_audit.build_manifest_tool_uses_current_schema(source_dir)
            )
            tool.write_text(
                'SCHEMA = "xgc2.build-artifact.v1"\n',
                encoding="utf-8",
            )
            self.assertTrue(
                workflow_audit.build_manifest_tool_uses_current_schema(source_dir)
            )


class CatalogDependencyPolicyTests(unittest.TestCase):
    def test_catalog_exclusions_require_ids_reasons_and_unique_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "exclusions.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "xgc2.catalog-exclusions.v1",
                        "products": [
                            {"id": "inactive-product", "reason": "Not on runtime path."}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                catalog_collector.load_excluded_product_ids(path),
                {"inactive-product"},
            )

            path.write_text(
                json.dumps(
                    {
                        "schema": "xgc2.catalog-exclusions.v1",
                        "products": [
                            {"id": "duplicate", "reason": "First."},
                            {"id": "duplicate", "reason": "Second."},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate excluded product id"):
                catalog_collector.load_excluded_product_ids(path)

    @staticmethod
    def product(
        product_id: str,
        package: str,
        *,
        depends: list[str] | None = None,
        recommends: list[str] | None = None,
        release: dict[str, object] | None = None,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "id": product_id,
            "apt": {
                "install": [package],
                "packages": [package],
                "depends": depends or [],
                "recommends": recommends or [],
            },
        }
        if release is not None:
            value["release"] = release
        return value

    def test_catalog_rejects_missing_internal_policy(self):
        products = [
            self.product("provider", "provider-deb"),
            self.product("consumer", "consumer-deb", depends=["provider-deb"]),
        ]

        self.assertEqual(
            catalog_collector.validate_internal_dependency_policies(products),
            [
                "consumer: missing release.dependency_policy[provider] "
                "for internal apt.depends edge"
            ],
        )

    def test_catalog_accepts_explicit_policy_and_external_dependency(self):
        products = [
            self.product("provider", "provider-deb"),
            self.product(
                "consumer",
                "consumer-deb",
                depends=["provider-deb", "curl"],
                release={"dependency_policy": {"provider": "verify"}},
            ),
        ]

        self.assertEqual(
            catalog_collector.validate_internal_dependency_policies(products),
            [],
        )

    def test_catalog_recommends_is_a_direct_internal_edge(self):
        products = [
            self.product("provider", "provider-deb"),
            self.product(
                "consumer",
                "consumer-deb",
                recommends=["provider-deb"],
            ),
        ]

        self.assertEqual(
            catalog_collector.validate_internal_dependency_policies(products),
            [
                "consumer: missing release.dependency_policy[provider] "
                "for internal apt.recommends edge"
            ],
        )

    def test_catalog_legacy_escape_hatch_only_suppresses_missing_policy(self):
        products = [
            self.product("provider", "provider-deb"),
            self.product(
                "consumer",
                "consumer-deb",
                depends=["provider-deb"],
                release={"dependency_policy": {"not-direct": "order"}},
            ),
        ]

        errors = catalog_collector.validate_internal_dependency_policies(
            products,
            allow_implicit=True,
        )

        self.assertEqual(
            errors,
            [
                "consumer: release.dependency_policy references non-direct "
                "upstream product(s): not-direct"
            ],
        )


class DagTests(unittest.TestCase):
    @staticmethod
    def product(
        product_id: str,
        *,
        apt_package: str | None = None,
        apt_depends: tuple[str, ...] = (),
        apt_recommends: tuple[str, ...] = (),
        release: dict[str, object] | None = None,
    ) -> object:
        package = apt_package or product_id
        return planner.Product(
            product_id=product_id,
            name=product_id,
            kind="ros1-apt",
            version="1.0.0-1",
            source_file=Path(f"products/{product_id}/.xgc2/product.yml"),
            source_dir=Path(f"products/{product_id}"),
            apt_distributions=("focal",),
            apt_install=(package,),
            apt_packages=(package,),
            apt_depends=apt_depends,
            groups=(),
            release=release or {},
            apt_recommends=apt_recommends,
        )

    def test_mixed_product_with_deb_metadata_is_an_apt_product(self):
        product = planner.Product(
            product_id="xgc2-fs150",
            name="FS150",
            kind="mixed",
            version="1.0.0-1",
            source_file=Path("products/fs150/.xgc2/product.yml"),
            source_dir=Path("products/fs150"),
            apt_distributions=("focal",),
            apt_install=("xgc2-fs150",),
            apt_packages=("xgc2-fs150",),
            apt_depends=(),
            groups=(),
            release={},
        )
        self.assertTrue(product.is_apt)

    def test_package_architecture_and_distribution_overrides_are_validated(self):
        product = planner.Product(
            product_id="xgc2-mixed-package",
            name="Mixed package",
            kind="ros1-apt",
            version="1.0.0-1",
            source_file=Path("product.yml"),
            source_dir=Path("."),
            apt_distributions=("bionic", "focal"),
            apt_install=("core",),
            apt_packages=("core", "amd-only", "melodic-only"),
            apt_depends=(),
            groups=(),
            release={},
            apt_package_architectures={"amd-only": ("amd64",)},
            apt_package_distributions={"melodic-only": ("bionic",)},
        )
        self.assertEqual(product.package_architectures["amd-only"], ("amd64",))
        self.assertEqual(product.package_distributions["melodic-only"], ("bionic",))
        self.assertEqual(product.package_architectures["core"], ("amd64", "arm64"))

    def test_install_package_cannot_be_architecture_specific(self):
        product = planner.Product(
            product_id="bad", name="bad", kind="apt", version="1",
            source_file=Path("product.yml"), source_dir=Path("."),
            apt_distributions=("focal",), apt_install=("core",),
            apt_packages=("core",), apt_depends=(), groups=(), release={},
            apt_package_architectures={"core": ("amd64",)},
        )
        with self.assertRaisesRegex(ValueError, "must support amd64 and arm64"):
            _ = product.package_architectures

    def test_dependency_actions_rebuild_verify_and_order(self):
        downstream = {"a": {"b", "c", "d"}, "b": {"e"}, "c": set(), "d": set(), "e": set()}
        policies = {
            ("a", "b"): "rebuild",
            ("a", "c"): "verify",
            ("a", "d"): "order",
            ("b", "e"): "rebuild",
        }
        self.assertEqual(
            planner.propagate_downstream_actions({"a"}, downstream, policies),
            {
                "a": "release",
                "b": "release",
                "c": "compatibility-verify",
                "e": "release",
            },
        )

    def test_internal_apt_edge_requires_explicit_policy(self):
        provider = self.product("provider", apt_package="provider-deb")
        consumer = self.product(
            "consumer",
            apt_depends=("provider-deb (>= 1.0.0-1)",),
        )

        with self.assertRaisesRegex(
            ValueError,
            r"consumer <- provider \(apt\.depends; legacy default=rebuild\)",
        ):
            planner.build_graph([provider, consumer])

    def test_release_requires_edge_requires_explicit_policy(self):
        provider = self.product("provider")
        consumer = self.product(
            "consumer",
            release={"requires": ["provider"]},
        )

        with self.assertRaisesRegex(
            ValueError,
            r"consumer <- provider \(release\.requires; legacy default=order\)",
        ):
            planner.build_graph([provider, consumer])

    def test_internal_recommends_edge_requires_explicit_policy(self):
        provider = self.product("provider", apt_package="provider-deb")
        consumer = self.product(
            "consumer",
            apt_recommends=("provider-deb (>= 1.0.0-1)",),
        )

        with self.assertRaisesRegex(
            ValueError,
            r"consumer <- provider \(apt\.recommends; legacy default=verify\)",
        ):
            planner.build_graph([provider, consumer])

    def test_explicit_policy_is_used_for_direct_internal_edge(self):
        provider = self.product("provider", apt_package="provider-deb")
        consumer = self.product(
            "consumer",
            apt_depends=("provider-deb",),
            release={"dependency_policy": {"provider": "verify"}},
        )

        downstream, upstream, policies, sources = planner.build_graph([provider, consumer])

        self.assertEqual(downstream["provider"], {"consumer"})
        self.assertEqual(upstream["consumer"], {"provider"})
        self.assertEqual(policies[("provider", "consumer")], "verify")
        self.assertEqual(sources[("provider", "consumer")], ("apt.depends",))

    def test_legacy_escape_hatch_preserves_implicit_defaults(self):
        apt_provider = self.product("apt-provider", apt_package="provider-deb")
        order_provider = self.product("order-provider")
        consumer = self.product(
            "consumer",
            apt_depends=("provider-deb",),
            release={"requires": ["order-provider"]},
        )

        _downstream, _upstream, policies, sources = planner.build_graph(
            [apt_provider, order_provider, consumer],
            allow_implicit_dependency_policy=True,
        )

        self.assertEqual(policies[("apt-provider", "consumer")], "rebuild")
        self.assertEqual(policies[("order-provider", "consumer")], "order")
        self.assertEqual(sources[("apt-provider", "consumer")], ("apt.depends",))
        self.assertEqual(sources[("order-provider", "consumer")], ("release.requires",))

    def test_load_catalog_forwards_legacy_policy_flag_to_automatic_collection(self):
        captured: list[str] = []

        def fake_run(args, **kwargs):
            del kwargs
            captured.extend(args)
            catalog_path = root / ".work" / "release-products.json"
            catalog_path.parent.mkdir(parents=True, exist_ok=True)
            catalog_path.write_text('{"products": []}\n', encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            planner,
            "run",
            side_effect=fake_run,
        ):
            root = Path(directory)
            self.assertEqual(
                planner.load_catalog(
                    root,
                    None,
                    allow_implicit_dependency_policy=True,
                ),
                [],
            )

        self.assertIn("--allow-implicit-dependency-policy", captured)

    def test_plan_preserves_dependency_edge_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = replace(
                self.product("provider", apt_package="provider-deb"),
                source_dir=root / "provider",
                source_file=root / "provider" / ".xgc2" / "product.yml",
            )
            consumer = replace(
                self.product(
                    "consumer",
                    apt_recommends=("provider-deb",),
                    release={"dependency_policy": {"provider": "verify"}},
                ),
                source_dir=root / "consumer",
                source_file=root / "consumer" / ".xgc2" / "product.yml",
            )
            for product in (provider, consumer):
                product.source_dir.mkdir()

            downstream, _upstream, policies, sources = planner.build_graph(
                [provider, consumer]
            )

            def target(product):
                return planner.ReleaseTarget(
                    product=product,
                    repository=f"example/{product.product_id}",
                    ref="main",
                    workflow="release.yml",
                    workflow_path=None,
                    dispatch_inputs={},
                    action="release",
                    source_sha="a" * 40,
                    expected_version=product.version,
                    expected_apt_versions={"focal": product.version},
                )

            plan = planner.product_plan_json(
                root,
                [["provider"], ["consumer"]],
                {"provider": target(provider), "consumer": target(consumer)},
                downstream,
                policies,
                sources,
            )

        consumer_item = plan["layers"][1][0]
        self.assertEqual(
            consumer_item["dependency_sources"],
            {"provider": ["apt.recommends"]},
        )

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
            "schema": "xgc2.release-state.v2",
            "release_id": "test-release",
            "phase": "succeeded",
            "plan_digest": "plan-1",
            "release_lock_digest": "lock-1",
            "execution_policy_digest": "",
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
            "schema": "xgc2.release-state.v2",
            "release_id": "test-release",
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
            "schema": "xgc2.release-state.v2",
            "release_id": "test-release",
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

    def test_resume_rejects_different_stable_release_id(self):
        previous = {
            "schema": "xgc2.release-state.v2",
            "release_id": "release-old",
            "plan_digest": "plan-1",
            "release_lock_digest": "lock-1",
            "products": {"a": {"status": "success"}},
        }
        with self.assertRaisesRegex(ValueError, "release id"):
            scheduler.initial_state(
                {"a": {"id": "a"}},
                previous,
                plan_digest="plan-1",
                release_lock_digest="lock-1",
                release_id="release-new",
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

    def test_resume_rejects_different_execution_policy(self):
        previous = {
            "schema": "xgc2.release-state.v2",
            "release_id": "test-release",
            "plan_digest": "plan",
            "release_lock_digest": "lock",
            "execution_policy_digest": "old-policy",
            "products": {"a": {"status": "success"}},
        }
        with self.assertRaisesRegex(ValueError, "execution policy"):
            scheduler.initial_state(
                {"a": {"id": "a"}}, previous,
                plan_digest="plan", release_lock_digest="lock",
                execution_policy_digest="new-policy",
            )

    def test_resumed_success_must_reverify_before_releasing_downstream(self):
        plan = {
            "layers": [
                [{"id": "upstream", "dependencies": []}],
                [{"id": "downstream", "dependencies": ["upstream"]}],
            ]
        }
        previous = {
            "schema": "xgc2.release-state.v2",
            "release_id": "test-release",
            "phase": "succeeded",
            "plan_digest": scheduler.canonical_digest(plan),
            "release_lock_digest": "lock-1",
            "execution_policy_digest": scheduler.canonical_digest(
                scheduler.execution_policy_value(
                    quality_required=False,
                    source_tests=False,
                    reuse_ci_artifacts=True,
                )
            ),
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

    def test_plan_lock_requires_exact_content_equivalence(self):
        item = {"id": "a", "expected_source_sha": "a" * 40}
        plan = {"schema": "xgc2.release-plan.v2", "layers": [[item]]}
        scheduler.validate_plan_lock_equivalence(
            plan, {"schema": "xgc2.release-lock.v2", "products": [item]}
        )
        with self.assertRaisesRegex(ValueError, "not equivalent"):
            scheduler.validate_plan_lock_equivalence(
                plan,
                {"schema": "xgc2.release-lock.v2", "products": [{**item, "id": "b"}]},
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
            process = mock.Mock(stdout=io.StringIO("runner output\n"))
            process.wait.return_value = returncode
            forwarded = io.StringIO()
            with self.subTest(returncode=returncode), mock.patch.object(
                scheduler.subprocess, "Popen", return_value=process
            ) as invoked, mock.patch.object(scheduler.sys, "stdout", forwarded):
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
            self.assertTrue(result["output_streamed"])
            self.assertEqual(result["output"], "runner output\n")
            self.assertEqual(invoked.call_args.args[0][:2], [sys.executable, "-u"])
            self.assertEqual(forwarded.getvalue(), "runner output\n")

    def test_scheduler_passes_bounded_apt_visibility_timeout_to_worker(self):
        process = mock.Mock(stdout=io.StringIO("ok\n"))
        process.wait.return_value = 0
        with mock.patch.object(
            scheduler.subprocess, "Popen", return_value=process
        ) as invoked, mock.patch.object(scheduler.sys, "stdout", io.StringIO()):
            scheduler.run_product(
                Path("runner.py"),
                Path("plan.json"),
                "product",
                quality_required=False,
                source_tests=False,
                reuse_ci_artifacts=True,
                apt_timeout_seconds=120,
            )
        command = invoked.call_args.args[0]
        self.assertIn("--apt-timeout-seconds", command)
        self.assertEqual(command[command.index("--apt-timeout-seconds") + 1], "120")

    def test_shared_apt_circuit_enforces_global_backoff_across_products(self):
        plan = {"layers": [[
            {"id": "a", "dependencies": []},
            {"id": "b", "dependencies": []},
        ]]}
        clock = FakeClock()
        attempts: dict[str, int] = {}
        calls: list[tuple[str, int, float]] = []
        calls_lock = threading.Lock()

        def fake_run_product(*args, **kwargs):
            del kwargs
            product_id = args[2]
            with calls_lock:
                attempt = attempts.get(product_id, 0) + 1
                attempts[product_id] = attempt
                calls.append((product_id, attempt, clock.time()))
            if (product_id == "a" and attempt <= 3) or (
                product_id == "b" and attempt == 1
            ):
                return {
                    "status": "transient",
                    "returncode": 75,
                    "output": f"{product_id}: APT index/manifest not visible",
                    "duration_seconds": 0.1,
                }
            return {
                "status": "success", "returncode": 0,
                "output": "ok", "duration_seconds": 0.1,
            }

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            scheduler, "run_product", side_effect=fake_run_product
        ):
            state = scheduler.schedule(
                plan,
                plan_path=Path(directory) / "plan.json",
                state_path=Path(directory) / "state.json",
                runner=Path("runner.py"),
                max_parallel=2,
                quality_required=False,
                source_tests=False,
                release_lock_digest="lock-1",
                retry_delays=(15, 30, 60),
                sleep_fn=clock.sleep,
                now_fn=clock.time,
            )

        self.assertEqual(clock.sleeps, [15, 30, 60])
        self.assertLess(calls.index(("a", 4, 1105.0)), calls.index(("b", 2, 1105.0)))
        self.assertEqual(state["shared_infra_circuit_trips"], 1)
        self.assertNotIn("shared_infra_circuit", state)
        self.assertTrue(all(node["status"] == "success" for node in state["products"].values()))

    def test_open_circuit_uses_pending_node_when_no_retry_wait_exists(self):
        plan = {"layers": [[{"id": "pending", "dependencies": []}]]}
        clock = FakeClock()
        initial = {
            "schema": "xgc2.release-state.v2",
            "release_id": "test-release",
            "plan_digest": scheduler.canonical_digest(plan),
            "release_lock_digest": "lock-1",
            "execution_policy_digest": "",
            "started_at": int(clock.time()),
            "phase": "preparing",
            "products": {
                "pending": {
                    "status": "pending", "queued_at": int(clock.time()),
                    "attempts": 0, "transient_retries": 0,
                }
            },
            "shared_infra_circuit": {
                "status": "open", "opened_by": "finished-node",
                "opened_at": int(clock.time()), "next_probe_at": clock.time() + 15,
            },
        }
        success = {
            "status": "success", "returncode": 0,
            "output": "ok", "duration_seconds": 0.1,
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            scheduler, "initial_state", return_value=initial
        ), mock.patch.object(
            scheduler, "run_product", return_value=success
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

        self.assertEqual(clock.sleeps, [15])
        run_product.assert_called_once()
        self.assertEqual(state["products"]["pending"]["status"], "success")
        self.assertNotIn("shared_infra_circuit", state)

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
                        '"publish_seconds": 1.25, "stage_queue_seconds": 2.75, '
                        '"reused_ci_artifact": false}'
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
        self.assertEqual(node["stage_queue_seconds"], 2.75)
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

    def test_same_train_resume_skips_stage_and_promote(self):
        plan = {"layers": [[{"id": "leaf", "action": "release", "dependencies": []}]]}
        state = {
            "schema": "xgc2.release-state.v2",
            "release_id": "release-1",
            "release_lock_digest": "b" * 64,
            "plan_digest": scheduler.canonical_digest(plan),
            "phase": "prepared",
            "resume_phase": "succeeded",
            "train_digest": "c" * 64,
            "products": {
                "leaf": {"status": "success", "prepare_checkpoint": "production_verified"}
            },
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            scheduler, "run_global_command"
        ) as global_command, mock.patch.object(
            scheduler, "verify_promoted_product"
        ) as verify, mock.patch.object(scheduler, "reconcile_plan_ci"):
            root = Path(directory)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            result = scheduler.finalize_release_train(
                plan,
                state,
                plan_path=plan_path,
                state_path=root / "state.json",
                runner=Path("runner.py"),
                release_lock_digest="b" * 64,
                release_id="release-1",
                max_parallel=1,
                quality_required=False,
                source_tests=False,
            )
        self.assertEqual(result["phase"], "succeeded")
        global_command.assert_not_called()
        verify.assert_not_called()

    def test_prepared_train_invokes_exactly_one_global_promote(self):
        plan = {
            "schema": "xgc2.release-plan.v2",
            "layers": [[{
                "id": "leaf",
                "action": "release",
                "expected_version": "1.0.0-1",
                "expected_source_sha": "a" * 40,
                "apt_distributions": ["focal"],
                "dependencies": [],
            }]],
        }
        state = {
            "schema": "xgc2.release-state.v2",
            "release_id": "release-1",
            "release_lock_digest": "b" * 64,
            "plan_digest": scheduler.canonical_digest(plan),
            "phase": "prepared",
            "products": {"leaf": {"status": "success"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            receipt_dir = root / "release-stage-receipts"
            receipt_dir.mkdir()
            (receipt_dir / "leaf.json").write_text(
                json.dumps({
                    "schema": "xgc2.stage-receipt.v1",
                    "release_id": "release-1",
                    "release_lock_digest": "b" * 64,
                    "product": "leaf",
                    "run_id": 1,
                    "products": [{
                        "product": "leaf",
                        "distribution": "focal",
                        "version": "1.0.0-1",
                        "source_sha": "a" * 40,
                        "bundle_digest": "c" * 64,
                        "build_manifest_digests": ["d" * 64],
                        "debs": [{
                            "package": "leaf",
                            "version": "1.0.0-1",
                            "architecture": "amd64",
                            "sha256": "e" * 64,
                        }],
                    }],
                }),
                encoding="utf-8",
            )
            commands: list[list[str]] = []

            def global_command(command, **_kwargs):
                commands.append(command)
                if "train" in command:
                    completed = scheduler.subprocess.run(
                        command, text=True, stdout=scheduler.subprocess.PIPE,
                        stderr=scheduler.subprocess.STDOUT,
                    )
                    if completed.returncode:
                        raise AssertionError(completed.stdout)
                    return completed
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps({
                        "train_digest": "f" * 64,
                        "promoted_at": "2026-07-10T00:00:00Z",
                    }),
                )

            with mock.patch.object(
                scheduler, "run_global_command", side_effect=global_command
            ), mock.patch.object(
                scheduler, "verify_plan_freshness"
            ), mock.patch.object(
                scheduler,
                "verify_promoted_product",
                return_value={
                    "status": "success", "returncode": 0,
                    "output": 'XGC2_RESULT={"prepare_checkpoint":"production_verified"}',
                    "duration_seconds": 0.1,
                },
            ), mock.patch.object(scheduler, "reconcile_plan_ci"):
                result = scheduler.finalize_release_train(
                    plan,
                    state,
                    plan_path=plan_path,
                    state_path=root / "state.json",
                    runner=Path("runner.py"),
                    release_lock_digest="b" * 64,
                    release_id="release-1",
                    max_parallel=1,
                    quality_required=False,
                    source_tests=False,
                )
        promote_commands = [command for command in commands if "promote" in command]
        self.assertEqual(len(promote_commands), 1)
        self.assertEqual(result["phase"], "succeeded")


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

    def test_trusted_candidate_download_is_retained_for_single_transfer(self):
        product = self.product()
        runs = [{
            "databaseId": 12345, "status": "completed", "conclusion": "success",
            "headSha": product["expected_source_sha"], "event": "push",
            "createdAt": "2026-07-10T01:00:00Z",
        }]
        downloads = 0

        def fake_run(command, check=True, timeout=None):
            nonlocal downloads
            if command[:3] == ["gh", "run", "list"]:
                self.assertIsNone(timeout)
                return mock.Mock(returncode=0, stdout=json.dumps(runs), stderr="")
            if command[:2] == ["gh", "api"]:
                self.assertIsNone(timeout)
                return mock.Mock(returncode=0, stdout="1\n", stderr="")
            if command[:3] == ["gh", "run", "download"]:
                self.assertEqual(timeout, runner.ARTIFACT_DOWNLOAD_TIMEOUT_SECONDS)
                downloads += 1
                output = Path(command[command.index("--dir") + 1])
                for arch in ("amd64", "arm64"):
                    target = output / arch
                    target.mkdir(parents=True, exist_ok=True)
                    (target / f"{arch}.json").write_text(json.dumps({
                        "schema": "xgc2.build-artifact.v1", "product": product["id"],
                        "source_sha": product["expected_source_sha"],
                        "version": product["expected_version"], "distribution": "focal",
                        "architecture": arch, "ci": {"run_id": 12345},
                        "debs": [{
                            "file": f"x_{arch}.deb", "package": "x",
                            "version": product["expected_version"], "architecture": arch,
                            "sha256": "d" * 64, "size": 1,
                        }],
                    }), encoding="utf-8")
                return mock.Mock(returncode=0, stdout="", stderr="")
            raise AssertionError(command)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            runner, "run", side_effect=fake_run
        ):
            cache = Path(directory)
            self.assertEqual(
                runner.find_trusted_ci_run(
                    product, wait_seconds=0, poll_seconds=1, artifact_cache_root=cache
                ),
                12345,
            )
            self.assertTrue((cache / "12345").is_dir())
        self.assertEqual(downloads, 1)

    def test_artifact_download_timeout_is_transient_and_bounded(self):
        product = self.product()
        timeout = subprocess.TimeoutExpired(
            ["gh", "run", "download"], runner.ARTIFACT_DOWNLOAD_TIMEOUT_SECONDS
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "artifacts"
            with mock.patch.object(runner, "run", side_effect=timeout) as gh_run:
                with self.assertRaisesRegex(
                    runner.TransientReleaseError,
                    rf"timed out after {runner.ARTIFACT_DOWNLOAD_TIMEOUT_SECONDS}s",
                ):
                    runner.download_run_artifacts(product, 12345, output)
        self.assertEqual(
            gh_run.call_args.kwargs["timeout"], runner.ARTIFACT_DOWNLOAD_TIMEOUT_SECONDS
        )

    def test_trusted_ci_artifact_download_timeout_is_transient_and_bounded(self):
        product = self.product()
        timeout = subprocess.TimeoutExpired(
            ["gh", "run", "download"], runner.ARTIFACT_DOWNLOAD_TIMEOUT_SECONDS
        )
        with mock.patch.object(runner, "run", side_effect=timeout) as gh_run:
            with self.assertRaisesRegex(
                runner.TransientReleaseError,
                rf"trusted CI artifact download from run 12345 timed out after "
                rf"{runner.ARTIFACT_DOWNLOAD_TIMEOUT_SECONDS}s",
            ):
                runner.trusted_ci_artifacts_match(product, 12345)
        self.assertEqual(
            gh_run.call_args.kwargs["timeout"], runner.ARTIFACT_DOWNLOAD_TIMEOUT_SECONDS
        )

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

    def test_central_overlay_uses_production_packages_for_identical_skipped_deb(self):
        product = {
            "id": "xgc2-test",
            "expected_source_sha": "a" * 40,
            "expected_version": "1.2.3-4",
            "apt_versions": {"focal": "1.2.3-4~focal"},
            "apt_distributions": ["focal"],
            "apt_packages": ["libxgc2-test"],
        }
        apt_sha = "d" * 64
        production_stanza = {
            "Package": "libxgc2-test",
            "Version": "1.2.3-4~focal",
            "Architecture": "amd64",
            "SHA256": apt_sha,
        }
        overlay_manifest = {
            "schema": "xgc2.release-artifact.v1",
            "product": "xgc2-test",
            "source_sha": "a" * 40,
            "version": "1.2.3-4",
            "distribution": "focal",
            "architecture": "amd64",
            "release_lock_digest": "b" * 64,
            "build_manifest_digest": "c" * 64,
            "debs": [{
                "package": "libxgc2-test",
                "version": "1.2.3-4~focal",
                "architecture": "amd64",
                "sha256": apt_sha,
            }],
        }

        def indexes(base_url, _distribution, _arch):
            return [] if base_url == "https://apt.example/staging/release-1" else [
                production_stanza
            ]

        with mock.patch.dict(
            runner.os.environ,
            {"XGC2_RELEASE_LOCK_DIGEST": "b" * 64},
            clear=False,
        ), mock.patch.object(
            runner, "apt_stanzas", side_effect=indexes
        ) as apt_stanzas, mock.patch.object(
            runner, "read_release_manifest", return_value=overlay_manifest
        ) as read_manifest:
            runner.verify_apt(
                product,
                apt_base_url="https://apt.example/staging/release-1",
                apt_fallback_base_url="https://apt.example",
                manifest_base_url="https://apt.example/staging/release-1/manifests",
                arches=("amd64",),
                timeout_seconds=1,
                poll_seconds=0,
                run_number=None,
                require_current_lock=True,
            )

        self.assertEqual(
            [call.args[0] for call in apt_stanzas.call_args_list],
            ["https://apt.example/staging/release-1", "https://apt.example"],
        )
        self.assertIn(
            "https://apt.example/staging/release-1/manifests/",
            read_manifest.call_args.args[0],
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

    def test_visibility_respects_distribution_and_architecture_package_map(self):
        product = {
            "id": "mapped", "expected_source_sha": "a" * 40,
            "expected_version": "1-1", "apt_versions": {"bionic": "1-1", "focal": "1-1"},
            "apt_distributions": ["bionic", "focal"], "apt_install": ["core"],
            "apt_packages": ["core", "amd-only", "focal-only"],
            "apt_package_architectures": {"amd-only": ["amd64"]},
            "apt_package_distributions": {"focal-only": ["focal"]},
        }
        seen: set[tuple[str, str, str]] = set()

        def visible(_product, **kwargs):
            seen.add((kwargs["distribution"], kwargs["arch"], kwargs["package"]))
            return True

        with mock.patch.object(runner, "apt_stanzas", return_value=[]), mock.patch.object(
            runner, "package_release_visible", side_effect=visible
        ):
            runner.verify_apt(
                product, apt_base_url="https://staging",
                manifest_base_url="https://staging/manifests",
                arches=("amd64", "arm64"), timeout_seconds=1, poll_seconds=0,
                run_number=None, require_current_lock=True,
            )
        self.assertNotIn(("bionic", "arm64", "amd-only"), seen)
        self.assertNotIn(("bionic", "amd64", "focal-only"), seen)
        self.assertIn(("focal", "amd64", "amd-only"), seen)
        self.assertIn(("focal", "arm64", "focal-only"), seen)


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


class CentralStagingTests(unittest.TestCase):
    @staticmethod
    def fake_deb_metadata(path: Path) -> dict[str, object]:
        architecture = "amd64" if "amd64" in path.name else "arm64"
        return {
            "file": path.name,
            "package": "xgc2-test",
            "version": "1.2.3-4",
            "architecture": architecture,
            "sha256": stage_tool.sha256(path),
            "size": path.stat().st_size,
        }

    @staticmethod
    def resumable_product() -> dict[str, object]:
        return {
            "id": "x", "action": "release", "expected_source_sha": "a" * 40,
            "expected_version": "1-1", "apt_versions": {"focal": "1-1"},
            "apt_distributions": ["focal"], "apt_packages": ["x"],
            "apt_install": ["x"], "dependency_set_digest": "d" * 64,
        }

    @staticmethod
    def resumable_args(plan_path: Path) -> SimpleNamespace:
        return SimpleNamespace(
            plan=str(plan_path), product="x", apt_arch=[], verify_production=False,
            verify_lock_only=False, reconcile_ci=False, apt_overlay_url="",
            apt_base_url="https://apt.example",
            manifest_base_url="https://apt.example/manifests",
            apt_timeout_seconds=1, poll_seconds=0, reuse_ci_artifacts=True,
            ci_wait_seconds=0, timeout_seconds=1, quality_required=False,
            source_tests=False,
        )

    @staticmethod
    def staged_product(bundle_digest: str = "e" * 64) -> dict[str, object]:
        return {
            "product": "x", "distribution": "focal", "version": "1-1",
            "source_sha": "a" * 40, "bundle_digest": bundle_digest,
            "build_manifest_digests": ["f" * 64],
            "debs": [{
                "package": "x", "version": "1-1", "architecture": "all",
                "sha256": "1" * 64,
            }],
        }

    @staticmethod
    def server_stage_status(
        staged_product: dict[str, object], *, status: str = "promoting"
    ) -> dict[str, object]:
        digest = str(staged_product["bundle_digest"])
        return {
            "status": status,
            "bundles": {digest: {
                "product": dict(staged_product), "manifests": [{"path": "x"}],
            }},
            "distributions": {"focal": {"published": True}},
        }

    def test_publisher_stage_lock_serializes_two_callers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_acquired = threading.Event()
            second_attempting = threading.Event()
            second_acquired = threading.Event()
            release_first = threading.Event()
            queue_seconds: dict[str, float] = {}
            errors: list[BaseException] = []

            def first() -> None:
                try:
                    with runner.publisher_stage_lock(root) as waited:
                        queue_seconds["first"] = waited
                        first_acquired.set()
                        release_first.wait(2)
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            def second() -> None:
                try:
                    second_attempting.set()
                    with runner.publisher_stage_lock(root) as waited:
                        queue_seconds["second"] = waited
                        second_acquired.set()
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            first_thread = threading.Thread(target=first)
            second_thread = threading.Thread(target=second)
            first_thread.start()
            self.assertTrue(first_acquired.wait(1))
            second_thread.start()
            self.assertTrue(second_attempting.wait(1))
            try:
                self.assertFalse(second_acquired.wait(0.05))
            finally:
                release_first.set()
            first_thread.join(2)
            second_thread.join(2)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(second_acquired.is_set())
            self.assertGreaterEqual(queue_seconds["second"], 0.04)

    def test_trusted_artifacts_form_deterministic_bundle_and_release_train(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            plan_path = root / "plan.json"
            receipt_dir = root / "receipts"
            receipt = receipt_dir / "xgc2-test.json"
            plan = {
                "schema": "xgc2.release-plan.v2",
                "layers": [[{
                    "id": "xgc2-test",
                    "action": "release",
                    "expected_source_sha": "a" * 40,
                    "expected_version": "1.2.3-4",
                    "version": "1.2.3-3",
                    "apt_distributions": ["focal"],
                    "apt_versions": {"focal": "1.2.3-4"},
                    "apt_packages": ["xgc2-test"],
                    "apt_install": ["xgc2-test"],
                }]],
            }
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            for architecture in ("amd64", "arm64"):
                artifact = artifact_root / architecture
                artifact.mkdir(parents=True)
                deb = artifact / f"xgc2-test_1.2.3-4_{architecture}.deb"
                deb.write_bytes(f"fake-{architecture}".encode())
                entry = self.fake_deb_metadata(deb)
                manifest = {
                    "schema": "xgc2.build-artifact.v1",
                    "product": "xgc2-test",
                    "source_sha": "a" * 40,
                    "version": "1.2.3-4",
                    "distribution": "focal",
                    "architecture": architecture,
                    "ci": {"run_id": "123", "workflow": "ci", "workflow_ref": "ci.yml@main"},
                    "debs": [entry],
                }
                (artifact / f"{architecture}.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
            args = SimpleNamespace(
                plan=str(plan_path),
                product="xgc2-test",
                artifact_dir=str(artifact_root),
                run_id=123,
                release_id="release-123",
                release_lock_digest="b" * 64,
                published_at="2026-07-10T00:00:00Z",
                output_dir=str(root / "bundles"),
                receipt=str(receipt),
            )
            with mock.patch.object(
                stage_tool, "deb_metadata", side_effect=self.fake_deb_metadata
            ):
                self.assertEqual(stage_tool.prepare(args), 0)
                first = json.loads(receipt.read_text(encoding="utf-8"))
                first_digest = first["products"][0]["bundle_digest"]
                self.assertEqual(stage_tool.prepare(args), 0)
                second = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(first_digest, second["products"][0]["bundle_digest"])
            self.assertEqual(
                ["amd64", "arm64"],
                [entry["architecture"] for entry in second["products"][0]["debs"]],
            )
            release_manifests = list((root / "bundles").rglob("manifests/**/*.json"))
            self.assertEqual(len(release_manifests), 2)
            train_path = root / "release-train.json"
            train_args = SimpleNamespace(
                plan=str(plan_path),
                receipt_dir=str(receipt_dir),
                release_id="release-123",
                release_lock_digest="b" * 64,
                plan_digest=stage_tool.canonical_digest(plan),
                output=str(train_path),
            )
            self.assertEqual(stage_tool.create_train(train_args), 0)
            train = json.loads(train_path.read_text(encoding="utf-8"))
            self.assertEqual(train["schema"], "xgc2.release-train.v1")
            self.assertEqual(len(train["products"]), 1)
            self.assertNotIn("bundle_dir", train["products"][0])

    def test_release_train_rejects_missing_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {"layers": [[{"id": "x", "action": "release"}]]}
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            (root / "receipts").mkdir()
            with self.assertRaisesRegex(ValueError, "receipt set"):
                stage_tool.create_train(
                    SimpleNamespace(
                        plan=str(plan_path),
                        receipt_dir=str(root / "receipts"),
                        release_id="release-1",
                        release_lock_digest="b" * 64,
                        plan_digest="",
                        output=str(root / "train.json"),
                    )
                )

    def test_release_train_rejects_same_package_identity_with_different_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {
                "layers": [[{
                    "id": "x", "action": "release", "expected_version": "1-1",
                    "expected_source_sha": "a" * 40, "apt_distributions": ["focal"],
                }]]
            }
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            receipt_dir = root / "receipts"
            receipt_dir.mkdir()
            (receipt_dir / "x.json").write_text(json.dumps({
                "schema": "xgc2.stage-receipt.v1", "release_id": "r",
                "release_lock_digest": "b" * 64, "product": "x", "run_id": 1,
                "products": [{
                    "product": "x", "distribution": "focal", "version": "1-1",
                    "source_sha": "a" * 40, "bundle_digest": "c" * 64,
                    "build_manifest_digests": ["d" * 64],
                    "debs": [
                        {"package": "x", "version": "1-1", "architecture": "amd64", "sha256": "e" * 64},
                        {"package": "x", "version": "1-1", "architecture": "amd64", "sha256": "f" * 64},
                    ],
                }],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "different SHA256"):
                stage_tool.create_train(SimpleNamespace(
                    plan=str(plan_path), receipt_dir=str(receipt_dir), release_id="r",
                    release_lock_digest="b" * 64, plan_digest="", output=str(root / "train.json"),
                ))

    def test_stage_timeout_is_confirmed_by_digest_bound_status(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "payload").write_bytes(b"trusted")
            digest = publisher.tree_digest(bundle)
            timeout = mock.Mock(returncode=124, stdout=b"", stderr=b"timeout")
            status = mock.Mock(
                returncode=0,
                stdout=json.dumps({
                    "status": "prepared",
                    "bundles": {digest: {
                        "manifests": [{"path": "x.json", "sha256": "c" * 64}],
                        "product": {"distribution": "focal"},
                    }},
                    "distributions": {"focal": {"published": True}},
                }).encode(),
                stderr=b"",
            )
            with mock.patch.object(
                publisher, "run_remote", side_effect=[timeout, status]
            ):
                self.assertEqual(
                    publisher.stage(
                        SimpleNamespace(
                            release_id="release-1",
                            release_lock_digest="b" * 64,
                            distribution="focal",
                            bundle=str(bundle),
                        )
                    ),
                    0,
                )

    def test_stage_transport_failure_without_published_manifests_is_transient(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "payload").write_bytes(b"trusted")
            digest = publisher.tree_digest(bundle)
            transport = mock.Mock(returncode=255, stdout=b"", stderr=b"reset")
            incomplete = mock.Mock(
                returncode=0,
                stdout=json.dumps({
                    "status": "prepared",
                    "bundles": {digest: {"manifests": [], "product": {"distribution": "focal"}}},
                    "distributions": {"focal": {"published": True}},
                }).encode(),
                stderr=b"",
            )
            with mock.patch.object(publisher, "run_remote", side_effect=[transport, incomplete]), \
                    self.assertRaises(SystemExit) as raised:
                publisher.stage(SimpleNamespace(
                    release_id="release-1", release_lock_digest="b" * 64,
                    distribution="focal", bundle=str(bundle),
                ))
            self.assertEqual(raised.exception.code, 75)

    def test_stage_transport_failure_with_unavailable_status_is_transient(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "payload").write_bytes(b"trusted")
            transport = mock.Mock(returncode=255, stdout=b"", stderr=b"reset")
            status_unavailable = mock.Mock(
                returncode=255, stdout=b"", stderr=b"still unavailable"
            )
            with mock.patch.object(
                publisher, "run_remote", side_effect=[transport, status_unavailable]
            ), self.assertRaises(SystemExit) as raised:
                publisher.stage(SimpleNamespace(
                    release_id="release-1", release_lock_digest="b" * 64,
                    distribution="focal", bundle=str(bundle),
                ))
            self.assertEqual(raised.exception.code, 75)

    def test_unconfirmed_promote_timeout_becomes_transient_exit_75(self):
        with tempfile.TemporaryDirectory() as directory:
            train = Path(directory) / "train.json"
            train.write_text(
                json.dumps({
                    "schema": "xgc2.release-train.v1",
                    "release_id": "release-1",
                    "release_lock_digest": "b" * 64,
                    "plan_digest": "c" * 64,
                    "products": [],
                }),
                encoding="utf-8",
            )
            timeout = mock.Mock(returncode=124, stdout=b"", stderr=b"timeout")
            status = mock.Mock(
                returncode=0,
                stdout=json.dumps({"status": "promoting", "train_digest": ""}).encode(),
                stderr=b"",
            )
            with mock.patch.object(
                publisher, "run_remote", side_effect=[timeout, status]
            ), self.assertRaises(SystemExit) as raised:
                publisher.promote(
                    SimpleNamespace(
                        release_id="release-1",
                        release_lock_digest="b" * 64,
                        train=str(train),
                    )
                )
            self.assertEqual(raised.exception.code, 75)

    def test_staged_checkpoint_requires_http_visibility_before_fast_pass(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                "GH_TOKEN": "token", "XGC2_RELEASE_ID": "release-1",
                "XGC2_RELEASE_LOCK_DIGEST": "b" * 64,
                "XGC2_EXECUTION_POLICY_DIGEST": "c" * 64,
            },
            clear=False,
        ):
            root = Path(directory)
            product = {
                "id": "x", "action": "release", "expected_source_sha": "a" * 40,
                "expected_version": "1-1", "apt_versions": {"focal": "1-1"},
                "apt_distributions": ["focal"], "apt_packages": ["x"],
                "apt_install": ["x"], "dependency_set_digest": "d" * 64,
            }
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps({"layers": [[product]]}), encoding="utf-8")
            runner.write_node_checkpoint(plan_path, product, phase="staged", run_id=5)
            receipt_dir = root / "release-stage-receipts"
            receipt_dir.mkdir()
            receipt_product = {
                "product": "x", "distribution": "focal", "version": "1-1",
                "source_sha": "a" * 40, "bundle_digest": "e" * 64,
                "build_manifest_digests": ["f" * 64],
                "debs": [{
                    "package": "x", "version": "1-1", "architecture": "all",
                    "sha256": "1" * 64,
                }],
            }
            (receipt_dir / "x.json").write_text(
                json.dumps({"products": [receipt_product]}), encoding="utf-8"
            )
            status_product = dict(receipt_product)
            status = {
                "status": "prepared",
                "bundles": {"e" * 64: {
                    "product": status_product, "manifests": [{"path": "x"}],
                }},
                "distributions": {"focal": {"published": True}},
            }
            args = SimpleNamespace(
                plan=str(plan_path), product="x", apt_arch=[], verify_production=False,
                verify_lock_only=False, reconcile_ci=False, apt_overlay_url="",
                apt_base_url="https://apt.example",
                manifest_base_url="https://apt.example/manifests",
                apt_timeout_seconds=1, poll_seconds=0, reuse_ci_artifacts=True,
                ci_wait_seconds=0, timeout_seconds=1, quality_required=False,
                source_tests=False,
            )
            completed = mock.Mock(returncode=0, stdout=json.dumps(status), stderr="")
            with mock.patch.object(runner, "subprocess_checked", return_value=completed), \
                    mock.patch.object(runner, "verify_apt") as visibility:
                self.assertEqual(runner.execute_central(args), 0)
            visibility.assert_called_once()
            self.assertEqual(
                visibility.call_args.kwargs["apt_base_url"],
                "https://apt.example/staging/release-1",
            )

    def test_local_staged_checkpoint_accepts_exact_promoting_server_state(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                "GH_TOKEN": "token", "XGC2_RELEASE_ID": "release-1",
                "XGC2_RELEASE_LOCK_DIGEST": "b" * 64,
                "XGC2_EXECUTION_POLICY_DIGEST": "c" * 64,
            },
            clear=False,
        ):
            root = Path(directory)
            product = self.resumable_product()
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps({"layers": [[product]]}), encoding="utf-8")
            runner.write_node_checkpoint(plan_path, product, phase="staged", run_id=5)
            receipt_dir = root / "release-stage-receipts"
            receipt_dir.mkdir()
            staged_product = self.staged_product()
            (receipt_dir / "x.json").write_text(
                json.dumps({"products": [staged_product]}), encoding="utf-8"
            )
            completed = mock.Mock(
                returncode=0,
                stdout=json.dumps(self.server_stage_status(staged_product)),
                stderr="",
            )
            with mock.patch.object(
                runner, "subprocess_checked", return_value=completed
            ), mock.patch.object(runner, "verify_apt") as visibility:
                self.assertEqual(runner.execute_central(self.resumable_args(plan_path)), 0)
            visibility.assert_called_once()

    def test_durable_recovery_without_local_checkpoint_accepts_promoting(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                "GH_TOKEN": "token", "XGC2_RELEASE_ID": "release-1",
                "XGC2_RELEASE_LOCK_DIGEST": "b" * 64,
                "XGC2_EXECUTION_POLICY_DIGEST": "c" * 64,
            },
            clear=False,
        ):
            root = Path(directory)
            product = self.resumable_product()
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps({"layers": [[product]]}), encoding="utf-8")
            staged_product = self.staged_product()
            completed = mock.Mock(
                returncode=0,
                stdout=json.dumps(self.server_stage_status(staged_product)),
                stderr="",
            )
            with mock.patch.object(
                runner, "run", return_value=completed
            ), mock.patch.object(runner, "verify_apt") as visibility:
                self.assertEqual(runner.execute_central(self.resumable_args(plan_path)), 0)
            visibility.assert_called_once()
            checkpoint = runner.load_node_checkpoint(plan_path, product)
            self.assertEqual(checkpoint["phase"], "staged")
            self.assertEqual(checkpoint["artifact_source"], "server-recovered")

    def test_local_staged_checkpoint_restages_exact_run_when_server_bundle_is_absent(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                "GH_TOKEN": "token", "XGC2_RELEASE_ID": "release-1",
                "XGC2_RELEASE_LOCK_DIGEST": "b" * 64,
                "XGC2_EXECUTION_POLICY_DIGEST": "c" * 64,
            },
            clear=False,
        ):
            root = Path(directory)
            product = self.resumable_product()
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps({"layers": [[product]]}), encoding="utf-8")
            runner.write_node_checkpoint(plan_path, product, phase="staged", run_id=5)
            receipt_dir = root / "release-stage-receipts"
            receipt_dir.mkdir()
            expected = self.staged_product()
            expected["bundle_dir"] = str(root / "bundle-focal")
            (receipt_dir / "x.json").write_text(
                json.dumps({"products": [expected]}), encoding="utf-8"
            )
            wrong = self.staged_product("9" * 64)
            completed = mock.Mock(
                returncode=0,
                stdout=json.dumps(self.server_stage_status(wrong)),
                stderr="",
            )
            # The exact run is immutable and can be downloaded/prepared again.
            (root / "release-run-artifacts" / "x" / "5").mkdir(parents=True)
            receipt = root / "release-stage-receipts" / "x.json"
            receipt.write_text(
                json.dumps({"products": [expected]}), encoding="utf-8"
            )
            ok = mock.Mock(returncode=0, stdout="{}", stderr="")
            with mock.patch.object(
                runner, "subprocess_checked", side_effect=[completed, ok, ok]
            ) as command, mock.patch.object(
                runner, "verify_release_lock_is_current"
            ), mock.patch.object(
                runner, "download_run_artifacts"
            ), mock.patch.object(runner, "verify_apt") as visibility:
                self.assertEqual(runner.execute_central(self.resumable_args(plan_path)), 0)

            self.assertEqual(command.call_count, 3)
            visibility.assert_called_once()
            checkpoint = runner.load_node_checkpoint(plan_path, product)
            self.assertEqual(checkpoint["phase"], "staged")
            self.assertEqual(checkpoint["run_id"], 5)
            self.assertEqual(checkpoint["artifact_source"], "fallback")

    def test_stale_server_recovered_checkpoint_falls_back_to_trusted_ci(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                "GH_TOKEN": "token", "XGC2_RELEASE_ID": "release-1",
                "XGC2_RELEASE_LOCK_DIGEST": "b" * 64,
                "XGC2_EXECUTION_POLICY_DIGEST": "c" * 64,
            },
            clear=False,
        ):
            root = Path(directory)
            product = self.resumable_product()
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps({"layers": [[product]]}), encoding="utf-8")
            runner.write_node_checkpoint(
                plan_path,
                product,
                phase="staged",
                artifact_source="server-recovered",
            )
            receipt_dir = root / "release-stage-receipts"
            receipt_dir.mkdir()
            expected = self.staged_product()
            expected["bundle_dir"] = str(root / "bundle-focal")
            (receipt_dir / "x.json").write_text(
                json.dumps({"products": [expected]}), encoding="utf-8"
            )
            (root / "release-run-artifacts" / "x" / "77").mkdir(parents=True)
            absent = mock.Mock(
                returncode=0,
                stdout=json.dumps({
                    "status": "prepared", "bundles": {},
                    "distributions": {"focal": {"published": True}},
                }),
                stderr="",
            )
            ok = mock.Mock(returncode=0, stdout="{}", stderr="")
            with mock.patch.object(
                runner, "subprocess_checked", side_effect=[absent, ok, ok]
            ), mock.patch.object(
                runner, "verify_release_lock_is_current"
            ), mock.patch.object(
                runner, "find_trusted_ci_run", return_value=77
            ) as trusted, mock.patch.object(runner, "verify_apt"):
                self.assertEqual(runner.execute_central(self.resumable_args(plan_path)), 0)

            trusted.assert_called_once()
            checkpoint = runner.load_node_checkpoint(plan_path, product)
            self.assertEqual(checkpoint["phase"], "staged")
            self.assertEqual(checkpoint["run_id"], 77)
            self.assertEqual(checkpoint["artifact_source"], "push-ci")


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


class CiReconciliationTests(unittest.TestCase):
    def test_failed_exact_source_push_ci_is_rerun_once_and_checkpointed(self):
        product = {
            "id": "x", "repository": "example/x", "ref": "main",
            "ci_workflow": "ci.yml", "expected_source_sha": "a" * 40,
        }
        failed = {
            "databaseId": 55, "status": "completed", "conclusion": "failure",
            "headSha": "a" * 40, "event": "push",
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            runner, "latest_push_ci_run", return_value=failed
        ), mock.patch.object(
            runner, "run_attempt", return_value={"run_attempt": 1}
        ), mock.patch.object(
            runner, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
        ) as command, mock.patch.object(
            runner, "wait_for_new_attempt", return_value={"conclusion": "success"}
        ), mock.patch.object(runner, "emit_result"):
            plan_path = Path(directory) / "plan.json"
            plan_path.write_text("{}", encoding="utf-8")
            self.assertEqual(
                runner.reconcile_push_ci(
                    SimpleNamespace(timeout_seconds=10, poll_seconds=0), product, plan_path
                ),
                0,
            )
            checkpoint = runner.load_ci_reconciliation_checkpoint(plan_path, product)
        self.assertEqual(checkpoint["phase"], "green")
        self.assertIn("rerun", command.call_args.args[0])


class ReleasePlanValidationTests(unittest.TestCase):
    def test_ros_package_variants_preserve_shell_placeholder_forms(self):
        self.assertEqual(
            plan_validator.package_variants("ros-noetic-xgc2-provider"),
            [
                ("ros-noetic-xgc2-provider", "focal"),
                ("ros-${ROS_DISTRO}-xgc2-provider", "focal"),
                (r"ros-\${ROS_DISTRO}-xgc2-provider", "focal"),
            ],
        )
        self.assertEqual(
            plan_validator.package_variants("ros-melodic-xgc2-provider"),
            [
                ("ros-melodic-xgc2-provider", "bionic"),
                ("ros-${ROS_DISTRO}-xgc2-provider", "bionic"),
                (r"ros-\${ROS_DISTRO}-xgc2-provider", "bionic"),
            ],
        )

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
    def write_consumer(
        root: Path,
        *,
        requires: bool,
        recommends: bool = False,
    ) -> Path:
        source = root / "consumer"
        scripts = source / ".xgc2" / "scripts"
        scripts.mkdir(parents=True)
        requirement = "\n  requires:\n  - xgc2-provider" if requires else ""
        recommendation = (
            "  recommends:\n  - ros-noetic-xgc2-provider (>= 2.0.0-1)"
            if recommends
            else "  recommends: []"
        )
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
                    recommendation,
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
            any(
                "apt.depends/apt.recommends/release.requires" in error
                for error in errors
            ),
            errors,
        )

    def test_release_requires_satisfies_installation_order_constraint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_consumer(root, requires=True)
            plan = {"layers": [[self.plan_item(source.relative_to(root).as_posix())]]}
            errors = plan_validator.validate(root, plan, catalog=self.catalog())

        self.assertEqual(errors, [])

    def test_apt_recommends_satisfies_declared_package_constraint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_consumer(root, requires=False, recommends=True)
            (source / ".xgc2" / "scripts" / "build_deb.sh").write_text(
                "Recommends: ros-noetic-xgc2-provider (>= 2.0.0-1)\n",
                encoding="utf-8",
            )
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

    def test_hidden_dependency_is_checked_for_compatibility_build_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_consumer(root, requires=False)
            item = self.plan_item(source.relative_to(root).as_posix())
            item["action"] = "compatibility-verify"
            errors = plan_validator.validate(
                root, {"layers": [[item]]}, catalog=self.catalog()
            )
        self.assertTrue(
            any(
                "apt.depends/apt.recommends/release.requires" in error
                for error in errors
            )
        )

    def test_owned_package_compatible_minimum_may_precede_current_catalog_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_consumer(root, requires=True)
            (source / ".xgc2" / "scripts" / "build_deb.sh").write_text(
                "Depends: ros-noetic-xgc2-provider (>= 1.0.0-1)\n",
                encoding="utf-8",
            )
            plan = {"layers": [[self.plan_item(source.relative_to(root).as_posix())]]}
            errors = plan_validator.validate(root, plan, catalog=self.catalog())

        self.assertEqual(errors, [])

    def test_owned_package_minimum_above_provider_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_consumer(root, requires=True)
            (source / ".xgc2" / "scripts" / "build_deb.sh").write_text(
                "Depends: ros-noetic-xgc2-provider (>= 3.0.0-1)\n",
                encoding="utf-8",
            )
            plan = {"layers": [[self.plan_item(source.relative_to(root).as_posix())]]}
            errors = plan_validator.validate(root, plan, catalog=self.catalog())

        self.assertTrue(
            any(">= 3.0.0-1" in error and "does not satisfy" in error for error in errors),
            errors,
        )

    def test_owned_package_exact_relation_remains_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_consumer(root, requires=True)
            (source / ".xgc2" / "scripts" / "build_deb.sh").write_text(
                "Depends: ros-noetic-xgc2-provider (= 1.0.0-1)\n",
                encoding="utf-8",
            )
            plan = {"layers": [[self.plan_item(source.relative_to(root).as_posix())]]}
            errors = plan_validator.validate(root, plan, catalog=self.catalog())

        self.assertTrue(
            any("= 1.0.0-1" in error and "does not satisfy" in error for error in errors),
            errors,
        )

    def test_owned_package_hard_install_pin_remains_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_consumer(root, requires=True)
            (source / ".xgc2" / "scripts" / "install_deps.sh").write_text(
                "apt-get install -y ros-noetic-xgc2-provider=1.0.0-1\n",
                encoding="utf-8",
            )
            plan = {"layers": [[self.plan_item(source.relative_to(root).as_posix())]]}
            errors = plan_validator.validate(root, plan, catalog=self.catalog())

        self.assertTrue(
            any("hard-coded install/compliance version" in error for error in errors),
            errors,
        )

    def test_owned_package_version_through_shell_variable_is_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_consumer(root, requires=True)
            (source / ".xgc2" / "scripts" / "install_deps.sh").write_text(
                'provider="ros-noetic-xgc2-provider"\n'
                'apt-get install -y "${provider} (>= 1.0.0-1)"\n',
                encoding="utf-8",
            )
            plan = {"layers": [[self.plan_item(source.relative_to(root).as_posix())]]}
            errors = plan_validator.validate(root, plan, catalog=self.catalog())

        self.assertEqual(errors, [])

    def test_dpkg_compare_threshold_for_release_requires_is_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_consumer(root, requires=True)
            (source / ".xgc2" / "scripts" / "install_deps.sh").write_text(
                'dpkg --compare-versions "$(dpkg-query -W ros-noetic-xgc2-provider)" '
                "ge '1.0.0-1'\n",
                encoding="utf-8",
            )
            plan = {"layers": [[self.plan_item(source.relative_to(root).as_posix())]]}
            errors = plan_validator.validate(root, plan, catalog=self.catalog())

        self.assertEqual(errors, [])

    def test_release_set_external_version_is_a_compatible_minimum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_consumer(root, requires=True)
            (source / ".xgc2" / "release-set.yml").write_text(
                "packages:\n"
                "  provider:\n"
                "    apt: ros-noetic-xgc2-provider\n"
                "    version: 1.0.0-1\n",
                encoding="utf-8",
            )
            plan = {"layers": [[self.plan_item(source.relative_to(root).as_posix())]]}
            errors = plan_validator.validate(root, plan, catalog=self.catalog())

        self.assertEqual(errors, [])

    def test_release_set_minimum_above_provider_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_consumer(root, requires=True)
            (source / ".xgc2" / "release-set.yml").write_text(
                "packages:\n"
                "  provider:\n"
                "    apt: ros-noetic-xgc2-provider\n"
                "    version: 3.0.0-1\n",
                encoding="utf-8",
            )
            plan = {"layers": [[self.plan_item(source.relative_to(root).as_posix())]]}
            errors = plan_validator.validate(root, plan, catalog=self.catalog())

        self.assertTrue(
            any("compatible minimum 3.0.0-1" in error for error in errors),
            errors,
        )


class VersionBumpSafetyTests(unittest.TestCase):
    def test_dependency_minimum_updates_are_cli_opt_in(self):
        defaults = version_bumper.parse_args(["--plan", "plan.json"])
        legacy_skip = version_bumper.parse_args(
            ["--plan", "plan.json", "--skip-dependency-updates"]
        )
        explicit_update = version_bumper.parse_args(
            ["--plan", "plan.json", "--update-dependency-minimums"]
        )

        self.assertFalse(defaults.update_dependency_minimums)
        self.assertFalse(legacy_skip.update_dependency_minimums)
        self.assertTrue(explicit_update.update_dependency_minimums)

    def test_default_behavior_preserves_compatible_dependency_minimums(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "consumer"
            (source / ".xgc2").mkdir(parents=True)
            product = source / ".xgc2" / "product.yml"
            release_set = source / ".xgc2" / "release-set.yml"
            product.write_text(
                "schema: xgc2.product.v1\n"
                "id: consumer\n"
                "name: Consumer\n"
                "kind: ros1-apt\n"
                "version: 1.0.0-1\n"
                "apt:\n"
                "  packages: [consumer-deb]\n"
                "  depends: [provider-deb (>= 1.0.0-1)]\n"
                "  recommends: [provider-deb (>= 1.0.0-1)]\n",
                encoding="utf-8",
            )
            release_set.write_text(
                "packages:\n"
                "  provider:\n"
                "    apt: provider-deb\n"
                "    version: 1.0.0-1\n",
                encoding="utf-8",
            )
            original_product = product.read_bytes()
            original_release_set = release_set.read_bytes()

            changed = version_bumper.update_product_metadata(
                root,
                {
                    "id": "consumer",
                    "action": "release",
                    "source": "consumer",
                    "version": "1.0.0-1",
                    "expected_version": "1.0.0-1",
                },
                owner_versions={"provider-deb": "1.0.0-2"},
                update_dependencies=False,
                apply=True,
            )
            rewritten_product = product.read_bytes()
            rewritten_release_set = release_set.read_bytes()

        self.assertEqual(changed, set())
        self.assertEqual(rewritten_product, original_product)
        self.assertEqual(rewritten_release_set, original_release_set)

    def test_explicit_opt_in_updates_all_compatible_dependency_minimums(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "consumer"
            (source / ".xgc2").mkdir(parents=True)
            (source / ".xgc2" / "product.yml").write_text(
                "schema: xgc2.product.v1\n"
                "id: consumer\n"
                "name: Consumer\n"
                "kind: ros1-apt\n"
                "version: 1.0.0-1\n"
                "apt:\n"
                "  packages: [consumer-deb]\n"
                "  depends: [provider-deb (>= 1.0.0-1)]\n"
                "  recommends: [provider-deb (>= 1.0.0-1)]\n",
                encoding="utf-8",
            )
            (source / ".xgc2" / "release-set.yml").write_text(
                "packages:\n"
                "  provider:\n"
                "    apt: provider-deb\n"
                "    version: 1.0.0-1\n",
                encoding="utf-8",
            )

            changed = version_bumper.update_product_metadata(
                root,
                {
                    "id": "consumer",
                    "action": "release",
                    "source": "consumer",
                    "version": "1.0.0-1",
                    "expected_version": "1.0.0-1",
                },
                owner_versions={"provider-deb": "1.0.0-2"},
                update_dependencies=True,
                apply=True,
            )
            metadata = version_bumper.load_yaml(source / ".xgc2" / "product.yml")
            release_set = version_bumper.load_yaml(source / ".xgc2" / "release-set.yml")

        self.assertIn(".xgc2/product.yml", changed)
        self.assertIn(".xgc2/release-set.yml", changed)
        self.assertEqual(metadata["apt"]["depends"], ["provider-deb (>= 1.0.0-2)"])
        self.assertEqual(
            metadata["apt"]["recommends"],
            ["provider-deb (>= 1.0.0-2)"],
        )
        self.assertEqual(release_set["packages"]["provider"]["version"], "1.0.0-2")

    def test_push_transaction_is_lease_guarded_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote.git"
            work = root / "work"
            version_bumper.run(["git", "init", "--bare", str(remote)])
            version_bumper.run(["git", "init", str(work)])
            version_bumper.run(["git", "checkout", "-b", "main"], cwd=work)
            version_bumper.run(["git", "config", "user.name", "test"], cwd=work)
            version_bumper.run(["git", "config", "user.email", "test@example.com"], cwd=work)
            (work / "value").write_text("base\n", encoding="utf-8")
            version_bumper.run(["git", "add", "value"], cwd=work)
            version_bumper.run(["git", "commit", "-m", "base"], cwd=work)
            base = version_bumper.git(["rev-parse", "HEAD"], work, check=True)
            version_bumper.run(["git", "remote", "add", "origin", str(remote)], cwd=work)
            version_bumper.run(["git", "push", "-u", "origin", "main"], cwd=work)
            (work / "value").write_text("target\n", encoding="utf-8")
            version_bumper.run(["git", "commit", "-am", "target"], cwd=work)
            target = version_bumper.git(["rev-parse", "HEAD"], work, check=True)
            entry = {
                "repository": "example/product", "ref": "main", "path": "product",
                "base": base, "target": target,
                "tree": version_bumper.git(["rev-parse", "HEAD^{tree}"], work, check=True),
                "status": "committed", "initial_plan_digest": "a" * 64,
                "final_plan_digest": "b" * 64, "_repo_path": str(work),
            }
            transaction = root / "transaction.json"
            version_bumper.push_transaction([entry], transaction)
            self.assertEqual(version_bumper.remote_ref_head(work, "main"), target)
            self.assertEqual(json.loads(transaction.read_text())["entries"][0]["status"], "pushed")
            version_bumper.push_transaction([entry], transaction)
            self.assertEqual(
                json.loads(transaction.read_text())["entries"][0]["status"],
                "already-pushed",
            )

    def test_script_dependency_relation_is_replaced_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            scripts = source / ".xgc2" / "scripts"
            scripts.mkdir(parents=True)
            path = scripts / "package_debs.sh"
            path.write_text(
                "Depends: xgc2-fs150-description (>= 0.1.0-3) (>= 0.1.0-1)\n",
                encoding="utf-8",
            )
            version_bumper.update_script_dependency_versions(
                source,
                ["xgc2-fs150-description"],
                ["xgc2-fs150-description (>= 0.1.0-4)"],
                apply=True,
            )
            text = path.read_text(encoding="utf-8")
        self.assertIn("xgc2-fs150-description (>= 0.1.0-4)", text)
        self.assertNotIn(") (", text)

    def test_unchanged_bare_dependency_preserves_stronger_script_relation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            scripts = source / ".xgc2" / "scripts"
            scripts.mkdir(parents=True)
            path = scripts / "build_deb.sh"
            path.write_text(
                'dependency = "libasound2 (>= 1.0.16)"\n'
                'fallback = "libasound2 (>= 1.0.16)"\n',
                encoding="utf-8",
            )
            original = path.read_bytes()
            changed = version_bumper.update_script_dependency_versions(
                source,
                ["libasound2"],
                ["libasound2"],
                apply=True,
            )
            rewritten = path.read_bytes()
        self.assertEqual(changed, set())
        self.assertEqual(rewritten, original)

    def test_distribution_specific_alternative_dependency_is_byte_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            scripts = source / ".xgc2" / "scripts"
            scripts.mkdir(parents=True)
            path = scripts / "build_deb.sh"
            path.write_text(
                'gcc_runtime_dep="libgcc-s1"\n'
                'if [[ "${package_distribution}" == "bionic" ]]; then\n'
                '  gcc_runtime_dep="libgcc1"\n'
                "fi\n"
                'base_depends="libeigen3-dev, libc6, ${gcc_runtime_dep}, libstdc++6"\n',
                encoding="utf-8",
            )
            original = path.read_bytes()
            dependency = "libgcc1 | libgcc-s1"
            for _attempt in range(4):
                changed = version_bumper.update_script_dependency_versions(
                    source,
                    [dependency],
                    [dependency],
                    apply=True,
                )
                self.assertEqual(changed, set())
                self.assertEqual(path.read_bytes(), original)
            text = path.read_text(encoding="utf-8")
        self.assertIn('gcc_runtime_dep="libgcc1"', text)
        self.assertNotIn("libgcc1 | libgcc-s1", text)

    def test_build_script_repairs_polluted_bare_args_and_updates_compare_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            scripts = source / ".xgc2" / "scripts"
            scripts.mkdir(parents=True)
            path = scripts / "build_debs_in_docker.sh"
            path.write_text(
                "apt-get install -y libxgc2-math-dev (>= 0.5.6-6~focal)\\\n\n"
                'dpkg --compare-versions "$(dpkg-query -W libxgc2-math-dev (>= 0.5.6-6~focal))" ge \'0.5.6-5~focal\'\n'
                'echo Depends | grep -F "libxgc2-math-dev (>= 0.5.6-5~focal)"\n',
                encoding="utf-8",
            )
            current = "libxgc2-math-dev (>= 0.5.6-6~focal)"
            version_bumper.update_script_dependency_versions(
                source, [current], [current], apply=True
            )
            text = path.read_text(encoding="utf-8")
        self.assertIn("apt-get install -y libxgc2-math-dev", text)
        self.assertNotIn("dpkg-query -W libxgc2-math-dev (>=", text)
        self.assertIn("ge '0.5.6-6~focal'", text)
        self.assertIn('grep -F "libxgc2-math-dev (>= 0.5.6-6~focal)"', text)

    def test_package_name_variable_stays_bare_and_use_relation_is_updated(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            scripts = source / ".xgc2" / "scripts"
            scripts.mkdir(parents=True)
            path = scripts / "package_debs.sh"
            path.write_text(
                'worlds_pkg="ros-noetic-xgc2-gazebo-worlds"\n'
                'depends="${worlds_pkg} (>= 1.1.0-9)"\n',
                encoding="utf-8",
            )
            version_bumper.update_script_dependency_versions(
                source,
                ["ros-noetic-xgc2-gazebo-worlds"],
                ["ros-noetic-xgc2-gazebo-worlds (>= 1.1.0-10)"],
                apply=True,
            )
            text = path.read_text(encoding="utf-8")
        self.assertIn('worlds_pkg="ros-noetic-xgc2-gazebo-worlds"', text)
        self.assertIn('${worlds_pkg} (>= 1.1.0-10)', text)
        self.assertNotIn('worlds_pkg="ros-noetic-xgc2-gazebo-worlds (>=', text)

    def test_dep_variable_contains_relation_while_pkg_variable_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            scripts = source / ".xgc2" / "scripts"
            scripts.mkdir(parents=True)
            package_script = scripts / "package_debs.sh"
            compliance = scripts / "check_package_compliance.sh"
            package_script.write_text(
                'visualization_dep="ros-noetic-xgc2-visualization"\n'
                'msgs_pkg="ros-noetic-xgc2-msgs (>= 1.0.0-1)"\n'
                'depends="${visualization_dep} (>= 1.0.0-1), ${msgs_pkg} (>= 1.0.0-1)"\n',
                encoding="utf-8",
            )
            compliance.write_text(
                'grep -Fq \'visualization_dep="ros-noetic-xgc2-visualization (>= 1.0.0-1)"\' package_debs.sh\n'
                'grep -Fq \'msgs_pkg="ros-noetic-xgc2-msgs (>= 1.0.0-1)"\' package_debs.sh\n'
                "grep -Fq '${visualization_dep} (= ${VERSION})' package_debs.sh\n"
                "grep -Fq '${msgs_pkg} (= ${VERSION})' package_debs.sh\n",
                encoding="utf-8",
            )
            version_bumper.update_script_dependency_versions(
                source,
                ["ros-noetic-xgc2-visualization", "ros-noetic-xgc2-msgs"],
                [
                    "ros-noetic-xgc2-visualization (>= 1.0.0-2)",
                    "ros-noetic-xgc2-msgs (>= 1.0.0-2)",
                ],
                apply=True,
            )
            package_text = package_script.read_text(encoding="utf-8")
            compliance_text = compliance.read_text(encoding="utf-8")
        self.assertIn(
            'visualization_dep="ros-noetic-xgc2-visualization (>= 1.0.0-2)"',
            package_text,
        )
        self.assertIn('msgs_pkg="ros-noetic-xgc2-msgs"', package_text)
        self.assertIn('${visualization_dep}, ${msgs_pkg} (>= 1.0.0-2)', package_text)
        self.assertIn(
            'visualization_dep="ros-noetic-xgc2-visualization (>= 1.0.0-2)"',
            compliance_text,
        )
        self.assertIn('msgs_pkg="ros-noetic-xgc2-msgs"', compliance_text)
        self.assertIn('${visualization_dep}', compliance_text)
        self.assertNotIn('${visualization_dep} (>=', compliance_text)
        self.assertIn('${msgs_pkg} (>= 1.0.0-2)', compliance_text)

    def test_px4_runtime_debian_version_tracks_planned_product_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "px4"
            (source / ".xgc2").mkdir(parents=True)
            (source / "manifest").mkdir()
            (source / ".xgc2" / "product.yml").write_text(
                "schema: xgc2.product.v1\nid: px4\nname: PX4\nkind: apt\nversion: 1.16.2-7\napt:\n  distribution: focal\n  packages: [px4]\n",
                encoding="utf-8",
            )
            (source / "manifest" / "px4_runtime.yaml").write_text(
                "schema: px4.runtime.v1\ndebian_version: 1.16.2-1\n",
                encoding="utf-8",
            )
            changed = version_bumper.update_product_metadata(
                root,
                {
                    "id": "px4", "action": "release", "source": "px4",
                    "version": "1.16.2-7", "expected_version": "1.16.2-8",
                    "apt_versions": {"focal": "1.16.2-8"},
                },
                owner_versions={}, update_dependencies=False, apply=True,
            )
            runtime = version_bumper.load_yaml(source / "manifest" / "px4_runtime.yaml")
        self.assertIn("manifest/px4_runtime.yaml", changed)
        self.assertEqual(runtime["debian_version"], "1.16.2-8")

    def test_single_distribution_apt_version_tracks_planned_product_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "camera-calibration"
            (source / ".xgc2").mkdir(parents=True)
            (source / ".xgc2" / "scripts").mkdir()
            (source / ".xgc2" / "product.yml").write_text(
                "schema: xgc2.product.v1\nid: camera-calibration\nname: Camera Calibration\n"
                "kind: ros1-apt\nversion: 0.3.0-2\napt:\n  distribution: focal\n"
                "  packages: [ros-noetic-camera-calibration]\nrelease:\n"
                "  apt_versions:\n    focal: 0.3.0-2\n",
                encoding="utf-8",
            )
            compliance = source / ".xgc2" / "scripts" / "check_package_compliance.sh"
            compliance.write_text(
                "grep -q '^version: 0.3.0-2$' .xgc2/product.yml\n"
                "grep -q '^    focal: 0.3.0-2$' .xgc2/product.yml\n",
                encoding="utf-8",
            )
            changed = version_bumper.update_product_metadata(
                root,
                {
                    "id": "camera-calibration",
                    "action": "release",
                    "source": "camera-calibration",
                    "version": "0.3.0-2",
                    "expected_version": "0.3.0-3",
                    "apt_versions": {"focal": "0.3.0-3"},
                },
                owner_versions={}, update_dependencies=False, apply=True,
            )
            metadata = version_bumper.load_yaml(source / ".xgc2" / "product.yml")
            compliance_text = compliance.read_text(encoding="utf-8")
        self.assertIn(".xgc2/product.yml", changed)
        self.assertIn(".xgc2/scripts/check_package_compliance.sh", changed)
        self.assertEqual(metadata["version"], "0.3.0-3")
        self.assertEqual(metadata["release"]["apt_versions"], {"focal": "0.3.0-3"})
        self.assertIn("^version: 0.3.0-3$", compliance_text)
        self.assertIn("^    focal: 0.3.0-3$", compliance_text)

    def test_deterministic_commit_time_is_bound_to_plan_digest(self):
        digest = "1" * 64
        self.assertEqual(
            version_bumper.deterministic_commit_environment(digest)["GIT_AUTHOR_DATE"],
            version_bumper.deterministic_commit_environment(digest)["GIT_COMMITTER_DATE"],
        )


if __name__ == "__main__":
    unittest.main()
