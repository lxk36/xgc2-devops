#!/usr/bin/env python3
"""Plan and optionally run XGC2 APT product releases by dependency order."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PREFERRED_WORKFLOWS = (
    "release.yml",
    "release.yaml",
    "build-debs.yml",
    "build-debs.yaml",
    "ci.yml",
    "ci.yaml",
)
RELEASE_ACTION = "release"
VERIFY_ACTION = "verify"
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


def bump_debian_revision(version: str) -> str:
    match = re.match(r"^(?P<prefix>.+-)(?P<revision>\d+)(?P<suffix>(?:[~+][A-Za-z0-9._:+~-]+)?)$", version)
    if not match:
        raise ValueError(f"cannot bump Debian revision for version: {version}")
    return (
        f"{match.group('prefix')}"
        f"{int(match.group('revision')) + 1}"
        f"{match.group('suffix')}"
    )


def bump_or_promote_version(version: str, version_series: str) -> str:
    if not version_series:
        return bump_debian_revision(version)
    match = re.match(
        r"^(?P<series>\d+\.\d+)\.(?P<patch>\d+)-(?P<revision>\d+)"
        r"(?P<suffix>(?:[~+][A-Za-z0-9._:+~-]+)?)$",
        version,
    )
    if not match:
        return bump_debian_revision(version)
    current_series = match.group("series")
    if current_series == version_series:
        return bump_debian_revision(version)
    if current_series == "1.0" and version_series == "1.1":
        return f"{version_series}.{match.group('patch')}-1{match.group('suffix')}"
    return bump_debian_revision(version)


def normalize_github_repo(url: str) -> str:
    def strip_dot_git(value: str) -> str:
        return value[:-4] if value.endswith(".git") else value

    def redact_credentials(value: str) -> str:
        return re.sub(r"^(https://)[^/@]+@", r"\1***@", value)

    url = url.strip()
    if re.fullmatch(r"[\w.-]+/[\w.-]+", url):
        return strip_dot_git(url)
    patterns = (
        r"^git@github\.com:(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^https://(?:[^/@]+@)?github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^https://(?:[^/@]+:[^/@]+@)?github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^https://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, url)
        if match:
            return strip_dot_git(match.group("repo"))
    raise ValueError(f"unsupported GitHub remote URL: {redact_credentials(url)}")


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
    groups: tuple[str, ...]
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

    @property
    def release_requires(self) -> tuple[str, ...]:
        value = self.release.get("requires", [])
        if not isinstance(value, list):
            raise ValueError(f"{self.product_id}: release.requires must be a list")
        return tuple(str(item) for item in value)

    @property
    def ci_workflow(self) -> str:
        value = self.release.get("ci_workflow", "ci.yml")
        return str(value) if value else "ci.yml"


@dataclass(frozen=True)
class ReleaseTarget:
    product: Product
    repository: str
    ref: str
    workflow: str
    workflow_path: Path | None
    dispatch_inputs: dict[str, str]
    action: str
    source_sha: str
    expected_version: str
    expected_apt_versions: dict[str, str]


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
                groups=tuple(list_field(item, "groups")),
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
    active_ids = set(upstream)
    for product in active:
        for dependency in product.apt_depends:
            provider = owners.get(parse_dep_package(dependency))
            if provider and provider != product.product_id:
                downstream[provider].add(product.product_id)
                upstream[product.product_id].add(provider)
        for provider in product.release_requires:
            if provider not in active_ids:
                raise ValueError(
                    f"{product.product_id}: release.requires references unknown APT product {provider}"
                )
            if provider == product.product_id:
                raise ValueError(f"{product.product_id}: release.requires cannot reference itself")
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
        metadata_groups = {item.lower().replace("_", "-") for item in product.groups}
        if normalized in metadata_groups:
            selected.add(product.product_id)
        elif normalized in ("toolchain", "base"):
            if product.kind == "toolchain-apt":
                selected.add(product.product_id)
        elif normalized in ("uav-tracking", "multirotor-tracking"):
            if product.product_id in {
                "xgc2-tbb",
                "xgc2-acados",
                "libxgc2-math-dev",
                "libxgc2-state-machine-dev",
                "xgc2-ros-msgs",
                "xgc2-ros1-utils",
                "xgc2-estimator-hover-thrust",
                "xgc2-estimator-rigid-state",
                "xgc2-multirotor-controller",
                "xgc2-gazebo-sim-worlds",
                "xgc2-gazebo-sim-vrpn-bridge",
                "xgc2-px4-sitl-112",
                "xgc2-px4-sitl-114",
                "xgc2-gazebo-sim-fs150-sitl",
                "xgc2-gazebo-sim-visualization",
                "xgc2-gazebo-sim-tools",
                "xgc2-gazebo-sim",
            }:
                selected.add(product.product_id)
        elif normalized in ("ugv-tracking", "unicycle-tracking"):
            if product.product_id in {
                "xgc2-tbb",
                "xgc2-acados",
                "libxgc2-math-dev",
                "libxgc2-state-machine-dev",
                "xgc2-ros-msgs",
                "xgc2-ros1-utils",
                "xgc2-estimator-rigid-state",
                "xgc2-ugv-controller",
                "xgc2-gazebo-sim-worlds",
                "xgc2-gazebo-sim-vrpn-bridge",
                "xgc2-gazebo-sim-scout",
                "xgc2-gazebo-sim-visualization",
                "xgc2-gazebo-sim-tools",
                "xgc2-gazebo-sim",
            }:
                selected.add(product.product_id)
        elif normalized in ("gazebo", "gazebo-sim"):
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
                "unknown group "
                f"'{group}'; supported groups: gazebo-sim, simulator, sitl, "
                "toolchain, uav-tracking, ugv-tracking"
            )
    return selected


def graph_closure(initial: set[str], graph: dict[str, set[str]]) -> set[str]:
    selected = set(initial)
    queue = list(sorted(initial))
    while queue:
        current = queue.pop(0)
        for neighbor in sorted(graph.get(current, ())):
            if neighbor not in selected:
                selected.add(neighbor)
                queue.append(neighbor)
    return selected


def downstream_closure(initial: set[str], downstream: dict[str, set[str]]) -> set[str]:
    return graph_closure(initial, downstream)


def upstream_closure(initial: set[str], upstream: dict[str, set[str]]) -> set[str]:
    return graph_closure(initial, upstream)


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


def planned_product_version(
    product: Product,
    action: str,
    *,
    bump_release_versions: bool,
    version_series: str,
) -> str:
    if action != RELEASE_ACTION or not bump_release_versions:
        return product.version
    if not product.version:
        return product.version
    if product.apt_version_template:
        return product.version
    return bump_or_promote_version(product.version, version_series)


def planned_apt_versions(
    product: Product,
    action: str,
    *,
    bump_release_versions: bool,
    version_series: str,
) -> dict[str, str]:
    versions = apt_version_plan(product)
    if action != RELEASE_ACTION or not bump_release_versions:
        return versions
    if product.apt_version_template:
        return versions
    planned_version = planned_product_version(
        product,
        action,
        bump_release_versions=bump_release_versions,
        version_series=version_series,
    )
    if product.apt_version_overrides:
        # Distribution overrides are scoped views of the product version, not
        # independent version counters. Preserve only the distro qualifier so
        # stale overrides cannot drift behind product.yml (for example
        # 0.5.6-2~focal while the product is 0.5.6-3).
        scoped: dict[str, str] = {}
        for distribution, version in versions.items():
            suffix_match = re.search(r"([~+][A-Za-z0-9._:+~-]+)$", version)
            suffix = suffix_match.group(1) if suffix_match else ""
            scoped[distribution] = f"{planned_version}{suffix}"
        return scoped
    return {distribution: planned_version for distribution in product.apt_distributions}


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


def source_sha(product: Product) -> str:
    sha = git(["rev-parse", "HEAD"], product.source_dir)
    if not sha:
        raise ValueError(f"{product.product_id}: cannot infer source SHA for {product.source_dir}")
    return sha


def build_targets(
    products_by_id: dict[str, Product],
    selected: set[str],
    action_by_id: dict[str, str],
    *,
    bump_release_versions: bool,
    version_series: str,
) -> dict[str, ReleaseTarget]:
    targets: dict[str, ReleaseTarget] = {}
    for product_id in sorted(selected):
        product = products_by_id[product_id]
        workflow, workflow_path = infer_workflow(product)
        if workflow_path is not None and not workflow_has_dispatch(workflow_path):
            raise ValueError(f"{product_id}: {workflow_path} does not expose workflow_dispatch")
        input_names = workflow_input_names(workflow_path)
        target_can_validate_version = "expected_version" in input_names
        target_bump_versions = bump_release_versions and target_can_validate_version
        dispatch_inputs: dict[str, str] = {}
        raw_inputs = product.release.get("inputs")
        if isinstance(raw_inputs, dict):
            dispatch_inputs = {str(key): str(value) for key, value in raw_inputs.items()}
        reserved_inputs = sorted(set(dispatch_inputs) & STANDARD_WORKFLOW_INPUTS)
        if reserved_inputs:
            raise ValueError(
                f"{product_id}: release.inputs must not override standard "
                "orchestrator inputs: " + ", ".join(reserved_inputs)
            )
        action = action_by_id.get(product_id, RELEASE_ACTION)
        expected_versions = planned_apt_versions(
            product,
            action,
            bump_release_versions=target_bump_versions,
            version_series=version_series,
        )
        unique_versions = sorted(set(expected_versions.values()))
        expected_version = (
            unique_versions[0]
            if len(unique_versions) == 1
            else planned_product_version(
                product,
                action,
                bump_release_versions=target_bump_versions,
                version_series=version_series,
            )
        )
        targets[product_id] = ReleaseTarget(
            product=product,
            repository=infer_repository(product),
            ref=infer_ref(product),
            workflow=workflow,
            workflow_path=workflow_path,
            dispatch_inputs=dispatch_inputs,
            action=action,
            source_sha=source_sha(product),
            expected_version=expected_version,
            expected_apt_versions=expected_versions,
        )
    return targets


def target_plan_item(product_id: str, layer_index: int, target: ReleaseTarget) -> dict[str, Any]:
    product = target.product
    return {
        "id": product_id,
        "action": target.action,
        "layer": layer_index,
        "version": product.version,
        "expected_version": target.expected_version,
        "apt_versions": target.expected_apt_versions,
        "apt_version_template": product.apt_version_template,
        "skip_apt_verify": product.skip_apt_verify,
        "repository": target.repository,
        "ref": target.ref,
        "source": product.source_dir.as_posix(),
        "source_sha": target.source_sha,
        "expected_source_sha": target.source_sha,
        "workflow": target.workflow,
        "ci_workflow": product.ci_workflow,
        "workflow_inputs": sorted(workflow_input_names(target.workflow_path)),
        "inputs": target.dispatch_inputs,
        "apt_packages": list(product.apt_packages or product.apt_install),
        "apt_install": list(product.apt_install),
        "apt_distributions": list(product.apt_distributions),
    }


def product_plan_json(
    layers: list[list[str]],
    targets: dict[str, ReleaseTarget],
    downstream: dict[str, set[str]],
) -> dict[str, Any]:
    selected = set(targets)
    dependencies: dict[str, list[str]] = {product_id: [] for product_id in selected}
    for provider, consumers in downstream.items():
        if provider not in selected:
            continue
        for consumer in consumers:
            if consumer in selected:
                dependencies[consumer].append(provider)
    return {
        "schema": "xgc2.release-plan.v1",
        "max_parallel": 4,
        "layers": [
            [
                {
                    **target_plan_item(product_id, layer_index, targets[product_id]),
                    "dependencies": sorted(dependencies[product_id]),
                }
                for product_id in layer
            ]
            for layer_index, layer in enumerate(layers, start=1)
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
    label = (
        f"{target.product.product_id}\\n"
        f"{target.action}\\n"
        f"{target.expected_version or version_summary(target.product)}"
    )
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
        items = ", ".join(
            f"`{product_id}` ({targets[product_id].action})" for product_id in layer
        )
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
    lock_output: str | None,
    layers: list[list[str]],
    targets: dict[str, ReleaseTarget],
    downstream: dict[str, set[str]],
) -> None:
    plan = product_plan_json(layers, targets, downstream)
    plan_path = root / plan_output
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    with plan_path.open("w", encoding="utf-8") as handle:
        json.dump(plan, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"wrote {plan_path.relative_to(root)}")

    if lock_output:
        lock_path = root / lock_output
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_products = [
            item
            for layer in plan["layers"]
            for item in layer
        ]
        with lock_path.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema": "xgc2.release-lock.v1",
                    "products": lock_products,
                },
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
        print(f"wrote {lock_path.relative_to(root)}")

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
                f"{product_id} action={target.action} "
                f"expected={target.expected_version or version_summary(target.product)} "
                f"repo={target.repository} ref={target.ref} workflow={target.workflow}"
            )


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
        help="seed product group; supported: uav-tracking, ugv-tracking, gazebo-sim, simulator, sitl, toolchain",
    )
    parser.add_argument("--changed-from", help="git base ref for changed product detection")
    parser.add_argument("--changed-to", default="HEAD", help="git head ref for changed product detection")
    parser.add_argument("--no-upstream", action="store_true", help="do not include prerequisite dependency closure")
    parser.add_argument("--no-downstream", action="store_true", help="do not include reverse dependency closure")
    parser.add_argument(
        "--bump-release-versions",
        action="store_true",
        help="plan release actions with the next Debian revision",
    )
    parser.add_argument(
        "--release-version-series",
        default="",
        help="optional target series for 1.0.x release products, e.g. 1.1",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="deprecated safety guard; batch execution is only available in release-orchestrator",
    )
    parser.add_argument("--plan-output", default=".work/release-plan.json")
    parser.add_argument("--lock-output", default=".work/release-lock.json")
    parser.add_argument("--summary-output", help="write a Markdown DAG summary")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    catalog_path = Path(args.catalog).resolve() if args.catalog else None
    products = [product for product in load_catalog(root, catalog_path) if product.is_apt]
    products_by_id = {product.product_id: product for product in products}
    downstream, upstream = build_graph(products)

    explicit_seed = {
        product_id
        for item in args.product
        for product_id in split_csv(item)
    }
    changed_seed: set[str] = set()
    if args.changed_from:
        changed_seed.update(changed_products(root, products, args.changed_from, args.changed_to))
    group_seed: set[str] = set()
    for item in args.group:
        for group in split_csv(item):
            group_seed.update(group_products(root, products, group))

    seed = set(explicit_seed | changed_seed | group_seed)
    if not seed:
        seed = set(products_by_id)
        explicit_seed = set(seed)

    unknown = sorted(product_id for product_id in seed if product_id not in products_by_id)
    if unknown:
        raise SystemExit(f"unknown product id(s): {', '.join(unknown)}")

    selected = set(seed)
    prerequisite_ids: set[str] = set()
    downstream_ids: set[str] = set()
    if not args.no_upstream:
        prerequisite_ids = upstream_closure(seed, upstream) - seed
        selected.update(prerequisite_ids)
    if not args.no_downstream:
        downstream_roots = explicit_seed | changed_seed
        downstream_ids = downstream_closure(downstream_roots, downstream) - seed
        selected.update(downstream_ids)

    action_by_id = {
        product_id: (VERIFY_ACTION if product_id in prerequisite_ids else RELEASE_ACTION)
        for product_id in selected
    }
    for product_id in downstream_ids:
        action_by_id[product_id] = RELEASE_ACTION

    layers = topo_layers(selected, downstream)
    targets = build_targets(
        products_by_id,
        selected,
        action_by_id,
        bump_release_versions=args.bump_release_versions,
        version_series=args.release_version_series,
    )
    print_plan(layers, targets)
    write_plan_outputs(
        root=root,
        plan_output=args.plan_output,
        summary_output=args.summary_output,
        lock_output=args.lock_output,
        layers=layers,
        targets=targets,
        downstream=downstream,
    )

    if args.execute:
        raise SystemExit(
            "--execute is disabled: dispatch the xgc2-devops release-orchestrator "
            "workflow so approvals, digest-bound recovery, and dynamic scheduling apply"
        )
    print("dry-run plan complete; execute it through the release-orchestrator workflow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
