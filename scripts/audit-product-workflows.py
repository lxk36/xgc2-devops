#!/usr/bin/env python3
"""Audit product GitHub workflows against the XGC2 CI/release contract."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


STANDARD_RELEASE_INPUTS = {
    "expected_version",
    "expected_source_sha",
    "publish_apt",
    "run_cpp_quality",
    "run_source_tests",
}
OPTIONAL_RELEASE_BOOLEAN_INPUTS = {
    "publish_apt",
    "run_cpp_quality",
    "run_source_tests",
}
PUSH_QUALITY_GATE_JOBS = {
    "compliance",
    "cpp-quality",
    "formatting-check",
    "package-compliance",
}
BUILD_ARTIFACT_JOB_MARKERS = {
    "actions/upload-artifact",
    "build_deb.sh",
    "package_debs.sh",
    "dpkg-deb",
}
PREFERRED_RELEASE_WORKFLOWS = (
    "release.yml",
    "release.yaml",
    "build-debs.yml",
    "build-debs.yaml",
    "ci.yml",
    "ci.yaml",
)


def workflow_has_event(text: str, event: str) -> bool:
    return bool(re.search(rf"(?m)^\s*{re.escape(event)}\s*:", text))


def workflow_input_names(text: str) -> set[str]:
    names: set[str] = set()
    in_dispatch = False
    dispatch_indent = 0
    in_inputs = False
    inputs_indent = 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if in_inputs and indent <= inputs_indent:
            in_inputs = False
        if in_dispatch and indent <= dispatch_indent:
            in_dispatch = False
        if not in_dispatch and re.match(r"^\s*workflow_dispatch\s*:", line):
            in_dispatch = True
            dispatch_indent = indent
            continue
        if in_dispatch and not in_inputs and re.match(r"^\s*inputs\s*:", line):
            in_inputs = True
            inputs_indent = indent
            continue
        if in_inputs and indent == inputs_indent + 2:
            match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:", line)
            if match:
                names.add(match.group(1))
    return names


def workflow_input_defaults(text: str) -> dict[str, str]:
    defaults: dict[str, str] = {}
    in_dispatch = False
    dispatch_indent = 0
    in_inputs = False
    inputs_indent = 0
    current_input = ""
    current_input_indent = 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if current_input and indent <= current_input_indent:
            current_input = ""
        if in_inputs and indent <= inputs_indent:
            in_inputs = False
            current_input = ""
        if in_dispatch and indent <= dispatch_indent:
            in_dispatch = False
            current_input = ""
        if not in_dispatch and re.match(r"^\s*workflow_dispatch\s*:", line):
            in_dispatch = True
            dispatch_indent = indent
            continue
        if in_dispatch and not in_inputs and re.match(r"^\s*inputs\s*:", line):
            in_inputs = True
            inputs_indent = indent
            continue
        if in_inputs and indent == inputs_indent + 2:
            match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:", line)
            if match:
                current_input = match.group(1)
                current_input_indent = indent
            continue
        if current_input:
            match = re.match(r"^\s*default\s*:\s*(.+?)\s*$", line)
            if match:
                defaults[current_input] = match.group(1).strip().strip("'\"")
    return defaults


def infer_release_workflow(source_dir: Path, product: dict[str, Any]) -> Path:
    workflow_dir = source_dir / ".github" / "workflows"
    release_config = product.get("release") if isinstance(product.get("release"), dict) else {}
    configured = release_config.get("workflow")
    if configured:
        return workflow_dir / str(configured)
    for name in PREFERRED_RELEASE_WORKFLOWS:
        workflow = workflow_dir / name
        if workflow.exists() and workflow_has_event(
            workflow.read_text(encoding="utf-8", errors="ignore"), "workflow_dispatch"
        ):
            return workflow
    for workflow in sorted(workflow_dir.glob("*.y*ml")):
        if workflow_has_event(
            workflow.read_text(encoding="utf-8", errors="ignore"), "workflow_dispatch"
        ):
            return workflow
    return workflow_dir / "release.yml"


def workflow_job_blocks(text: str) -> dict[str, str]:
    jobs: dict[str, str] = {}
    lines = text.splitlines()
    in_jobs = False
    current_job = ""
    current_lines: list[str] = []
    for line in lines:
        if re.match(r"^jobs\s*:", line):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if line and not line.startswith(" "):
            break
        match = re.match(r"^  ([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*$", line)
        if match:
            if current_job:
                jobs[current_job] = "\n".join(current_lines)
            current_job = match.group(1)
            current_lines = [line]
            continue
        if current_job:
            current_lines.append(line)
    if current_job:
        jobs[current_job] = "\n".join(current_lines)
    return jobs


def workflow_pure_quality_jobs(text: str) -> set[str]:
    jobs = workflow_job_blocks(text)
    pure_quality_jobs: set[str] = set()
    for name, body in jobs.items():
        if name not in PUSH_QUALITY_GATE_JOBS:
            continue
        if any(marker in body for marker in BUILD_ARTIFACT_JOB_MARKERS):
            continue
        pure_quality_jobs.add(name)
    return pure_quality_jobs


def workflow_quality_needs(text: str, quality_jobs: set[str] | None = None) -> set[str]:
    if quality_jobs is None:
        quality_jobs = workflow_pure_quality_jobs(text)
    matches: set[str] = set()
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped.startswith("needs:"):
            index += 1
            continue

        indent = len(line) - len(line.lstrip(" "))
        value = stripped.split(":", 1)[1].strip()
        if value:
            for item in re.findall(r"[A-Za-z0-9_-]+", value):
                if item in quality_jobs:
                    matches.add(item)
            index += 1
            continue

        index += 1
        while index < len(lines):
            child = lines[index]
            child_stripped = child.strip()
            child_indent = len(child) - len(child.lstrip(" "))
            if child_stripped and child_indent <= indent:
                break
            item_match = re.match(r"^\s*-\s*([A-Za-z0-9_-]+)\s*$", child)
            if item_match and item_match.group(1) in quality_jobs:
                matches.add(item_match.group(1))
            index += 1
    return matches


def publishes_apt(text: str) -> bool:
    return any(
        token in text
        for token in (
            "publish_apt_repo.sh",
            "publish_self_hosted_apt.sh",
            "reprepro",
            "aptly publish",
        )
    )


def push_can_publish_apt(text: str) -> bool:
    if not workflow_has_event(text, "push") or not publishes_apt(text):
        return False
    if re.search(r"github\.event_name\s*==\s*'push'", text):
        return True
    guarded_dispatch = (
        "github.event_name == 'workflow_dispatch'" in text
        and "inputs.publish_apt" in text
    )
    return not guarded_dispatch


def push_runs_cpp_quality(root: Path, source_dir: Path) -> bool:
    ci_workflow = source_dir / ".github" / "workflows" / "ci.yml"
    if not ci_workflow.exists():
        ci_workflow = source_dir / ".github" / "workflows" / "ci.yaml"
    if not ci_workflow.exists():
        return False

    text = ci_workflow.read_text(encoding="utf-8", errors="ignore")
    if not workflow_has_event(text, "push"):
        return False
    return "check_cpp_quality.sh" in text


def workflow_quality_gates_other_jobs(workflow: Path) -> set[str]:
    if not workflow.exists():
        return set()

    text = workflow.read_text(encoding="utf-8", errors="ignore")
    return workflow_quality_needs(text)


def release_cpp_quality_is_gated(text: str) -> bool:
    jobs = workflow_job_blocks(text)
    body = jobs.get("cpp-quality")
    if body is None:
        return True
    return bool(
        re.search(
            r"(?m)^    if:\s*(?:\$\{\{\s*)?inputs\.run_cpp_quality\b",
            body,
        )
    )


def push_requires_version_bump(source_dir: Path) -> bool:
    ci_workflow = source_dir / ".github" / "workflows" / "ci.yml"
    if not ci_workflow.exists():
        ci_workflow = source_dir / ".github" / "workflows" / "ci.yaml"
    if not ci_workflow.exists():
        return False

    text = ci_workflow.read_text(encoding="utf-8", errors="ignore")
    return workflow_has_event(text, "push") and "check_version_bump.sh --ci" in text


def list_field(data: dict[str, Any], *path: str) -> list[str]:
    value: Any = data
    for key in path:
        if not isinstance(value, dict):
            return []
        value = value.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def load_catalog(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    products = data.get("products", [])
    if not isinstance(products, list):
        raise ValueError(f"{path}: products must be a list")
    return [item for item in products if isinstance(item, dict)]


def audit_product(root: Path, product: dict[str, Any]) -> list[dict[str, str]]:
    source = root / str(product["_source"])
    source_dir = source.parent.parent
    workflow_dir = source_dir / ".github" / "workflows"
    issues: list[dict[str, str]] = []
    if "apt" not in str(product.get("kind", "")):
        return issues

    workflows = sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml"))
    release_workflow = infer_release_workflow(source_dir, product)
    cpp_quality_script = source_dir / ".xgc2" / "scripts" / "check_cpp_quality.sh"
    if cpp_quality_script.exists() and not push_runs_cpp_quality(root, source_dir):
        issues.append(
            {
                "product": str(product["id"]),
                "severity": "error",
                "code": "push-cpp-quality-disabled",
                "path": (workflow_dir / "ci.yml").relative_to(root).as_posix(),
                "message": "push CI must run .xgc2/scripts/check_cpp_quality.sh",
            }
        )
    if push_requires_version_bump(source_dir):
        issues.append(
            {
                "product": str(product["id"]),
                "severity": "error",
                "code": "push-requires-version-bump",
                "path": (workflow_dir / "ci.yml").relative_to(root).as_posix(),
                "message": "ordinary push CI must not require product version bumps",
            }
        )
    if not release_workflow.exists():
        issues.append(
            {
                "product": str(product["id"]),
                "severity": "warn",
                "code": "missing-release-workflow",
                "path": release_workflow.relative_to(root).as_posix(),
                "message": "APT product should expose workflow_dispatch release.yml",
            }
        )

    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8", errors="ignore")
        quality_needs = workflow_quality_gates_other_jobs(workflow)
        if quality_needs:
            issues.append(
                {
                    "product": str(product["id"]),
                    "severity": "error",
                    "code": "workflow-quality-gates-build",
                    "path": workflow.relative_to(root).as_posix(),
                    "message": (
                        "pure quality/compliance jobs must run in parallel, not as another "
                        "job's needs: " + ", ".join(sorted(quality_needs))
                    ),
                }
            )
        if push_can_publish_apt(text):
            issues.append(
                {
                    "product": str(product["id"]),
                    "severity": "error",
                    "code": "push-publishes-apt",
                    "path": workflow.relative_to(root).as_posix(),
                    "message": "push-triggered workflow can publish APT",
                }
            )
        if workflow == release_workflow:
            input_names = workflow_input_names(text)
            missing = sorted(STANDARD_RELEASE_INPUTS - workflow_input_names(text))
            if missing:
                issues.append(
                    {
                        "product": str(product["id"]),
                        "severity": "error",
                        "code": "release-inputs-missing",
                        "path": workflow.relative_to(root).as_posix(),
                        "message": "missing inputs: " + ", ".join(missing),
                    }
                )
            if not release_cpp_quality_is_gated(text):
                issues.append(
                    {
                        "product": str(product["id"]),
                        "severity": "error",
                        "code": "release-cpp-quality-not-gated",
                        "path": workflow.relative_to(root).as_posix(),
                        "message": (
                            "release cpp-quality job must be gated by "
                            "inputs.run_cpp_quality so APT publishing can disable it"
                        ),
                    }
                )
            defaults = workflow_input_defaults(text)
            non_false_defaults = sorted(
                name
                for name in OPTIONAL_RELEASE_BOOLEAN_INPUTS & input_names
                if defaults.get(name, "").lower() != "false"
            )
            if non_false_defaults:
                issues.append(
                    {
                        "product": str(product["id"]),
                        "severity": "error",
                        "code": "release-optional-input-defaults-enabled",
                        "path": workflow.relative_to(root).as_posix(),
                        "message": (
                            "release optional boolean inputs must default false: "
                            + ", ".join(non_false_defaults)
                        ),
                    }
                )

    return issues


def markdown(issues: list[dict[str, str]]) -> str:
    lines = [
        "# XGC2 Product Workflow Audit",
        "",
        f"- Issues: `{len(issues)}`",
        "",
        "| Severity | Code | Product | Workflow | Message |",
        "| --- | --- | --- | --- | --- |",
    ]
    for issue in issues:
        lines.append(
            "| "
            f"{issue['severity']} | "
            f"`{issue['code']}` | "
            f"`{issue['product']}` | "
            f"`{issue['path']}` | "
            f"{issue['message']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--output-json")
    parser.add_argument("--output-md")
    parser.add_argument("--fail-on-error", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    catalog = load_catalog((root / args.catalog).resolve())
    issues: list[dict[str, str]] = []
    for product in catalog:
        if "_source" not in product:
            continue
        issues.extend(audit_product(root, product))

    if args.output_json:
        output = root / args.output_json
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"issues": issues}, indent=2, sort_keys=True) + "\n")
    if args.output_md:
        output = root / args.output_md
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown(issues), encoding="utf-8")

    errors = sum(1 for issue in issues if issue["severity"] == "error")
    warnings = sum(1 for issue in issues if issue["severity"] == "warn")
    print(f"workflow audit: {errors} error(s), {warnings} warning(s)")
    if args.fail_on_error and errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
