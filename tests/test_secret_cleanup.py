from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cleanup-product-apt-secrets.py"
SPEC = importlib.util.spec_from_file_location("cleanup_product_apt_secrets", SCRIPT)
cleanup = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = cleanup
SPEC.loader.exec_module(cleanup)


class SecretCleanupTests(unittest.TestCase):
    def test_normalize_github_repository(self):
        self.assertEqual(
            "lxk36/example",
            cleanup.normalize_github_repository("git@github.com:lxk36/example.git"),
        )
        self.assertEqual(
            "lxk36/example",
            cleanup.normalize_github_repository("https://github.com/lxk36/example.git"),
        )

    def test_repositories_are_deduplicated_and_mixed_apt_is_included(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("one", "mixed"):
                source = root / name / ".xgc2"
                source.mkdir(parents=True)
            catalog = {
                "products": [
                    {
                        "id": "one",
                        "kind": "toolchain-apt",
                        "apt": {"packages": ["one"]},
                        "release": {"repository": "lxk36/shared"},
                        "_source": "one/.xgc2/product.yml",
                    },
                    {
                        "id": "mixed",
                        "kind": "mixed",
                        "apt": {"install": ["mixed"]},
                        "release": {"repository": "lxk36/shared"},
                        "_source": "mixed/.xgc2/product.yml",
                    },
                    {
                        "id": "not-apt",
                        "kind": "docker",
                        "_source": "unused/.xgc2/product.yml",
                    },
                ]
            }
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            self.assertEqual(
                ["lxk36/shared"], cleanup.product_repositories(root, catalog_path)
            )

    def test_delete_allowlist_does_not_include_non_apt_secrets(self):
        self.assertNotIn("XGC2_CI_PAT", cleanup.REPOSITORY_SECRET_NAMES)
        self.assertNotIn("XGC2_RELEASE_ORCHESTRATOR_TOKEN", cleanup.REPOSITORY_SECRET_NAMES)
        self.assertEqual(
            {
                "APT_REPO_HOST",
                "APT_REPO_KNOWN_HOSTS",
                "APT_REPO_PORT",
                "APT_REPO_SSH_KEY",
            },
            set(cleanup.REPOSITORY_SECRET_NAMES),
        )

    def test_execute_requires_explicit_confirmation(self):
        with mock.patch.object(sys, "argv", [str(SCRIPT), "--execute"]):
            with self.assertRaises(SystemExit) as raised:
                cleanup.main()
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
