from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


resolver = load_script("resolve-product-metadata-submodules.py")


class MetadataSubmoduleFixture:
    def __init__(self, root: Path):
        self.root = root
        self.sources = [
            (
                "xgc2-gazebo-sim-camera",
                "products/ros1/simulator/gazebo-sim/camera/.xgc2/product.yml",
            ),
            (
                "xgc2-camera-calibration-ros1",
                "products/ros1/perception/camera-calibration/.xgc2/product.yml",
            ),
            (
                "xgc2-gazebo-sim",
                "products/ros1/simulator/gazebo-sim/.xgc2/product.yml",
            ),
        ]
        self.gitlinks = (
            "products/ros1/perception/camera-calibration",
            "products/ros1/simulator/gazebo-sim",
        )
        self.run("init", "-q")
        self.run("config", "user.name", "Test")
        self.run("config", "user.email", "test@example.com")
        (root / "seed").write_text("seed\n", encoding="utf-8")
        self.run("add", "seed")
        self.run("commit", "-qm", "seed")
        self.gitlink_object = self.run("rev-parse", "HEAD").stdout.strip()
        self.write_gitmodules(self.gitlinks)
        self.write_catalog(self.sources)
        self.run("add", ".gitmodules", "catalog/generated/products.json")
        for gitlink in self.gitlinks:
            self.run(
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{self.gitlink_object},{gitlink}",
            )
        self.run("commit", "-qm", "catalog and gitlinks")

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def write_gitmodules(self, gitlinks: tuple[str, ...]) -> None:
        lines: list[str] = []
        for index, gitlink in enumerate(reversed(gitlinks), start=1):
            lines.extend(
                [
                    f'[submodule "product-{index}"]',
                    f"\tpath = {gitlink}",
                    f"\turl = https://example.invalid/product-{index}.git",
                ]
            )
        (self.root / ".gitmodules").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def write_catalog(self, sources: list[tuple[str, str]], *, stage: bool = False) -> None:
        path = self.root / "catalog" / "generated" / "products.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "xgc2.catalog.v1",
                    "products": [
                        {"id": product_id, "_source": source}
                        for product_id, source in sources
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if stage:
            self.run("add", "catalog/generated/products.json")

    def materialize_sources(self) -> None:
        for _product_id, source in self.sources:
            path = self.root / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("schema: xgc2.product.v1\n", encoding="utf-8")


class MetadataSubmoduleResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = MetadataSubmoduleFixture(self.root)

    def test_catalog_sources_resolve_to_sorted_root_gitlinks(self):
        resolution = resolver.resolve(self.root)

        self.assertEqual(resolution.submodules, self.fixture.gitlinks)
        by_id = {item.product_id: item.gitlink for item in resolution.sources}
        self.assertEqual(
            by_id["xgc2-gazebo-sim-camera"],
            "products/ros1/simulator/gazebo-sim",
        )
        self.assertEqual(
            by_id["xgc2-camera-calibration-ros1"],
            "products/ros1/perception/camera-calibration",
        )

    def test_resolver_reads_tracked_catalog_not_unstaged_worktree_content(self):
        catalog = self.root / "catalog" / "generated" / "products.json"
        catalog.write_text("not json\n", encoding="utf-8")

        resolution = resolver.resolve(self.root)

        self.assertEqual(resolution.submodules, self.fixture.gitlinks)

    def test_untracked_catalog_fails_closed(self):
        self.fixture.run("rm", "--cached", "catalog/generated/products.json")

        with self.assertRaisesRegex(resolver.ResolutionError, "ls-files.*failed"):
            resolver.resolve(self.root)

    def test_invalid_or_empty_catalog_fails_closed(self):
        catalog = self.root / "catalog" / "generated" / "products.json"
        catalog.write_text('{"schema":"xgc2.catalog.v1","products":[]}\n', encoding="utf-8")
        self.fixture.run("add", "catalog/generated/products.json")

        with self.assertRaisesRegex(resolver.ResolutionError, "products must be non-empty"):
            resolver.resolve(self.root)

    def test_catalog_source_without_containing_gitlink_fails_closed(self):
        sources = [
            (
                "xgc2-unmapped",
                "products/ros1/perception/unmapped/.xgc2/product.yml",
            )
        ]
        self.fixture.write_catalog(sources, stage=True)

        with self.assertRaisesRegex(resolver.ResolutionError, "not contained by a tracked gitlink"):
            resolver.resolve(self.root)

    def test_catalog_source_path_must_be_normalized_and_inside_products(self):
        sources = [
            (
                "xgc2-escape",
                "products/ros1/../escape/.xgc2/product.yml",
            )
        ]
        self.fixture.write_catalog(sources, stage=True)

        with self.assertRaisesRegex(resolver.ResolutionError, "normalized products"):
            resolver.resolve(self.root)

    def test_gitlink_without_gitmodules_path_fails_closed(self):
        self.fixture.write_gitmodules((self.fixture.gitlinks[0],))
        self.fixture.run("add", ".gitmodules")

        with self.assertRaisesRegex(resolver.ResolutionError, "no matching .gitmodules path"):
            resolver.resolve(self.root)

    def test_checkout_verification_lists_every_missing_product_source(self):
        resolution = resolver.resolve(self.root)

        with self.assertRaises(resolver.ResolutionError) as context:
            resolver.verify_checkout(self.root, resolution)
        message = str(context.exception)
        self.assertIn("xgc2-gazebo-sim-camera", message)
        self.assertIn("xgc2-camera-calibration-ros1", message)

        self.fixture.materialize_sources()
        resolver.verify_checkout(self.root, resolution)


class MetadataSubmoduleCheckoutScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = MetadataSubmoduleFixture(self.root)
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copy2(
            ROOT / "scripts" / "resolve-product-metadata-submodules.py",
            scripts / "resolve-product-metadata-submodules.py",
        )
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self.log = self.root / "git.log"
        real_git = shutil.which("git")
        assert real_git
        fake_git = self.fake_bin / "git"
        source_commands = "\n".join(
            f'mkdir -p "${{PWD}}/{Path(source).parent.as_posix()}"; '
            f': > "${{PWD}}/{source}"'
            for _product_id, source in self.fixture.sources
        )
        fake_git.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ \"${1:-}\" == submodule && \"${2:-}\" == update ]]; then\n"
            "  printf '%s\\n' \"$*\" >> \"${FAKE_GIT_LOG}\"\n"
            "  if [[ -n \"${FAKE_GIT_FAIL_PATH:-}\" ]] &&\n"
            "     [[ \"${*: -1}\" == \"${FAKE_GIT_FAIL_PATH}\" ]]; then\n"
            "    exit 1\n"
            "  fi\n"
            f"  {source_commands}\n"
            "  exit 0\n"
            "fi\n"
            f'exec "{real_git}" "$@"\n',
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        self.environment = {
            **os.environ,
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
            "HOME": str(self.root / "home"),
            "FAKE_GIT_LOG": str(self.log),
            "GH_TOKEN": "test-token",
        }
        (self.root / "home").mkdir()

    def run_checkout(self, **extra_environment: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / "checkout-product-metadata-submodules.sh")],
            cwd=self.root,
            env={**self.environment, **extra_environment},
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_script_checks_out_each_catalog_root_in_deterministic_order(self):
        result = self.run_checkout()

        self.assertEqual(result.returncode, 0, result.stderr)
        updates = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            [line.rsplit(" ", 1)[-1] for line in updates],
            list(self.fixture.gitlinks),
        )
        git_config = (self.root / "home" / ".gitconfig").read_text(encoding="utf-8")
        self.assertIn("git@github.com:", git_config)

    def test_script_stops_on_first_checkout_failure(self):
        result = self.run_checkout(FAKE_GIT_FAIL_PATH=self.fixture.gitlinks[0])

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Submodule checkout failed", result.stdout)
        updates = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(updates), 1)

    def test_script_rejects_missing_tracked_catalog_before_credentials_or_checkout(self):
        self.fixture.run("rm", "--cached", "catalog/generated/products.json")

        result = self.run_checkout()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.root / "home" / ".gitconfig").exists())


if __name__ == "__main__":
    unittest.main()
