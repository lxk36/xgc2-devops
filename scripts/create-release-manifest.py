#!/usr/bin/env python3
"""Create and validate XGC2 build and release artifact manifests."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


BUILD_SCHEMA = "xgc2.build-artifact.v1"
RELEASE_SCHEMA = "xgc2.release-artifact.v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deb_metadata(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "dpkg-deb",
            "--show",
            "--showformat=${Package}\n${Version}\n${Architecture}\n",
            str(path),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    fields = result.stdout.splitlines()
    if len(fields) != 3 or not all(fields):
        raise ValueError(f"invalid dpkg metadata in {path}")
    package, version, architecture = fields
    return {
        "file": path.name,
        "package": package,
        "version": version,
        "architecture": architecture,
        "sha256": sha256(path),
        "size": path.stat().st_size,
    }


def debs_from_dir(deb_dir: Path, target_architecture: str) -> list[dict[str, Any]]:
    paths = sorted(deb_dir.glob("*.deb"))
    if not paths:
        raise ValueError(f"no deb files in {deb_dir}")
    entries = [deb_metadata(path) for path in paths]
    filenames = [str(entry["file"]) for entry in entries]
    if len(filenames) != len(set(filenames)):
        raise ValueError("duplicate deb filenames")
    invalid = [
        str(entry["file"])
        for entry in entries
        if entry["architecture"] not in (target_architecture, "all")
    ]
    if invalid:
        raise ValueError(
            f"debs do not match target architecture {target_architecture}: {', '.join(invalid)}"
        )
    return entries


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def require_identity(value: dict[str, Any], schema: str) -> None:
    if value.get("schema") != schema:
        raise ValueError(f"expected schema {schema}")
    for key in ("product", "version", "distribution", "architecture"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError(f"{key} must be a non-empty string")
    source_sha = value.get("source_sha")
    if not isinstance(source_sha, str) or SOURCE_SHA.fullmatch(source_sha) is None:
        raise ValueError("source_sha must be a 40- or 64-character lowercase hex digest")


def validate_debs(value: dict[str, Any], deb_dir: Path) -> list[dict[str, Any]]:
    entries = value.get("debs")
    if not isinstance(entries, list) or not entries:
        raise ValueError("debs must be a non-empty array")
    actual = {
        path.name: deb_metadata(path)
        for path in sorted(deb_dir.glob("*.deb"))
    }
    if set(actual) != {entry.get("file") for entry in entries if isinstance(entry, dict)}:
        raise ValueError("manifest deb set does not match deb directory")
    for entry in entries:
        if not isinstance(entry, dict) or actual.get(str(entry.get("file"))) != entry:
            raise ValueError(f"deb metadata or SHA256 mismatch: {entry!r}")
    target = str(value["architecture"])
    if any(entry["architecture"] not in (target, "all") for entry in entries):
        raise ValueError("manifest target architecture does not match deb architecture")
    return entries


def validate_ci_identity(value: dict[str, Any]) -> None:
    ci = value.get("ci")
    if not isinstance(ci, dict):
        raise ValueError("ci must be an object")
    if not all(isinstance(ci.get(key), (str, int)) and str(ci[key]) for key in ("run_id", "workflow", "workflow_ref")):
        raise ValueError("build manifest has incomplete CI identity")


def create_build(args: argparse.Namespace) -> int:
    if SOURCE_SHA.fullmatch(args.source_sha) is None:
        raise ValueError("--source-sha must be a 40- or 64-character lowercase hex digest")
    run_id = args.ci_run_id or os.environ.get("GITHUB_RUN_ID", "")
    workflow = args.ci_workflow or os.environ.get("GITHUB_WORKFLOW", "")
    workflow_ref = args.ci_workflow_ref or os.environ.get("GITHUB_WORKFLOW_REF", "")
    if not run_id or not workflow or not workflow_ref:
        raise ValueError("CI run id, workflow, and workflow ref are required")
    manifest = {
        "schema": BUILD_SCHEMA,
        "product": args.product,
        "version": args.version,
        "source_sha": args.source_sha,
        "distribution": args.distribution,
        "architecture": args.architecture,
        "ci": {
            "run_id": str(run_id),
            "workflow": workflow,
            "workflow_ref": workflow_ref,
        },
        "debs": debs_from_dir(Path(args.deb_dir), args.architecture),
    }
    write_json(Path(args.output), manifest)
    return 0


def create_release(args: argparse.Namespace) -> int:
    build_path = Path(args.build_manifest)
    build = json.loads(build_path.read_text(encoding="utf-8"))
    if not isinstance(build, dict):
        raise ValueError("build manifest must be an object")
    require_identity(build, BUILD_SCHEMA)
    validate_ci_identity(build)
    entries = validate_debs(build, Path(args.deb_dir))
    if not args.release_id:
        raise ValueError("--release-id is required")
    if HEX64.fullmatch(args.release_lock_digest) is None:
        raise ValueError("--release-lock-digest must be 64 lowercase hex characters")
    output_dir = Path(args.output_dir)
    included_build = output_dir / "build-manifests" / build_path.name
    included_build.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(build_path, included_build)
    for entry in entries:
        shutil.copy2(Path(args.deb_dir) / str(entry["file"]), output_dir / str(entry["file"]))
    manifest = {
        **{key: build[key] for key in (
            "product",
            "version",
            "source_sha",
            "distribution",
            "architecture",
            "ci",
            "debs",
        )},
        "schema": RELEASE_SCHEMA,
        "release_id": args.release_id,
        "release_lock_digest": args.release_lock_digest,
        "build_manifest": included_build.relative_to(output_dir).as_posix(),
        "build_manifest_digest": sha256(included_build),
        "published_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    for entry in entries:
        output = (
            output_dir
            / "manifests"
            / str(build["product"])
            / str(build["distribution"])
            / str(build["architecture"])
            / f"{entry['package']}_{entry['version']}.json"
        )
        write_json(output, manifest)
    return 0


def validate(args: argparse.Namespace) -> int:
    path = Path(args.manifest)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest must be an object")
    schema = str(value.get("schema", ""))
    require_identity(value, schema)
    validate_debs(value, Path(args.deb_dir))
    if schema == BUILD_SCHEMA:
        if "release_lock_digest" in value:
            raise ValueError("build manifest must not contain release_lock_digest")
        validate_ci_identity(value)
    elif schema == RELEASE_SCHEMA:
        for key in ("release_id", "build_manifest_digest", "published_at"):
            if not value.get(key):
                raise ValueError(f"release manifest is missing {key}")
        if HEX64.fullmatch(str(value.get("release_lock_digest", ""))) is None:
            raise ValueError("invalid release_lock_digest")
    else:
        raise ValueError(f"unsupported manifest schema: {schema}")
    return 0


def add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--product", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--distribution", required=True)
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--deb-dir", required=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="create xgc2.build-artifact.v1")
    add_identity_arguments(build)
    build.add_argument("--ci-run-id", default="")
    build.add_argument("--ci-workflow", default="")
    build.add_argument("--ci-workflow-ref", default="")
    build.add_argument("--output", required=True)
    build.set_defaults(handler=create_build)

    release = subparsers.add_parser("release", help="create xgc2.release-artifact.v1")
    release.add_argument("--build-manifest", required=True)
    release.add_argument("--deb-dir", required=True)
    release.add_argument("--release-id", required=True)
    release.add_argument("--release-lock-digest", required=True)
    release.add_argument("--output-dir", required=True)
    release.set_defaults(handler=create_release)

    check = subparsers.add_parser("validate", help="validate a manifest and its debs")
    check.add_argument("--manifest", required=True)
    check.add_argument("--deb-dir", required=True)
    check.set_defaults(handler=validate)

    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
