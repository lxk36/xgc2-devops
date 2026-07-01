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
    release_workflow = workflow_dir / "release.yml"
    release_config = product.get("release") if isinstance(product.get("release"), dict) else {}
    has_configured_workflow = bool(release_config.get("workflow"))
    if not has_configured_workflow and not release_workflow.exists():
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
        if workflow.name == "release.yml":
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
