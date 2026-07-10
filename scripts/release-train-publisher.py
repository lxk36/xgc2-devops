#!/usr/bin/env python3
"""SSH client for the centralized XGC2 release-train staging protocol."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DIST = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")


class PublisherError(RuntimeError):
    pass


def canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(path for path in root.rglob("*") if path.is_file() and not path.is_symlink()):
        digest.update(
            f"{file_digest(path)}  {path.relative_to(root).as_posix()}\n".encode("utf-8")
        )
    return digest.hexdigest()


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise PublisherError(f"{name} is required")
    return value


def timeout_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    if not raw.isdigit() or int(raw) < 1:
        raise PublisherError(f"{name} must be a positive integer")
    return int(raw)


def validate_identity(release_id: str, lock_digest: str) -> None:
    if RELEASE_ID.fullmatch(release_id) is None:
        raise PublisherError(f"invalid release id: {release_id!r}")
    if HEX64.fullmatch(lock_digest) is None:
        raise PublisherError("release lock digest must be a lowercase SHA256")


@contextmanager
def ssh_configuration() -> Iterator[list[str]]:
    host = required_env("APT_REPO_HOST")
    user = required_env("APT_REPO_USER")
    port = required_env("APT_REPO_PORT")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise PublisherError(f"invalid APT_REPO_PORT: {port!r}")
    key = required_env("APT_REPO_SSH_KEY")
    known_hosts = required_env("APT_REPO_KNOWN_HOSTS")
    with tempfile.TemporaryDirectory(prefix="xgc2-publisher-") as directory:
        root = Path(directory)
        key_path = root / "identity"
        known_hosts_path = root / "known_hosts"
        key_path.write_text(key + ("" if key.endswith("\n") else "\n"), encoding="utf-8")
        key_path.chmod(0o600)
        known_hosts_path.write_text(
            known_hosts + ("" if known_hosts.endswith("\n") else "\n"), encoding="utf-8"
        )
        known_hosts_path.chmod(0o600)
        yield [
            "ssh",
            "-p",
            port,
            "-i",
            os.fspath(key_path),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts_path}",
            "-o",
            f"ConnectTimeout={timeout_env('APT_REPO_SSH_CONNECT_TIMEOUT_SECONDS', 20)}",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=4",
            "-o",
            "TCPKeepAlive=yes",
            f"{user}@{host}",
        ]


def run_remote(
    command: list[str],
    *,
    stdin: BinaryIO | None = None,
    echo: bool = True,
    timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    timeout_seconds = timeout_seconds or timeout_env("APT_REPO_SSH_TIMEOUT_SECONDS", 900)
    with ssh_configuration() as base:
        try:
            result = subprocess.run(
                [*base, *command],
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
            stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
            result = subprocess.CompletedProcess(
                args=[*base, *command],
                returncode=124,
                stdout=stdout,
                stderr=stderr + f"\nlocal SSH timeout after {timeout_seconds}s\n".encode(),
            )
    if echo and result.stdout:
        sys.stdout.buffer.write(result.stdout)
    if echo and result.stderr:
        sys.stderr.buffer.write(result.stderr)
    return result


def status_value(release_id: str, lock_digest: str) -> dict[str, object] | None:
    result = run_remote(
        ["stage-status", release_id, lock_digest],
        echo=False,
        timeout_seconds=timeout_env("APT_REPO_SSH_STATUS_TIMEOUT_SECONDS", 120),
    )
    if result.returncode:
        return None
    try:
        value = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def json_stdout(result: subprocess.CompletedProcess[bytes], context: str) -> dict[str, object]:
    try:
        value = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublisherError(f"{context} returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PublisherError(f"{context} JSON must be an object")
    return value


def finish(result: subprocess.CompletedProcess[bytes]) -> None:
    if result.returncode == 0:
        return
    if result.returncode in {124, 255}:
        raise SystemExit(75)
    raise SystemExit(result.returncode)


def stage(args: argparse.Namespace) -> int:
    validate_identity(args.release_id, args.release_lock_digest)
    if DIST.fullmatch(args.distribution) is None:
        raise PublisherError(f"invalid distribution: {args.distribution!r}")
    bundle = Path(args.bundle).resolve(strict=True)
    if not bundle.is_dir():
        raise PublisherError(f"bundle is not a directory: {bundle}")
    expected_digest = tree_digest(bundle)
    with tempfile.TemporaryFile() as stream:
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for path in sorted(bundle.rglob("*")):
                if path.is_symlink():
                    raise PublisherError(f"bundle contains a symbolic link: {path}")
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(bundle).as_posix(), recursive=False)
        stream.seek(0)
        result = run_remote(
            ["stage", args.release_id, args.release_lock_digest, args.distribution],
            stdin=stream,
        )
    if result.returncode in {124, 255}:
        value = status_value(args.release_id, args.release_lock_digest)
        bundles = value.get("bundles") if isinstance(value, dict) else None
        distributions = value.get("distributions") if isinstance(value, dict) else None
        bundle_state = bundles.get(expected_digest) if isinstance(bundles, dict) else None
        distribution_state = (
            distributions.get(args.distribution) if isinstance(distributions, dict) else None
        )
        if (
            isinstance(value, dict)
            and value.get("status") in {"prepared", "promoted"}
            and isinstance(bundle_state, dict)
            and isinstance(bundle_state.get("manifests"), list)
            and bool(bundle_state["manifests"])
            and isinstance(bundle_state.get("product"), dict)
            and bundle_state["product"].get("distribution") == args.distribution
            and isinstance(distribution_state, dict)
            and distribution_state.get("published") is True
        ):
            print(json.dumps({"ok": True, "status": "confirmed-after-timeout", "bundle_digest": expected_digest}))
            return 0
        raise SystemExit(75)
    finish(result)
    response = json_stdout(result, "stage")
    if response.get("bundle_digest") != expected_digest:
        raise PublisherError("stage response bundle digest mismatch")
    return 0


def status(args: argparse.Namespace) -> int:
    validate_identity(args.release_id, args.release_lock_digest)
    result = run_remote(["stage-status", args.release_id, args.release_lock_digest])
    finish(result)
    if args.validate_json:
        json_stdout(result, "stage-status")
    return 0


def promote(args: argparse.Namespace) -> int:
    validate_identity(args.release_id, args.release_lock_digest)
    train = Path(args.train).resolve(strict=True)
    value = json.loads(train.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != "xgc2.release-train.v1":
        raise PublisherError("promotion input must be xgc2.release-train.v1")
    if value.get("release_id") != args.release_id:
        raise PublisherError("release train id does not match command")
    if value.get("release_lock_digest") != args.release_lock_digest:
        raise PublisherError("release train lock does not match command")
    with train.open("rb") as stream:
        result = run_remote(["promote", args.release_id, args.release_lock_digest], stdin=stream)
    if result.returncode in {124, 255}:
        status = status_value(args.release_id, args.release_lock_digest)
        if (
            isinstance(status, dict)
            and status.get("status") == "promoted"
            and status.get("train_digest") == canonical_digest(value)
        ):
            receipt = status.get("receipt")
            print(json.dumps({
                "ok": True,
                "status": "confirmed-after-timeout",
                "train_digest": canonical_digest(value),
                "promoted_at": (
                    receipt.get("promoted_at") if isinstance(receipt, dict) else None
                ),
            }))
            return 0
        raise SystemExit(75)
    finish(result)
    response = json_stdout(result, "promote")
    if response.get("train_digest") != canonical_digest(value):
        raise PublisherError("promote receipt train digest mismatch")
    return 0


def drop(args: argparse.Namespace) -> int:
    validate_identity(args.release_id, args.release_lock_digest)
    finish(run_remote(["drop-stage", args.release_id, args.release_lock_digest]))
    return 0


def health(_args: argparse.Namespace) -> int:
    finish(run_remote(["health"]))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    stage_parser = sub.add_parser("stage")
    stage_parser.add_argument("--release-id", required=True)
    stage_parser.add_argument("--release-lock-digest", required=True)
    stage_parser.add_argument("--distribution", required=True)
    stage_parser.add_argument("--bundle", required=True)
    stage_parser.set_defaults(handler=stage)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--release-id", required=True)
    status_parser.add_argument("--release-lock-digest", required=True)
    status_parser.add_argument("--validate-json", action="store_true")
    status_parser.set_defaults(handler=status)
    promote_parser = sub.add_parser("promote")
    promote_parser.add_argument("--release-id", required=True)
    promote_parser.add_argument("--release-lock-digest", required=True)
    promote_parser.add_argument("--train", required=True)
    promote_parser.set_defaults(handler=promote)
    drop_parser = sub.add_parser("drop")
    drop_parser.add_argument("--release-id", required=True)
    drop_parser.add_argument("--release-lock-digest", required=True)
    drop_parser.set_defaults(handler=drop)
    health_parser = sub.add_parser("health")
    health_parser.set_defaults(handler=health)
    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except (OSError, ValueError, PublisherError, json.JSONDecodeError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
