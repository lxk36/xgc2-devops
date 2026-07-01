#!/usr/bin/env python3
"""Plan and optionally run XGC2 APT product releases by dependency order."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_APT_BASE_URL = "https://xgc2.apt.xiaokang.ink"
DEFAULT_ARCHES = ("amd64", "arm64")
PREFERRED_WORKFLOWS = (
    "build-debs.yml",
    "build-debs.yaml",
    "ci.yml",
    "ci.yaml",
    "release.yml",
    "release.yaml",
)


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def git(args: list[str], cwd: Path, *, check: bool = False) -> str:
    result = run(["git", *args], cwd=cwd, check=check)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def list_field(data: dict[str, Any], *path: str) -> list[str]:
    value: Any = data
    for key in path:
        if not isinstance(value, dict):
            return []
        value = value.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def parse_dep_package(dependency: str) -> str:
    return dependency.split(" ", 1)[0].strip()


def normalize_github_repo(url: str) -> str:
    def strip_dot_git(value: str) -> str:
        return value[:-4] if value.endswith(".git") else value

    url = url.strip()
    if re.fullmatch(r"[\w.-]+/[\w.-]+", url):
        return strip_dot_git(url)
    patterns = (
        r"^git@github\.com:(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^https://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, url)
        if match:
            return strip_dot_git(match.group("repo"))
    raise ValueError(f"unsupported GitHub remote URL: {url}")


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    kind: str
    version: str
    source_file: Path
    source_dir: Path
    apt_distributions: tuple[str, ...]
    apt_install: tuple[str, ...]
    apt_packages: tuple[str, ...]
    apt_depends: tuple[str, ...]
    release: dict[str, Any]

    @property
    def is_apt(self) -> bool:
        return bool(self.apt_packages or self.apt_install) and "apt" in self.kind

    @property
    def provided_apt_packages(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.apt_packages, *self.apt_install)))

    @property
    def apt_version_overrides(self) -> dict[str, str]:
        versions = self.release.get("apt_versions")
        if not isinstance(versions, dict):
            return {}
        return {str(distribution): str(version) for distribution, version in versions.items()}

    @property
    def apt_version_template(self) -> str:
        template = self.release.get("apt_version_template")
        return str(template) if template else ""

    @property
    def skip_apt_verify(self) -> bool:
        return bool(self.release.get("skip_apt_verify", False))


@dataclass(frozen=True)
class ReleaseTarget:
    product: Product
    repository: str
    ref: str
    workflow: str
    workflow_path: Path | None
    dispatch_inputs: dict[str, str]


def load_catalog(root: Path, catalog_path: Path | None) -> list[Product]:
    if catalog_path is None:
        catalog_path = root / ".work" / "release-products.json"
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        run(
            [
                "python3",
                "scripts/collect-products.py",
                "--root",
                ".",
                "--output",
                str(catalog_path.relative_to(root)),
            ],
            cwd=root,
            capture=False,
        )

    with catalog_path.open("r", encoding="utf-8") as handle:
        catalog = json.load(handle)

    products: list[Product] = []
    for item in catalog.get("products", []):
        source_file = root / str(item["_source"])
        apt = item.get("apt") if isinstance(item.get("apt"), dict) else {}
        release = item.get("release") if isinstance(item.get("release"), dict) else {}
        distributions = split_csv(str(apt.get("distribution", "focal")))
        products.append(
            Product(
                product_id=str(item["id"]),
                name=str(item["name"]),
                kind=str(item["kind"]),
                version=str(item.get("version", "")),
                source_file=source_file,
                source_dir=source_file.parent.parent,
                apt_distributions=tuple(distributions or ["focal"]),
                apt_install=tuple(list_field(item, "apt", "install")),
                apt_packages=tuple(list_field(item, "apt", "packages")),
                apt_depends=tuple(list_field(item, "apt", "depends")),
                release=release,
            )
        )
    return products


def build_graph(products: list[Product]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    active = [product for product in products if product.is_apt]
    owners: dict[str, str] = {}
    for product in active:
        for package in product.provided_apt_packages:
            owners[package] = product.product_id

    downstream: dict[str, set[str]] = {product.product_id: set() for product in active}
    upstream: dict[str, set[str]] = {product.product_id: set() for product in active}
    for product in active:
        for dependency in product.apt_depends:
            provider = owners.get(parse_dep_package(dependency))
            if provider and provider != product.product_id:
                downstream[provider].add(product.product_id)
                upstream[product.product_id].add(provider)
    return downstream, upstream


def changed_products(
    root: Path, products: list[Product], changed_from: str, changed_to: str
) -> set[str]:
    diff = git(["diff", "--name-only", f"{changed_from}..{changed_to}"], root, check=True)
    changed_paths = [Path(line) for line in diff.splitlines() if line.strip()]
    selected: set[str] = set()
    for product in products:
        source_dir = product.source_dir.relative_to(root)
        source_text = source_dir.as_posix()
        for changed_path in changed_paths:
            changed_text = changed_path.as_posix()
            if (
                changed_text == source_text
                or changed_text.startswith(f"{source_text}/")
                or source_text.startswith(f"{changed_text.rstrip('/')}/")
            ):
                selected.add(product.product_id)
                break
    return selected


def group_products(root: Path, products: list[Product], group: str) -> set[str]:
    normalized = group.strip().lower().replace("_", "-")
    if not normalized:
        return set()

    selected: set[str] = set()
    for product in products:
        source = product.source_file.relative_to(root).as_posix()
        if normalized in ("gazebo", "gazebo-sim"):
            if (
                source.startswith("products/ros1/simulator/gazebo-sim/")
                or source == "products/ros1/simulator/gazebo-sim/.xgc2/product.yml"
                or product.product_id == "xgc2-gazebo-sim-tools"
            ):
                selected.add(product.product_id)
        elif normalized == "simulator":
            if (
                source.startswith("products/ros1/simulator/")
                or source.startswith("products/ros2/simulator/")
                or product.product_id == "xgc2-gazebo-sim-tools"
            ):
                selected.add(product.product_id)
        elif normalized == "sitl":
            if "sitl" in product.product_id or "px4-sitl" in product.product_id:
                selected.add(product.product_id)
        else:
            raise ValueError(
                f"unknown group '{group}'; supported groups: gazebo-sim, simulator, sitl"
            )
    return selected


def downstream_closure(initial: set[str], downstream: dict[str, set[str]]) -> set[str]:
    selected = set(initial)
    queue = list(sorted(initial))
    while queue:
        current = queue.pop(0)
        for child in sorted(downstream.get(current, ())):
            if child not in selected:
                selected.add(child)
                queue.append(child)
    return selected


def topo_layers(selected: set[str], downstream: dict[str, set[str]]) -> list[list[str]]:
    indegree = {product_id: 0 for product_id in selected}
    for provider, consumers in downstream.items():
        if provider not in selected:
            continue
        for consumer in consumers:
            if consumer in selected:
                indegree[consumer] += 1

    ready = sorted(product_id for product_id, degree in indegree.items() if degree == 0)
    layers: list[list[str]] = []
    emitted: set[str] = set()
    while ready:
        layer = ready
        layers.append(layer)
        next_ready: list[str] = []
        for product_id in layer:
            emitted.add(product_id)
            for consumer in sorted(downstream.get(product_id, ())):
                if consumer not in indegree:
                    continue
                indegree[consumer] -= 1
                if indegree[consumer] == 0:
                    next_ready.append(consumer)
        ready = sorted(next_ready)

    if emitted != selected:
        cycle = ", ".join(sorted(selected - emitted))
        raise ValueError(f"dependency cycle or unresolved release graph among: {cycle}")
    return layers


def workflow_has_dispatch(path: Path) -> bool:
    if not path.exists():
        return False
    return "workflow_dispatch" in path.read_text(encoding="utf-8", errors="ignore")


def workflow_input_names(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    text = path.read_text(encoding="utf-8", errors="ignore")
    names: set[str] = set()
    for match in re.finditer(r"^\s{6}([A-Za-z_][A-Za-z0-9_-]*):", text, flags=re.MULTILINE):
        names.add(match.group(1))
    return names


def expected_apt_version(
    product: Product,
    distribution: str,
    *,
    run_number: int | None = None,
) -> str | None:
    overrides = product.apt_version_overrides
    if distribution in overrides:
        return overrides[distribution]
    if product.apt_version_template:
        if run_number is None:
            return None
        return product.apt_version_template.format(
            distribution=distribution,
            run_number=run_number,
            version=product.version,
        )
    return product.version or None


def apt_version_plan(product: Product) -> dict[str, str]:
    return {
        distribution: version
        for distribution in product.apt_distributions
        if (version := expected_apt_version(product, distribution)) is not None
    }


def version_summary(product: Product) -> str:
    versions = apt_version_plan(product)
    if versions:
        unique_versions = sorted(set(versions.values()))
        if len(unique_versions) == 1:
            return unique_versions[0]
        return ",".join(f"{dist}={version}" for dist, version in sorted(versions.items()))
    if product.apt_version_template:
        return f"template:{product.apt_version_template}"
    return product.version or "unversioned"


def infer_workflow(product: Product) -> tuple[str, Path | None]:
    configured = product.release.get("workflow")
    workflow_dir = product.source_dir / ".github" / "workflows"
    if configured:
        workflow_path = workflow_dir / str(configured)
        return str(configured), workflow_path if workflow_path.exists() else None
    for name in PREFERRED_WORKFLOWS:
        workflow_path = workflow_dir / name
        if workflow_has_dispatch(workflow_path):
            return name, workflow_path
    for workflow_path in sorted(workflow_dir.glob("*.y*ml")):
        if workflow_has_dispatch(workflow_path):
            return workflow_path.name, workflow_path
    raise ValueError(f"{product.product_id}: no workflow_dispatch workflow found")


def infer_repository(product: Product) -> str:
    configured = product.release.get("repository")
    if configured:
        return normalize_github_repo(str(configured))
    remote = git(["remote", "get-url", "origin"], product.source_dir)
    if not remote:
        raise ValueError(f"{product.product_id}: cannot infer git remote for {product.source_dir}")
    return normalize_github_repo(remote)


def infer_ref(product: Product) -> str:
    configured = product.release.get("ref")
    if configured:
        return str(configured)
    branch = git(["branch", "--show-current"], product.source_dir)
    if branch:
        return branch
    remote_branches = git(["branch", "-r", "--contains", "HEAD"], product.source_dir)
    candidates = []
    for line in remote_branches.splitlines():
        candidate = line.strip().lstrip("*").strip()
        if not candidate.startswith("origin/") or candidate == "origin/HEAD":
            continue
        candidates.append(candidate.split("/", 1)[1])
    preferred = ("noetic", "master", "main")
    for name in preferred:
        if name in candidates:
            return name
    if len(candidates) == 1:
        return candidates[0]
    remote_head = git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], product.source_dir)
    if remote_head.startswith("origin/"):
        return remote_head.split("/", 1)[1]
    raise ValueError(
        f"{product.product_id}: cannot infer release ref; add release.ref to product.yml"
    )


def build_targets(products_by_id: dict[str, Product], selected: set[str]) -> dict[str, ReleaseTarget]:
    targets: dict[str, ReleaseTarget] = {}
    for product_id in sorted(selected):
        product = products_by_id[product_id]
        workflow, workflow_path = infer_workflow(product)
        if workflow_path is not None and not workflow_has_dispatch(workflow_path):
            raise ValueError(f"{product_id}: {workflow_path} does not expose workflow_dispatch")
        dispatch_inputs: dict[str, str] = {}
        raw_inputs = product.release.get("inputs")
        if isinstance(raw_inputs, dict):
            dispatch_inputs = {str(key): str(value) for key, value in raw_inputs.items()}
        targets[product_id] = ReleaseTarget(
            product=product,
            repository=infer_repository(product),
            ref=infer_ref(product),
            workflow=workflow,
            workflow_path=workflow_path,
            dispatch_inputs=dispatch_inputs,
        )
    return targets


def product_plan_json(layers: list[list[str]], targets: dict[str, ReleaseTarget]) -> dict[str, Any]:
    return {
        "schema": "xgc2.release-plan.v1",
        "layers": [
            [
                {
                    "id": product_id,
                    "version": targets[product_id].product.version,
                    "apt_versions": apt_version_plan(targets[product_id].product),
                    "apt_version_template": targets[product_id].product.apt_version_template,
                    "skip_apt_verify": targets[product_id].product.skip_apt_verify,
                    "repository": targets[product_id].repository,
                    "ref": targets[product_id].ref,
                    "workflow": targets[product_id].workflow,
                    "inputs": targets[product_id].dispatch_inputs,
                    "apt_packages": list(targets[product_id].product.apt_packages),
                    "apt_distributions": list(targets[product_id].product.apt_distributions),
                }
                for product_id in layer
            ]
            for layer in layers
        ],
    }


def node_id(product_id: str) -> str:
    return "p_" + re.sub(r"[^A-Za-z0-9_]", "_", product_id)


def selected_edges(selected: set[str], downstream: dict[str, set[str]]) -> list[tuple[str, str]]:
    return [
        (provider, consumer)
        for provider in sorted(selected)
        for consumer in sorted(downstream.get(provider, ()))
        if consumer in selected
    ]


def product_label(target: ReleaseTarget) -> str:
    label = f"{target.product.product_id}\\n{version_summary(target.product)}"
    return label.replace('"', "'")


def plan_summary_markdown(
    layers: list[list[str]],
    targets: dict[str, ReleaseTarget],
    downstream: dict[str, set[str]],
) -> str:
    selected = set(targets)
    lines = [
        "# XGC2 APT Release DAG",
        "",
        "## Parallel Layers",
        "",
    ]
    for index, layer in enumerate(layers, start=1):
        items = ", ".join(f"`{product_id}`" for product_id in layer)
        lines.append(f"- Layer {index}: {items}")

    lines.extend(
        [
            "",
            "## Mermaid",
            "",
            "```mermaid",
            "flowchart LR",
        ]
    )
    for index, layer in enumerate(layers, start=1):
        lines.append(f'  subgraph layer_{index}["Layer {index} - parallel"]')
        for product_id in layer:
            lines.append(f'    {node_id(product_id)}["{product_label(targets[product_id])}"]')
        lines.append("  end")
    for provider, consumer in selected_edges(selected, downstream):
        lines.append(f"  {node_id(provider)} --> {node_id(consumer)}")
    lines.extend(["```", ""])
    return "\n".join(lines)


def write_plan_outputs(
    *,
    root: Path,
    plan_output: str,
    summary_output: str | None,
    layers: list[list[str]],
    targets: dict[str, ReleaseTarget],
    downstream: dict[str, set[str]],
) -> None:
    plan_path = root / plan_output
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    with plan_path.open("w", encoding="utf-8") as handle:
        json.dump(product_plan_json(layers, targets), handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"wrote {plan_path.relative_to(root)}")

    if summary_output:
        summary_path = root / summary_output
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            plan_summary_markdown(layers, targets, downstream),
            encoding="utf-8",
        )
        print(f"wrote {summary_path.relative_to(root)}")


def print_plan(layers: list[list[str]], targets: dict[str, ReleaseTarget]) -> None:
    print("Release plan:")
    for index, layer in enumerate(layers, start=1):
        print(f"  Layer {index}:")
        for product_id in layer:
            target = targets[product_id]
            print(
                "    "
                f"{product_id} {version_summary(target.product)} "
                f"repo={target.repository} ref={target.ref} workflow={target.workflow}"
            )


def trigger_workflow(
    target: ReleaseTarget,
    *,
    quality_required: bool,
    source_tests: bool,
) -> int:
    inputs = workflow_input_names(target.workflow_path)
    command = [
        "gh",
        "workflow",
        "run",
        target.workflow,
        "--repo",
        target.repository,
        "--ref",
        target.ref,
    ]
    if "publish_apt" in inputs:
        command.extend(["-f", "publish_apt=true"])
    if "run_cpp_quality" in inputs:
        command.extend(["-f", f"run_cpp_quality={str(quality_required).lower()}"])
    if "run_source_tests" in inputs:
        command.extend(["-f", f"run_source_tests={str(source_tests).lower()}"])
    for name, value in sorted(target.dispatch_inputs.items()):
        command.extend(["-f", f"{name}={value}"])

    triggered_after = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10))
    run(command, capture=False)
    return find_workflow_run(target, triggered_after)


def find_workflow_run(target: ReleaseTarget, triggered_after: str) -> int:
    for _ in range(30):
        result = run(
            [
                "gh",
                "run",
                "list",
                "--repo",
                target.repository,
                "--workflow",
                target.workflow,
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
                if str(item.get("createdAt", "")) >= triggered_after:
                    if target.ref and item.get("headBranch") not in (None, "", target.ref):
                        continue
                    run_id = item.get("databaseId")
                    if isinstance(run_id, int):
                        return run_id
        time.sleep(5)
    raise RuntimeError(f"{target.product.product_id}: could not find dispatched workflow run")


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
    target: ReleaseTarget,
    run_id: int,
    *,
    timeout_seconds: int,
    poll_seconds: int,
    quality_required: bool,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    print(f"{target.product.product_id}: waiting for {target.repository} run {run_id}")
    while time.time() < deadline:
        result = run(
            [
                "gh",
                "run",
                "view",
                str(run_id),
                "--repo",
                target.repository,
                "--json",
                "status,conclusion,jobs,url,number",
            ],
            check=False,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout or "{}")
            status = data.get("status")
            if status == "completed":
                ok, reason = run_completed_successfully(data, quality_required=quality_required)
                if ok:
                    print(f"{target.product.product_id}: workflow completed ({reason})")
                    return data
                raise RuntimeError(
                    f"{target.product.product_id}: workflow failed ({reason}) {data.get('url')}"
                )
        time.sleep(poll_seconds)
    raise TimeoutError(f"{target.product.product_id}: workflow run {run_id} timed out")


def apt_stanzas(base_url: str, distribution: str, arch: str) -> list[dict[str, str]]:
    url = f"{base_url.rstrip('/')}/dists/{distribution}/main/binary-{arch}/Packages"
    with urllib.request.urlopen(url, timeout=30) as response:
        text = response.read().decode("utf-8", errors="replace")
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


def apt_has_package(
    base_url: str, distribution: str, arch: str, package: str, version: str
) -> bool:
    return any(
        stanza.get("Package") == package and stanza.get("Version") == version
        for stanza in apt_stanzas(base_url, distribution, arch)
    )


def verify_apt_product(
    target: ReleaseTarget,
    *,
    base_url: str,
    arches: tuple[str, ...],
    timeout_seconds: int,
    poll_seconds: int,
    run_number: int | None,
) -> None:
    product = target.product
    if product.skip_apt_verify:
        print(f"{product.product_id}: apt verification skipped by product metadata")
        return
    packages = product.apt_packages or product.apt_install
    if not packages:
        return

    deadline = time.time() + timeout_seconds
    pending: set[tuple[str, str, str, str]] = set()
    for distribution in product.apt_distributions:
        version = expected_apt_version(product, distribution, run_number=run_number)
        if not version:
            raise RuntimeError(
                f"{product.product_id}: apt version is required for {distribution}; "
                "set version, release.apt_versions, release.apt_version_template, "
                "or release.skip_apt_verify"
            )
        for arch in arches:
            for package in packages:
                pending.add((distribution, arch, package, version))

    while pending and time.time() < deadline:
        for item in list(pending):
            distribution, arch, package, version = item
            try:
                if apt_has_package(base_url, distribution, arch, package, version):
                    pending.remove(item)
            except Exception as exc:  # noqa: BLE001 - keep retrying transient APT fetch errors.
                print(f"{product.product_id}: apt check retry after {type(exc).__name__}: {exc}")
        if pending:
            time.sleep(poll_seconds)

    if pending:
        missing = ", ".join(
            f"{dist}/{arch}:{pkg}={version}" for dist, arch, pkg, version in sorted(pending)
        )
        raise TimeoutError(
            f"{product.product_id}: expected apt version(s) not visible for {missing}"
        )
    print(f"{product.product_id}: apt index contains expected version(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="xgc2-devops repository root")
    parser.add_argument("--catalog", help="existing collect-products JSON output")
    parser.add_argument(
        "--product",
        action="append",
        default=[],
        help="changed/seed product id; may be repeated or comma-separated",
    )
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        help="seed product group; supported: gazebo-sim, simulator, sitl",
    )
    parser.add_argument("--changed-from", help="git base ref for changed product detection")
    parser.add_argument("--changed-to", default="HEAD", help="git head ref for changed product detection")
    parser.add_argument("--no-downstream", action="store_true", help="do not include reverse dependency closure")
    parser.add_argument("--execute", action="store_true", help="trigger and monitor GitHub release workflows")
    parser.add_argument("--no-wait", action="store_true", help="trigger workflows without waiting")
    parser.add_argument("--skip-apt-verify", action="store_true", help="skip APT Packages index verification")
    parser.add_argument("--quality-required", action="store_true", help="treat quality jobs as required")
    parser.add_argument("--source-tests", action="store_true", help="request source tests when workflow supports it")
    parser.add_argument("--apt-base-url", default=DEFAULT_APT_BASE_URL)
    parser.add_argument("--apt-arch", action="append", default=[], help="APT arch to verify")
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--apt-timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--plan-output", default=".work/release-plan.json")
    parser.add_argument("--summary-output", help="write a Markdown DAG summary")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    catalog_path = Path(args.catalog).resolve() if args.catalog else None
    products = [product for product in load_catalog(root, catalog_path) if product.is_apt]
    products_by_id = {product.product_id: product for product in products}
    downstream, _ = build_graph(products)

    selected = {
        product_id
        for item in args.product
        for product_id in split_csv(item)
    }
    if args.changed_from:
        selected.update(changed_products(root, products, args.changed_from, args.changed_to))
    for item in args.group:
        for group in split_csv(item):
            selected.update(group_products(root, products, group))
    if not selected:
        selected = set(products_by_id)

    unknown = sorted(product_id for product_id in selected if product_id not in products_by_id)
    if unknown:
        raise SystemExit(f"unknown product id(s): {', '.join(unknown)}")

    if not args.no_downstream:
        selected = downstream_closure(selected, downstream)

    layers = topo_layers(selected, downstream)
    targets = build_targets(products_by_id, selected)
    print_plan(layers, targets)
    write_plan_outputs(
        root=root,
        plan_output=args.plan_output,
        summary_output=args.summary_output,
        layers=layers,
        targets=targets,
        downstream=downstream,
    )

    if not args.execute:
        print("dry-run only; pass --execute to trigger workflows")
        return 0

    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required for --execute")

    arches = tuple(args.apt_arch or DEFAULT_ARCHES)
    for layer in layers:
        run_ids: dict[str, int] = {}
        for product_id in layer:
            target = targets[product_id]
            run_ids[product_id] = trigger_workflow(
                target,
                quality_required=args.quality_required,
                source_tests=args.source_tests,
            )
            print(f"{product_id}: triggered run {run_ids[product_id]}")

        if args.no_wait:
            continue

        for product_id in layer:
            target = targets[product_id]
            run_data = wait_for_run(
                target,
                run_ids[product_id],
                timeout_seconds=args.timeout_seconds,
                poll_seconds=args.poll_seconds,
                quality_required=args.quality_required,
            )
            if not args.skip_apt_verify:
                run_number = run_data.get("number")
                verify_apt_product(
                    target,
                    base_url=args.apt_base_url,
                    arches=arches,
                    timeout_seconds=args.apt_timeout_seconds,
                    poll_seconds=args.poll_seconds,
                    run_number=run_number if isinstance(run_number, int) else None,
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
