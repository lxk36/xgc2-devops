from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
        self.assertNotIn("XGC2_CI_PAT", cleanup.APT_SECRET_NAMES)
        self.assertNotIn("XGC2_RELEASE_ORCHESTRATOR_TOKEN", cleanup.APT_SECRET_NAMES)
        self.assertEqual(
            {
                "APT_REPO_HOST",
                "APT_REPO_KNOWN_HOSTS",
                "APT_REPO_PORT",
                "APT_REPO_SSH_KEY",
                "APT_REPO_USER",
            },
            set(cleanup.APT_SECRET_NAMES),
        )

    def test_environment_names_collects_and_deduplicates_paginated_results(self):
        response = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                [
                    {"environments": [{"name": "production"}]},
                    {
                        "environments": [
                            {"name": "staging"},
                            {"name": "production"},
                        ]
                    },
                ]
            ),
            stderr="",
        )
        with mock.patch.object(cleanup, "run", return_value=response) as run_mock:
            self.assertEqual(
                ["production", "staging"],
                cleanup.environment_names("lxk36/product"),
            )
        run_mock.assert_called_once_with(
            [
                "gh",
                "api",
                "repos/lxk36/product/environments",
                "--paginate",
                "--slurp",
            ]
        )

    def test_central_repository_and_non_apt_delete_are_fail_closed(self):
        with self.assertRaises(ValueError):
            cleanup.secret_names(cleanup.CENTRAL_REPOSITORY)
        with self.assertRaises(ValueError):
            cleanup.environment_names(cleanup.CENTRAL_REPOSITORY)
        with self.assertRaises(ValueError):
            cleanup.delete_secret(
                cleanup.CENTRAL_REPOSITORY, "APT_REPO_SSH_KEY"
            )
        with self.assertRaises(ValueError):
            cleanup.secret_names("LXK36/XGC2-DEVOPS")
        with self.assertRaises(ValueError):
            cleanup.delete_secret("lxk36/product", "XGC2_CI_PAT")

    def test_execute_cleans_all_repository_and_environment_scopes(self):
        repository = "lxk36/product"
        apt_names = set(cleanup.APT_SECRET_NAMES)
        secrets = {
            (repository, None): apt_names | {"XGC2_CI_PAT"},
            (repository, "production"): apt_names | {"UNRELATED"},
            (repository, "staging"): {"UNRELATED"},
        }
        read_scopes: list[tuple[str, str | None]] = []
        deleted: list[tuple[str, str, str | None]] = []

        def fake_secret_names(repo, environment=None):
            read_scopes.append((repo, environment))
            return set(secrets[(repo, environment)])

        def fake_delete(repo, name, environment=None):
            deleted.append((repo, name, environment))
            secrets[(repo, environment)].remove(name)

        argv = [
            str(SCRIPT),
            "--catalog",
            "fresh-products.json",
            "--execute",
            "--confirm",
            cleanup.EXECUTE_CONFIRMATION,
        ]
        with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(
                cleanup, "product_repositories", return_value=[repository]
            ), \
            mock.patch.object(
                cleanup,
                "environment_names",
                return_value=["production", "staging"],
            ) as environment_names_mock, \
            mock.patch.object(
                cleanup, "secret_names", side_effect=fake_secret_names
            ), \
            mock.patch.object(
                cleanup, "delete_secret", side_effect=fake_delete
            ), \
            redirect_stdout(io.StringIO()), \
            redirect_stderr(io.StringIO()):
            self.assertEqual(0, cleanup.main())

        self.assertEqual(10, len(deleted))
        self.assertEqual(apt_names, {name for _, name, _ in deleted})
        self.assertNotIn((repository, "XGC2_CI_PAT", None), deleted)
        self.assertEqual({"XGC2_CI_PAT"}, secrets[(repository, None)])
        self.assertEqual({"UNRELATED"}, secrets[(repository, "production")])
        self.assertEqual({"UNRELATED"}, secrets[(repository, "staging")])
        self.assertEqual(2, environment_names_mock.call_count)
        for scope in (
            (repository, None),
            (repository, "production"),
            (repository, "staging"),
        ):
            self.assertGreaterEqual(read_scopes.count(scope), 2)

    def test_preflight_failure_prevents_every_delete(self):
        repositories = ["lxk36/good", "lxk36/unreadable"]

        def fake_secret_names(repository, environment=None):
            if repository == "lxk36/unreadable":
                raise subprocess.CalledProcessError(1, ["gh", "secret", "list"])
            return {"APT_REPO_HOST"}

        argv = [
            str(SCRIPT),
            "--catalog",
            "fresh-products.json",
            "--execute",
            "--confirm",
            cleanup.EXECUTE_CONFIRMATION,
        ]
        with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(
                cleanup, "product_repositories", return_value=repositories
            ), \
            mock.patch.object(cleanup, "environment_names", return_value=[]), \
            mock.patch.object(
                cleanup, "secret_names", side_effect=fake_secret_names
            ), \
            mock.patch.object(cleanup, "delete_secret") as delete_mock, \
            redirect_stdout(io.StringIO()), \
            redirect_stderr(io.StringIO()):
            self.assertEqual(1, cleanup.main())
        delete_mock.assert_not_called()

    def test_execute_requires_explicit_confirmation(self):
        with mock.patch.object(
            sys,
            "argv",
            [str(SCRIPT), "--catalog", "fresh-products.json", "--execute"],
        ):
            with self.assertRaises(SystemExit) as raised:
                cleanup.main()
        self.assertEqual(raised.exception.code, 2)

    def test_catalog_must_be_explicit(self):
        with mock.patch.object(sys, "argv", [str(SCRIPT)]):
            with self.assertRaises(SystemExit) as raised:
                cleanup.main()
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
