#!/usr/bin/env python3
"""Unit tests for new-repo provisioning utility."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from new_repo import (
    GiteaClient,
    GitHubClient,
    SecretProvider,
    determine_target,
    normalize_license,
    parse_args,
    parse_github_target,
    reconfigure_local_remotes,
    resolve_name_and_org,
)


class TestArgParsing(unittest.TestCase):
    """Test CLI argument parsing and default values."""

    def test_default_arguments(self) -> None:
        import sys

        sys.argv = ["new-repo", "sample-project"]
        args = parse_args()
        self.assertEqual(args.name, "sample-project")
        self.assertIsNone(args.org)
        self.assertIsNone(args.github_org)
        self.assertFalse(args.private)
        self.assertEqual(args.license, "Unlicense")
        self.assertIsNone(args.template)
        self.assertFalse(args.no_mirror)
        self.assertFalse(args.no_clone)
        self.assertIsNone(args.as_persona)
        self.assertIsNone(args.migrate)

    def test_as_persona_argument(self) -> None:
        import sys

        sys.argv = ["new-repo", "sample-project", "--as", "anneux"]
        args = parse_args()
        self.assertEqual(args.as_persona, "anneux")

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
            "-o",
            "homelab",
            "--github-org",
            "homelab-gh",
            "--private",
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
        self.assertEqual(args.org, "homelab")
        self.assertEqual(args.github_org, "homelab-gh")
        self.assertTrue(args.private)
        self.assertEqual(args.description, "My public API")
        self.assertEqual(args.license, "MIT")
        self.assertEqual(args.template, "template-fastapi")
        self.assertTrue(args.no_mirror)
        self.assertTrue(args.no_clone)
        self.assertIsNone(args.migrate)


class TestLicenseNormalization(unittest.TestCase):
    """Test license identifier normalization against Gitea's registry."""

    def test_unlicense_casing_variants(self) -> None:
        self.assertEqual(normalize_license("UNLICENSE"), "Unlicense")
        self.assertEqual(normalize_license("unlicense"), "Unlicense")
        self.assertEqual(normalize_license("Unlicense"), "Unlicense")

    def test_common_licenses_case_insensitivity(self) -> None:
        self.assertEqual(normalize_license("mit"), "MIT")
        self.assertEqual(normalize_license("MIT"), "MIT")
        self.assertEqual(normalize_license("apache-2.0"), "Apache-2.0")
        self.assertEqual(normalize_license("APACHE-2.0"), "Apache-2.0")
        self.assertEqual(normalize_license("gpl-3.0"), "GPL-3.0")
        self.assertEqual(normalize_license("bsd-3-clause"), "BSD-3-Clause")

    def test_empty_and_whitespace_licenses(self) -> None:
        self.assertEqual(normalize_license(""), "")
        self.assertEqual(normalize_license("   "), "")
        self.assertEqual(normalize_license(None), "")

    def test_unrecognized_license_passthrough(self) -> None:
        self.assertEqual(normalize_license("Custom-Proprietary"), "Custom-Proprietary")

    def test_gitea_client_create_repo_normalizes_license(self) -> None:
        client = GiteaClient("https://gitea.local", "kiskaadee", "secret-token")
        with patch.object(client, "_request", return_value={}) as mock_req:
            client.create_repo("my-repo", license_name="UNLICENSE")
            mock_req.assert_called_once()
            payload = mock_req.call_args[0][2]
            self.assertEqual(payload["license"], "Unlicense")

        with patch.object(client, "_request", return_value={}) as mock_req:
            client.create_repo("my-repo-no-license", license_name="")
            mock_req.assert_called_once()
            payload = mock_req.call_args[0][2]
            self.assertNotIn("license", payload)


