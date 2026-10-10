#!/usr/bin/env python3
"""Unit tests for new-repo provisioning utility."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from new_repo import (
    SecretProvider,
    parse_args,
    parse_github_target,
    reconfigure_local_remotes,
)


class TestArgParsing(unittest.TestCase):
    """Test CLI argument parsing and default values."""

    def test_default_arguments(self) -> None:
        import sys

        sys.argv = ["new-repo", "sample-project"]
        args = parse_args()
        self.assertEqual(args.name, "sample-project")
        self.assertFalse(args.public)
        self.assertEqual(args.license, "UNLICENSE")
        self.assertIsNone(args.template)
        self.assertFalse(args.no_mirror)
        self.assertFalse(args.no_clone)
        self.assertIsNone(args.migrate)

    def test_migrate_arguments(self) -> None:
        import sys

        sys.argv = ["new-repo", "--migrate"]
        args = parse_args()
        self.assertEqual(args.migrate, "")

        sys.argv = ["new-repo", "--migrate", "https://github.com/octocat/Hello-World"]
        args = parse_args()
        self.assertEqual(args.migrate, "https://github.com/octocat/Hello-World")

    def test_omitted_name_defaults_to_none(self) -> None:
        import sys

        sys.argv = ["new-repo"]
        args = parse_args()
        self.assertIsNone(args.name)

    def test_custom_flags(self) -> None:
        import sys

        sys.argv = [
            "new-repo",
            "open-api",
            "--public",
            "-d",
            "My public API",
            "-l",
            "MIT",
            "-t",
            "template-fastapi",
            "-M",
            "--no-clone",
        ]
        args = parse_args()
        self.assertEqual(args.name, "open-api")
        self.assertTrue(args.public)
        self.assertEqual(args.description, "My public API")
        self.assertEqual(args.license, "MIT")
        self.assertEqual(args.template, "template-fastapi")
        self.assertTrue(args.no_mirror)
        self.assertTrue(args.no_clone)
        self.assertIsNone(args.migrate)


class TestDotenvLoader(unittest.TestCase):
    """Test parsing of local .env files without external dependencies."""

    def test_load_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dotenv_file = Path(tmpdir) / ".env"
            dotenv_file.write_text(
                "# Sample configuration\n"
                "GITEA_TOKEN=test-token-123\n"
                'GITHUB_TOKEN="gh-token-456"\n'
                "GITEA_USER='custom-user'\n"
                "\n"
                "INVALID_LINE\n"
            )
            loaded = SecretProvider._load_dotenv(dotenv_file)
            self.assertEqual(loaded.get("GITEA_TOKEN"), "test-token-123")
            self.assertEqual(loaded.get("GITHUB_TOKEN"), "gh-token-456")
            self.assertEqual(loaded.get("GITEA_USER"), "custom-user")


class TestTargetParsing(unittest.TestCase):
    """Test parsing of GitHub target URLs, SSH remotes, and shorthands."""

    def test_parse_https_url(self) -> None:
        dummy_dir = Path("/nonexistent/dir")
        name, owner, url = parse_github_target(
            "https://github.com/octocat/Hello-World", dummy_dir
        )
        self.assertEqual(name, "Hello-World")
        self.assertEqual(owner, "octocat")
        self.assertEqual(url, "https://github.com/octocat/Hello-World.git")

    def test_parse_https_url_with_git_suffix(self) -> None:
        dummy_dir = Path("/nonexistent/dir")
        name, owner, url = parse_github_target(
            "https://github.com/octocat/Hello-World.git", dummy_dir
        )
        self.assertEqual(name, "Hello-World")
        self.assertEqual(owner, "octocat")
        self.assertEqual(url, "https://github.com/octocat/Hello-World.git")

    def test_parse_ssh_url(self) -> None:
        dummy_dir = Path("/nonexistent/dir")
        name, owner, url = parse_github_target(
            "git@github.com:octocat/Hello-World.git", dummy_dir
        )
        self.assertEqual(name, "Hello-World")
        self.assertEqual(owner, "octocat")
        self.assertEqual(url, "https://github.com/octocat/Hello-World.git")

    def test_parse_shorthand(self) -> None:
        dummy_dir = Path("/nonexistent/dir")
        name, owner, url = parse_github_target("octocat/Hello-World", dummy_dir)
        self.assertEqual(name, "Hello-World")
        self.assertEqual(owner, "octocat")
        self.assertEqual(url, "https://github.com/octocat/Hello-World.git")

    def test_parse_invalid_target_raises_value_error(self) -> None:
        dummy_dir = Path("/nonexistent/dir")
        with self.assertRaises(ValueError):
            parse_github_target("not-a-repo", dummy_dir)

    def test_auto_detect_outside_git_repo_raises_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, self.assertRaises(RuntimeError):
            parse_github_target(None, Path(tmpdir))

    def test_auto_detect_inside_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    "git@github.com:myorg/cool-project.git",
                ],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            name, owner, url = parse_github_target("", repo_path)
            self.assertEqual(name, "cool-project")
            self.assertEqual(owner, "myorg")
            self.assertEqual(url, "https://github.com/myorg/cool-project.git")


class TestReconfigureLocalRemotes(unittest.TestCase):
    """Test reconfiguring local git remotes on migration."""

    def test_reconfigure_remotes_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/octocat/Hello-World.git",
                ],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            # Create a dummy commit
            (repo_path / "README.md").write_text("# Hello World\n")
            subprocess.run(
                ["git", "add", "."], cwd=repo_path, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "Initial commit"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )

            # Execute reconfiguration
            gitea_target = "git@gitea.local:kiskaadee/Hello-World.git"
            reconfigure_local_remotes(repo_path, gitea_target)

            # Check that origin is now gitea
            origin_url = subprocess.check_output(
                ["git", "remote", "get-url", "origin"],
                cwd=repo_path,
                text=True,
            ).strip()
            self.assertEqual(origin_url, gitea_target)

            # Check that github is now previous origin
            github_url = subprocess.check_output(
                ["git", "remote", "get-url", "github"],
                cwd=repo_path,
                text=True,
            ).strip()
            self.assertEqual(github_url, "https://github.com/octocat/Hello-World.git")


if __name__ == "__main__":
    unittest.main()
