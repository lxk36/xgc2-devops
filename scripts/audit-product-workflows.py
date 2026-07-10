#!/usr/bin/env python3
"""Audit product workflows against the centralized XGC2 release contract."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


REQUIRED_RELEASE_INPUTS = {
    "expected_version",
    "expected_source_sha",
    "prepare_action",
    "apt_overlay_url",
    "dependency_set_digest",
    "run_cpp_quality",
    "run_source_tests",
}
FORBIDDEN_RELEASE_INPUTS = {
    "publish_apt",
    "release_id",
    "release_lock_digest",
    "trusted_ci_run_id",
    "ci_run_id",
}
# Keep publish_apt here so the parser's backward-compatibility unit test still
# proves that untyped booleans are detected. The release audit separately bans it.
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
)
FORBIDDEN_PRODUCT_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "duplicate-apt-overlay-prefix",
        re.compile(
            r"s#https://xgc2\.apt\.xiaokang\.ink#\$\{XGC2_APT_OVERLAY_URL%/\}#g;"
            r"\s*s#\$\{XGC2_APT_BASE_URL:-https://xgc2\.apt\.xiaokang\.ink\}#"
        ),
        "APT overlay replacement must not rewrite the base URL twice",
    ),
    (
        "product-apt-secret",
        re.compile(r"\bAPT_REPO_[A-Z0-9_]+\b"),
        "product code must not reference centralized APT credentials",
    ),
    (
        "product-production-environment",
        re.compile(r"\bxgc2-apt-production\b"),
        "product workflows must not reference the production Environment",
    ),
    (
        "product-publish-input-or-job",
        re.compile(r"\bpublish[-_]apt\b"),
        "product workflows must not expose APT publish inputs or jobs",
    ),
    (
        "product-publish-helper",
        re.compile(
            r"publish_(?:self_hosted_)?apt|publish_apt_repo|xgc2-publish|"
            r"\breprepro\b|\baptly\s+publish\b"
        ),
        "product code must not contain an APT publishing implementation",
    ),
    (
        "product-release-manifest",
        re.compile(r"xgc2\.release-artifact\.v1|xgc2_artifact_manifest\.py\s+release"),
        "only xgc2-devops may create release manifests",
    ),
)


def workflow_has_event(text: str, event: str) -> bool:
    return bool(re.search(rf"(?m)^\s*{re.escape(event)}\s*:", text))


def host_manifest_directory_precreated(text: str) -> bool:
    return bool(
        re.search(
            r"(?m)^\s*(?:install\s+-d(?:\s+-m\s+\S+)?|mkdir\s+-p)\b"
            r"[^\n]*\.ci/build-manifests(?:\"|\s|$)",
            text,
        )
    )


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


def _workflow_input_properties(text: str, property_name: str) -> dict[str, str]:
    values: dict[str, str] = {}
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
            match = re.match(
                rf"^\s*{re.escape(property_name)}\s*:\s*(.+?)\s*$", line
            )
            if match:
                values[current_input] = match.group(1).strip().strip("'\"")
    return values


def workflow_input_defaults(text: str) -> dict[str, str]:
    return _workflow_input_properties(text, "default")


def workflow_input_types(text: str) -> dict[str, str]:
    return _workflow_input_properties(text, "type")


def non_boolean_optional_release_inputs(text: str) -> set[str]:
    names = workflow_input_names(text)
    types = workflow_input_types(text)
    return {
        name
        for name in OPTIONAL_RELEASE_BOOLEAN_INPUTS & names
        if types.get(name, "").lower() != "boolean"
    }


def infer_release_workflow(source_dir: Path, product: dict[str, Any]) -> Path:
    workflow_dir = source_dir / ".github" / "workflows"
    release = product.get("release") if isinstance(product.get("release"), dict) else {}
    configured = release.get("workflow")
    if configured:
        return workflow_dir / str(configured)
    for name in PREFERRED_RELEASE_WORKFLOWS:
        candidate = workflow_dir / name
        if candidate.exists() and workflow_has_event(
            candidate.read_text(encoding="utf-8", errors="ignore"), "workflow_dispatch"
        ):
            return candidate
    return workflow_dir / "release.yml"


def workflow_job_blocks(text: str) -> dict[str, str]:
    jobs: dict[str, str] = {}
    lines = text.splitlines()
    in_jobs = False
    current = ""
    body: list[str] = []
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
            if current:
                jobs[current] = "\n".join(body)
            current = match.group(1)
            body = [line]
        elif current:
            body.append(line)
    if current:
        jobs[current] = "\n".join(body)
    return jobs


def workflow_pure_quality_jobs(text: str) -> set[str]:
    pure: set[str] = set()
    for name, body in workflow_job_blocks(text).items():
        if name in PUSH_QUALITY_GATE_JOBS and not any(
            marker in body for marker in BUILD_ARTIFACT_JOB_MARKERS
        ):
            pure.add(name)
    return pure


def workflow_quality_needs(text: str, quality_jobs: set[str] | None = None) -> set[str]:
    targets = quality_jobs if quality_jobs is not None else workflow_pure_quality_jobs(text)
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
            matches.update(set(re.findall(r"[A-Za-z0-9_-]+", value)) & targets)
            index += 1
            continue
        index += 1
        while index < len(lines):
            child = lines[index]
            child_indent = len(child) - len(child.lstrip(" "))
            if child.strip() and child_indent <= indent:
                break
            item = re.match(r"^\s*-\s*([A-Za-z0-9_-]+)\s*$", child)
            if item and item.group(1) in targets:
                matches.add(item.group(1))
            index += 1
    return matches


def workflow_quality_gates_other_jobs(workflow: Path) -> set[str]:
    if not workflow.exists():
        return set()
    text = workflow.read_text(encoding="utf-8", errors="ignore")
    quality = workflow_pure_quality_jobs(text)
    gated: set[str] = set()
    for name, body in workflow_job_blocks(text).items():
        if name not in quality:
            gated.update(workflow_quality_needs(body, quality))
    return gated


def push_runs_cpp_quality(root: Path, source_dir: Path) -> bool:
    del root
    workflow = source_dir / ".github" / "workflows" / "ci.yml"
    if not workflow.exists():
        workflow = workflow.with_suffix(".yaml")
    return workflow.exists() and "check_cpp_quality.sh" in workflow.read_text(
        encoding="utf-8", errors="ignore"
    )


def push_requires_version_bump(source_dir: Path) -> bool:
    workflow = source_dir / ".github" / "workflows" / "ci.yml"
    if not workflow.exists():
        workflow = workflow.with_suffix(".yaml")
    if not workflow.exists():
        return False
    text = workflow.read_text(encoding="utf-8", errors="ignore")
    return workflow_has_event(text, "push") and "check_version_bump.sh --ci" in text


def load_catalog(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    products = data.get("products", [])
    if not isinstance(products, list):
        raise ValueError(f"{path}: products must be a list")
    return [item for item in products if isinstance(item, dict)]


def is_apt_product(product: dict[str, Any]) -> bool:
    apt = product.get("apt")
    return isinstance(apt, dict) and bool(apt.get("install") or apt.get("packages"))


def issue(
    product: dict[str, Any], code: str, path: Path, root: Path, message: str
) -> dict[str, str]:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.as_posix()
    return {
        "product": str(product.get("id", "unknown")),
        "severity": "error",
        "code": code,
        "path": relative,
        "message": message,
    }


def forbidden_product_issues(
    root: Path, product: dict[str, Any], source_dir: Path, paths: list[Path]
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for code, pattern, message in FORBIDDEN_PRODUCT_PATTERNS:
            if pattern.search(text):
                issues.append(issue(product, code, path, root, message))
    return issues


def audit_product(root: Path, product: dict[str, Any]) -> list[dict[str, str]]:
    if not is_apt_product(product):
        return []
    source = root / str(product["_source"])
    source_dir = source.parent.parent
    workflow_dir = source_dir / ".github" / "workflows"
    release_config = product.get("release") if isinstance(product.get("release"), dict) else {}
    ci_workflow = workflow_dir / str(release_config.get("ci_workflow", "ci.yml"))
    release_workflow = infer_release_workflow(source_dir, product)
    issues: list[dict[str, str]] = []

    if push_requires_version_bump(source_dir):
        issues.append(
            issue(
                product,
                "push-requires-version-bump",
                ci_workflow,
                root,
                "ordinary push CI must not require product version bumps",
            )
        )
    cpp_quality = source_dir / ".xgc2" / "scripts" / "check_cpp_quality.sh"
    if cpp_quality.exists() and not push_runs_cpp_quality(root, source_dir):
        issues.append(
            issue(
                product,
                "push-cpp-quality-disabled",
                ci_workflow,
                root,
                "push CI must run check_cpp_quality.sh",
            )
        )

    for workflow, kind in ((ci_workflow, "push CI"), (release_workflow, "fallback release")):
        if not workflow.exists():
            issues.append(
                issue(
                    product,
                    f"missing-{kind.replace(' ', '-')}",
                    workflow,
                    root,
                    f"APT product must expose {kind}",
                )
            )
            continue
        text = workflow.read_text(encoding="utf-8", errors="ignore")
        if "xgc2_artifact_manifest.py build" not in text:
            issues.append(
                issue(
                    product,
                    f"{kind.replace(' ', '-')}-build-manifest-missing",
                    workflow,
                    root,
                    f"{kind} must create xgc2.build-artifact.v1",
                )
            )
        if "actions/upload-artifact" not in text:
            issues.append(
                issue(
                    product,
                    f"{kind.replace(' ', '-')}-artifact-upload-missing",
                    workflow,
                    root,
                    f"{kind} must upload trusted build output",
                )
            )
        if not re.search(r"(?m)^\s*retention-days:\s*14\s*$", text):
            issues.append(
                issue(
                    product,
                    f"{kind.replace(' ', '-')}-artifact-retention",
                    workflow,
                    root,
                    "trusted build artifacts must be retained for 14 days",
                )
            )
        for architecture in ("amd64", "arm64"):
            if architecture not in text:
                issues.append(
                    issue(
                        product,
                        f"{kind.replace(' ', '-')}-architecture-missing",
                        workflow,
                        root,
                        f"{kind} must cover {architecture}",
                    )
                )
        if (
            "docker run" in text
            and ".ci/build-manifests" in text
            and not host_manifest_directory_precreated(text)
        ):
            issues.append(
                issue(
                    product,
                    "host-manifest-directory-ownership",
                    workflow,
                    root,
                    "host must create .ci/build-manifests before a root Docker build",
                )
            )
        gated = workflow_quality_gates_other_jobs(workflow)
        if gated:
            issues.append(
                issue(
                    product,
                    "workflow-quality-gates-build",
                    workflow,
                    root,
                    "pure quality jobs must run in parallel: " + ", ".join(sorted(gated)),
                )
            )

    if release_workflow.exists():
        text = release_workflow.read_text(encoding="utf-8", errors="ignore")
        names = workflow_input_names(text)
        missing = sorted(REQUIRED_RELEASE_INPUTS - names)
        if missing:
            issues.append(
                issue(
                    product,
                    "prepare-inputs-missing",
                    release_workflow,
                    root,
                    "missing inputs: " + ", ".join(missing),
                )
            )
        forbidden = sorted(FORBIDDEN_RELEASE_INPUTS & names)
        if forbidden:
            issues.append(
                issue(
                    product,
                    "legacy-release-inputs-present",
                    release_workflow,
                    root,
                    "forbidden legacy inputs: " + ", ".join(forbidden),
                )
            )
        invalid_booleans = sorted(
            name
            for name in {"run_cpp_quality", "run_source_tests"} & names
            if workflow_input_types(text).get(name, "").lower() != "boolean"
        )
        if invalid_booleans:
            issues.append(
                issue(
                    product,
                    "prepare-boolean-input-types",
                    release_workflow,
                    root,
                    "boolean inputs must declare type boolean: "
                    + ", ".join(invalid_booleans),
                )
            )
        defaults = workflow_input_defaults(text)
        bad_defaults = sorted(
            name
            for name in {"run_cpp_quality", "run_source_tests"} & names
            if defaults.get(name, "").lower() != "false"
        )
        if bad_defaults:
            issues.append(
                issue(
                    product,
                    "prepare-boolean-input-defaults",
                    release_workflow,
                    root,
                    "optional booleans must default false: " + ", ".join(bad_defaults),
                )
            )
        required_behavior = {
            "inputs.prepare_action": "prepare_action must control release vs compatibility",
            "compatibility-verify": "workflow must implement compatibility-verify",
            "inputs.apt_overlay_url": "workflow must consume the staging overlay URL",
            "XGC2_APT_OVERLAY_URL": "workflow must pass the overlay into build scripts",
            "inputs.dependency_set_digest": "workflow must consume the dependency digest",
        }
        for marker, message in required_behavior.items():
            if marker not in text:
                issues.append(
                    issue(
                        product,
                        "prepare-contract-not-consumed",
                        release_workflow,
                        root,
                        message,
                    )
                )

    scan_paths = sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml"))
    script_dir = source_dir / ".xgc2" / "scripts"
    if script_dir.exists():
        scan_paths.extend(sorted(path for path in script_dir.rglob("*") if path.is_file()))
    issues.extend(forbidden_product_issues(root, product, source_dir, scan_paths))
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
    for item in issues:
        lines.append(
            f"| {item['severity']} | `{item['code']}` | `{item['product']}` | "
            f"`{item['path']}` | {item['message']} |"
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
    products = load_catalog((root / args.catalog).resolve())
    issues = [
        item
        for product in products
        if "_source" in product
        for item in audit_product(root, product)
    ]
    if args.output_json:
        output = root / args.output_json
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"issues": issues}, indent=2, sort_keys=True) + "\n")
    if args.output_md:
        output = root / args.output_md
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown(issues), encoding="utf-8")
    errors = sum(item["severity"] == "error" for item in issues)
    warnings = sum(item["severity"] == "warn" for item in issues)
    print(f"workflow audit: {errors} error(s), {warnings} warning(s)")
    return 1 if args.fail_on_error and errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