class TestOrganizationAndTargetResolution(unittest.TestCase):
    """Test resolution of repository names, org/repo shorthands, and flat local paths."""

    def test_resolve_name_and_org_variants(self) -> None:
        self.assertEqual(resolve_name_and_org("my-repo", None), ("my-repo", None))
        self.assertEqual(
            resolve_name_and_org("my-repo", "homelab"), ("my-repo", "homelab")
        )
        self.assertEqual(
            resolve_name_and_org("homelab/my-repo", None), ("my-repo", "homelab")
        )
        self.assertEqual(
            resolve_name_and_org("homelab/my-repo", "homelab"), ("my-repo", "homelab")
        )
        self.assertEqual(resolve_name_and_org(None, "homelab"), (None, "homelab"))
        self.assertEqual(resolve_name_and_org(".", "homelab"), (None, "homelab"))

    def test_resolve_name_and_org_conflicts_and_invalid(self) -> None:
        with self.assertRaises(ValueError):
            resolve_name_and_org("org1/my-repo", "org2")
        with self.assertRaises(ValueError):
            resolve_name_and_org("a/b/c", None)
        with self.assertRaises(ValueError):
            resolve_name_and_org("/leading-slash", None)
        with self.assertRaises(ValueError):
            resolve_name_and_org("trailing-slash/", None)

    def test_determine_target_flat_directory_for_org_shorthand(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path(tmpdir)
            repo_name, target_dir, is_existing_git, target_org = determine_target(
                "homelab/core", None, cwd=cwd
            )
            self.assertEqual(repo_name, "core")
            self.assertEqual(target_dir, cwd / "core")
            self.assertFalse(is_existing_git)
            self.assertEqual(target_org, "homelab")

    def test_determine_target_inside_matching_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "core"
            repo_dir.mkdir()
            (repo_dir / ".git").mkdir()
            repo_name, target_dir, is_existing_git, target_org = determine_target(
                "homelab/core", None, cwd=repo_dir
            )
            self.assertEqual(repo_name, "core")
            self.assertEqual(target_dir, repo_dir)
            self.assertTrue(is_existing_git)
            self.assertEqual(target_org, "homelab")


class TestApiClientOrganizationRouting(unittest.TestCase):
    """Test GiteaClient and GitHubClient organization endpoint routing."""

    def test_gitea_create_repo_personal_vs_org(self) -> None:
        client = GiteaClient("https://gitea.local", "kiskaadee", "secret-token")
        with patch.object(client, "_request", return_value={}) as mock_req:
            client.create_repo("personal-repo")
            mock_req.assert_called_once()
            self.assertEqual(mock_req.call_args[0][1], "/api/v1/user/repos")

        with patch.object(client, "_request", return_value={}) as mock_req:
            client.create_repo("org-repo", org="homelab")
            mock_req.assert_called_once()
            self.assertEqual(mock_req.call_args[0][1], "/api/v1/orgs/homelab/repos")

    def test_gitea_ensure_org_creates_when_missing(self) -> None:
        client = GiteaClient("https://gitea.local", "kiskaadee", "secret-token")
        with (
            patch.object(client, "org_exists", return_value=False) as mock_exists,
            patch.object(client, "create_org", return_value={}) as mock_create,
        ):
            created = client.ensure_org("homelab", private=True)
            self.assertTrue(created)
            mock_exists.assert_called_once_with("homelab")
            mock_create.assert_called_once_with("homelab", visibility="private")

    def test_gitea_ensure_org_skips_when_existing(self) -> None:
        client = GiteaClient("https://gitea.local", "kiskaadee", "secret-token")
        with (
            patch.object(client, "org_exists", return_value=True) as mock_exists,
            patch.object(client, "create_org", return_value={}) as mock_create,
        ):
            created = client.ensure_org("homelab", private=True)
            self.assertFalse(created)
            mock_exists.assert_called_once_with("homelab")
            mock_create.assert_not_called()

    def test_gitea_push_mirror_and_template_and_migrate_owner(self) -> None:
        client = GiteaClient("https://gitea.local", "kiskaadee", "secret-token")
        with patch.object(client, "_request", return_value={}) as mock_req:
            client.create_push_mirror(
                "core",
                "https://github.com/kiskaadee/core.git",
                "kiskaadee",
                "gh-token",
                owner="homelab",
            )
            self.assertEqual(
                mock_req.call_args[0][1], "/api/v1/repos/homelab/core/push_mirrors"
            )

        with patch.object(client, "_request", return_value={}) as mock_req:
            client.generate_from_template(
                "homelab", "tmpl", "new-service", owner="homelab"
            )
            self.assertEqual(mock_req.call_args[0][2]["owner"], "homelab")

        with patch.object(client, "_request", return_value={}) as mock_req:
            client.migrate_repo(
                "https://github.com/kiskaadee/core.git",
                "core",
                "gh-token",
                repo_owner="homelab",
            )
            self.assertEqual(mock_req.call_args[0][2]["repo_owner"], "homelab")

    def test_github_create_empty_repo_personal_vs_org(self) -> None:
        gh_client = GitHubClient("kiskaadee", "gh-token")
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"{}"
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            gh_client.create_empty_repo("core")
            req_personal = mock_urlopen.call_args[0][0]
            self.assertEqual(req_personal.full_url, "https://api.github.com/user/repos")

            gh_client.create_empty_repo("core", org="homelab-gh")
            req_org = mock_urlopen.call_args[0][0]
            self.assertEqual(
                req_org.full_url, "https://api.github.com/orgs/homelab-gh/repos"
            )


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


class TestTeaLoginParserAndPersonaResolution(unittest.TestCase):
    """Test YAML block parsing of tea logins and persona identity resolution."""

    def setUp(self) -> None:
        self.sample_tea_yaml = (
            "logins:\n"
            "- name: kiskaadee\n"
            "  url: https://gitea.roadtotech.me\n"
            "  token: token-kiskaadee-123\n"
            "  default: true\n"
            "  user: kiskaadee\n"
            "- name: anneux\n"
            "  url: https://gitea.roadtotech.me\n"
            "  token: token-anneux-456\n"
            "  default: false\n"
            "  ssh_key: /path/to/anne_key\n"
            "  user: anneux\n"
            "preferences:\n"
            "  editor: false\n"
        )

    def test_parse_tea_logins(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.yml"
            cfg_path.write_text(self.sample_tea_yaml)
            logins = SecretProvider._parse_tea_logins(cfg_path)
            self.assertEqual(len(logins), 2)
            self.assertEqual(logins[0]["name"], "kiskaadee")
            self.assertTrue(logins[0]["default"])
            self.assertEqual(logins[1]["name"], "anneux")
            self.assertFalse(logins[1]["default"])
            self.assertEqual(logins[1]["ssh_key"], "/path/to/anne_key")

    def test_extract_tea_login_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tea_dir = Path(tmpdir) / ".config" / "tea"
            tea_dir.mkdir(parents=True)
            (tea_dir / "config.yml").write_text(self.sample_tea_yaml)
            with patch("pathlib.Path.home", return_value=Path(tmpdir)):
                token, url, user, ssh_key = SecretProvider._extract_tea_login(target_persona=None)
                self.assertEqual(user, "kiskaadee")
                self.assertEqual(token, "token-kiskaadee-123")
                self.assertIsNone(ssh_key)

    def test_extract_tea_login_by_persona(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tea_dir = Path(tmpdir) / ".config" / "tea"
            tea_dir.mkdir(parents=True)
            (tea_dir / "config.yml").write_text(self.sample_tea_yaml)
            with patch("pathlib.Path.home", return_value=Path(tmpdir)):
                token, url, user, ssh_key = SecretProvider._extract_tea_login(target_persona="anneux")
                self.assertEqual(user, "anneux")
                self.assertEqual(token, "token-anneux-456")
                self.assertEqual(ssh_key, "/path/to/anne_key")

    def test_extract_tea_login_missing_persona_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tea_dir = Path(tmpdir) / ".config" / "tea"
            tea_dir.mkdir(parents=True)
            (tea_dir / "config.yml").write_text(self.sample_tea_yaml)
            with patch("pathlib.Path.home", return_value=Path(tmpdir)), self.assertRaises(SystemExit):
                SecretProvider._extract_tea_login(target_persona="unknown-agent")

    def test_gitea_client_get_authenticated_user(self) -> None:
        client = GiteaClient("https://gitea.local", "kiskaadee", "secret-token")
        with patch.object(client, "_request", return_value={"username": "kiskaadee"}):
            self.assertEqual(client.get_authenticated_user(), "kiskaadee")


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
