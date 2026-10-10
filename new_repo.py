#!/usr/bin/env python3
"""new-repo: Automated repository provisioning across Gitea and GitHub push mirrors.

This tool provisions a sovereign primary repository on self-hosted Gitea (with license,
auto-init, or template), provisions an empty offsite backup repository on GitHub,
registers Gitea automated push mirroring with in-memory authentication, and
clones the working tree locally.

Architecture & Design:
- Primary Configuration Contract: 12-Factor Environment Variables (`GITEA_TOKEN`, `GITHUB_TOKEN`).
- Workstation Secret Fallback: Mozilla SOPS / Age RAM decryption when environment variables are unset.
- Zero External Dependencies: Built strictly with Python 3 standard library modules.
- Type Safety & Standards: Full static type annotations (Pyright) and Ruff formatted.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ==============================================================================
# Configuration & Credentials Management
# ==============================================================================


@dataclass(frozen=True)
class Credentials:
    """Resolved credentials and endpoint targets for Gitea and GitHub APIs.

    Attributes:
        gitea_url: Base HTTPS/HTTP URL of the Gitea instance (e.g. 'https://gitea.roadtotech.me').
        gitea_user: Gitea username owning the primary repository.
        gitea_token: Gitea Personal Access Token with repository read/write permissions.
        github_user: GitHub username owning the offsite backup mirror repository.
        github_token: GitHub Personal Access Token with repository creation permissions.
    """

    gitea_url: str
    gitea_user: str
    gitea_token: str
    github_user: str
    github_token: str


class SecretProvider:
    """Resolves API credentials using a multi-tier fallback strategy.

    Hierarchy:
        1. Process environment variables (GITEA_TOKEN, GITHUB_TOKEN, etc.).
        2. Local .env file in the current working directory or repository root.
        3. Laptop SOPS encrypted secrets file (~/Config/secrets.yaml).
        4. Legacy fallbacks (tea CLI config for Gitea, Homelab SOPS file for GitHub).
    """

    @staticmethod
    def _load_dotenv(dotenv_path: Path) -> dict[str, str]:
        """Parse a simple .env file without external dependencies.

        Lines starting with '#' or lacking '=' are ignored. Quotes around values
        are stripped. Variables already present in `os.environ` take precedence.
        """
        env_vars: dict[str, str] = {}
        if not dotenv_path.is_file():
            return env_vars

        try:
            with open(dotenv_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and k not in os.environ:
                        env_vars[k] = v
        except OSError:
            pass
        return env_vars

    @staticmethod
    def _extract_sops_key(key_path: list[str], secrets_file: Path) -> str | None:
        """Extract a single key from a SOPS-encrypted file directly into process memory.

        Uses `sops -d --extract` to retrieve only the requested key string without
        decrypting the entire file to disk or leaking plaintext into command-line arguments.
        """
        if not secrets_file.is_file():
            return None

        extract_arg = "".join(f'["{k}"]' for k in key_path)
        try:
            res = subprocess.run(
                ["sops", "-d", "--extract", extract_arg, str(secrets_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except FileNotFoundError:
            return None
        return None

    @staticmethod
    def _extract_tea_login() -> tuple[str | None, str | None, str | None]:
        """Extract Gitea credentials from ~/.config/tea/config.yml if the tea CLI is configured.

        Returns:
            A tuple of (token, url, user), or None for missing values.
        """
        tea_cfg = Path.home() / ".config" / "tea" / "config.yml"
        if not tea_cfg.is_file():
            return None, None, None

        token: str | None = None
        url: str | None = None
        user: str | None = None

        try:
            with open(tea_cfg, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("token:"):
                        token = line.split(":", 1)[1].strip().strip("\"'")
                    elif line.startswith("url:"):
                        url = line.split(":", 1)[1].strip().strip("\"'")
                    elif line.startswith("user:"):
                        user = line.split(":", 1)[1].strip().strip("\"'")
            return token, url, user
        except OSError:
            return None, None, None

    @classmethod
    def resolve(cls) -> Credentials:
        """Resolve all necessary credentials following the multi-tier hierarchy.

        Raises:
            SystemExit: If Gitea or GitHub tokens cannot be resolved from any source.
        """
        # Load local .env if present
        dotenv = cls._load_dotenv(Path.cwd() / ".env")

        def get_val(key: str) -> str | None:
            return os.environ.get(key) or dotenv.get(key)

        config_secrets = Path.home() / "Config" / "secrets.yaml"
        homelab_secrets = (
            Path(os.environ["HOMELAB_SECRETS_FILE"])
            if "HOMELAB_SECRETS_FILE" in os.environ
            else Path.home() / "Homelab" / "Core" / "nixos" / "secrets.yaml"
        )

        # 1. Gitea resolution
        tea_token, tea_url, tea_user = cls._extract_tea_login()

        gitea_url = get_val("GITEA_URL") or tea_url or "https://gitea.roadtotech.me"
        gitea_user = get_val("GITEA_USER") or tea_user or "kiskaadee"
        gitea_token = (
            get_val("GITEA_TOKEN")
            or cls._extract_sops_key(["gitea", "api_token"], config_secrets)
            or tea_token
        )

        if not gitea_token:
            print(
                "Error: Unable to resolve Gitea API token.\n"
                "To provide credentials, either:\n"
                "  1. Export GITEA_TOKEN in your environment (or define in a .env file)\n"
                "  2. Add 'gitea.api_token' to ~/Config/secrets.yaml via SOPS\n"
                "  3. Configure tea CLI via 'tea login add'\n"
                "\n"
                "Note: In Gitea, generate a token at: User Settings -> Applications -> Manage Access Tokens.",
                file=sys.stderr,
            )
            sys.exit(1)

        # 2. GitHub resolution
        github_user = get_val("GITHUB_USER") or "kiskaadee"
        github_token = (
            get_val("GITHUB_TOKEN")
            or cls._extract_sops_key(["system", "gh_repo_token"], config_secrets)
            or cls._extract_sops_key(["system", "gh_repo_token"], homelab_secrets)
        )

        if not github_token:
            print(
                "Error: Unable to resolve GitHub API token.\n"
                "To provide credentials, either:\n"
                "  1. Export GITHUB_TOKEN in your environment (or define in a .env file)\n"
                "  2. Add 'system.gh_repo_token' to ~/Config/secrets.yaml via SOPS\n",
                file=sys.stderr,
            )
            sys.exit(1)

        return Credentials(
            gitea_url=gitea_url,
            gitea_user=gitea_user,
            gitea_token=gitea_token,
            github_user=github_user,
            github_token=github_token,
        )


# ==============================================================================
# API Clients
# ==============================================================================


class GiteaClient:
    """REST API Client for self-hosted Gitea instances.

    Handles repo creation, template repository generation, and push mirror configuration.
    """

    def __init__(self, base_url: str, user: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.token = token

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send an authenticated JSON HTTP request to the Gitea API."""
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None

        req = urllib.request.Request(
            url=url,
            data=data,
            headers={
                "Authorization": f"token {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )

        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8", errors="replace")
            try:
                err_data = json.loads(raw_err)
                msg = err_data.get("message", raw_err)
            except (json.JSONDecodeError, UnicodeDecodeError):
                msg = raw_err
            raise RuntimeError(f"Gitea API error ({e.code} {e.reason}): {msg}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Gitea network error: {e.reason}") from e

    def create_repo(
        self,
        name: str,
        private: bool = True,
        description: str = "",
        auto_init: bool = True,
        license_name: str = "UNLICENSE",
    ) -> dict[str, Any]:
        """Create a new repository under the authenticated user account."""
        payload: dict[str, Any] = {
            "name": name,
            "private": private,
            "description": description,
            "auto_init": auto_init,
            "default_branch": "main",
        }
        if license_name:
            payload["license"] = license_name

        return self._request("POST", "/api/v1/user/repos", payload)

    def generate_from_template(
        self,
        template_owner: str,
        template_name: str,
        name: str,
        private: bool = True,
        description: str = "",
    ) -> dict[str, Any]:
        """Generate a new repository from an existing Gitea template repository."""
        payload = {
            "owner": self.user,
            "name": name,
            "private": private,
            "description": description,
        }
        path = f"/api/v1/repos/{template_owner}/{template_name}/generate"
        return self._request("POST", path, payload)

    def create_push_mirror(
        self,
        repo_name: str,
        remote_address: str,
        remote_username: str,
        remote_password: str,
        sync_on_commit: bool = True,
        interval: str = "8h",
    ) -> dict[str, Any]:
        """Register a push mirror for a Gitea repository to an external Git target."""
        payload = {
            "remote_address": remote_address,
            "remote_username": remote_username,
            "remote_password": remote_password,
            "sync_on_commit": sync_on_commit,
            "interval": interval,
        }
        path = f"/api/v1/repos/{self.user}/{repo_name}/push_mirrors"
        return self._request("POST", path, payload)


class GitHubClient:
    """REST API Client for GitHub repository management."""

    def __init__(self, user: str, token: str) -> None:
        self.user = user
        self.token = token
        self.api_url = "https://api.github.com"

    def create_empty_repo(
        self,
        name: str,
        private: bool = True,
        description: str = "",
    ) -> dict[str, Any]:
        """Create an empty target repository on GitHub with auto_init=False.

        Crucial invariant: The repository MUST be empty (no README, no license)
        so that Gitea's push mirror can push all refs without fast-forward conflicts.
        """
        payload = {
            "name": name,
            "private": private,
            "description": description,
            "auto_init": False,
        }
        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url=f"{self.api_url}/user/repos",
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8", errors="replace")
            try:
                err_data = json.loads(raw_err)
                msg = err_data.get("message", raw_err)
            except (json.JSONDecodeError, UnicodeDecodeError):
                msg = raw_err
            raise RuntimeError(f"GitHub API error ({e.code} {e.reason}): {msg}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"GitHub network error: {e.reason}") from e


# ==============================================================================
# Local Git Orchestration
# ==============================================================================


def clone_locally(gitea_host: str, user: str, repo_name: str, target_dir: Path) -> None:
    """Clones the newly created repository into the target directory."""
    if target_dir.exists():
        print(f"⚠️  Target directory '{target_dir}' already exists. Skipping clone.")
        return

    clone_url = f"git@{gitea_host}:{user}/{repo_name}.git"
    print(f"📦 Cloning repository locally into '{target_dir.name}'...")
    try:
        subprocess.run(
            ["git", "clone", clone_url, str(target_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        print(f"✅ Cloned successfully to {target_dir}")
    except subprocess.CalledProcessError as e:
        print(f"⚠️  Failed to clone repository: {e.stderr.strip()}", file=sys.stderr)


# ==============================================================================
# CLI Entrypoint & Workflow
# ==============================================================================


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="new-repo",
        description="Provision repositories across self-hosted Gitea and GitHub push mirror.",
    )
    parser.add_argument(
        "name",
        type=str,
        help="Name of the repository to create",
    )
    parser.add_argument(
        "-p",
        "--public",
        action="store_true",
        default=False,
        help="Make repository public (default: private)",
    )
    parser.add_argument(
        "-d",
        "--description",
        type=str,
        default="",
        help="Description for the repository",
    )
    parser.add_argument(
        "-l",
        "--license",
        type=str,
        default="UNLICENSE",
        help="License identifier template (default: UNLICENSE, pass empty string to disable)",
    )
    parser.add_argument(
        "-t",
        "--template",
        type=str,
        default=None,
        help="Optional Gitea template repository ('[owner/]template-name')",
    )
    parser.add_argument(
        "-M",
        "--no-mirror",
        action="store_true",
        default=False,
        help="Disable automatic offsite push mirroring to GitHub",
    )
    parser.add_argument(
        "--no-clone",
        action="store_true",
        default=False,
        help="Skip cloning the repository locally after creation",
    )
    return parser.parse_args()


def main() -> None:
    """Main orchestration flow."""
    args = parse_args()
    repo_name: str = args.name.strip()
    is_private: bool = not args.public

    if not repo_name:
        print("Error: Repository name cannot be empty.", file=sys.stderr)
        sys.exit(1)

    print(f"🚀 Initializing repository provisioning: '{repo_name}'")
    print(f"   Visibility: {'Private' if is_private else 'Public'}")

    # 1. Resolve Credentials
    creds = SecretProvider.resolve()
    gitea_client = GiteaClient(
        base_url=creds.gitea_url,
        user=creds.gitea_user,
        token=creds.gitea_token,
    )
    github_client = GitHubClient(
        user=creds.github_user,
        token=creds.github_token,
    )

    # 2. Provision Gitea Primary Repository
    gitea_html_url: str | None = None
    try:
        if args.template:
            template_parts = args.template.split("/", 1)
            if len(template_parts) == 2:
                tmpl_owner, tmpl_name = template_parts[0], template_parts[1]
            else:
                tmpl_owner, tmpl_name = creds.gitea_user, template_parts[0]

            print(
                f"🏗️  Generating Gitea repository from template '{tmpl_owner}/{tmpl_name}'..."
            )
            res = gitea_client.generate_from_template(
                template_owner=tmpl_owner,
                template_name=tmpl_name,
                name=repo_name,
                private=is_private,
                description=args.description,
            )
        else:
            print("🏗️  Creating primary repository on Gitea...")
            res = gitea_client.create_repo(
                name=repo_name,
                private=is_private,
                description=args.description,
                auto_init=True,
                license_name=args.license,
            )
        gitea_html_url = res.get(
            "html_url", f"{creds.gitea_url}/{creds.gitea_user}/{repo_name}"
        )
        print(f"✅ Gitea repository created: {gitea_html_url}")
    except RuntimeError as e:
        print(f"❌ Failed to create Gitea repository: {e}", file=sys.stderr)
        sys.exit(1)

    # 3. Provision GitHub Backup Mirror & Push Mirror Registration
    github_html_url: str | None = None
    if not args.no_mirror:
        print("☁️  Provisioning offsite mirror target on GitHub...")
        try:
            gh_res = github_client.create_empty_repo(
                name=repo_name,
                private=is_private,
                description=args.description,
            )
            github_html_url = gh_res.get(
                "html_url", f"https://github.com/{creds.github_user}/{repo_name}"
            )
            print(f"✅ GitHub mirror target created: {github_html_url}")

            print("🔗 Registering automated push mirror on Gitea...")
            github_clone_target = (
                f"https://github.com/{creds.github_user}/{repo_name}.git"
            )
            gitea_client.create_push_mirror(
                repo_name=repo_name,
                remote_address=github_clone_target,
                remote_username=creds.github_user,
                remote_password=creds.github_token,
                sync_on_commit=True,
                interval="8h",
            )
            print("✅ Gitea push mirror registered (sync on commit enabled)")
        except RuntimeError as e:
            # Retain & Report policy: Gitea repo exists, so inform the user rather than deleting it
            print(f"⚠️  Push mirror provisioning failed: {e}", file=sys.stderr)
            print(
                f"   Your primary Gitea repository remains intact at: {gitea_html_url}\n"
                "   You can inspect or configure mirroring manually if needed.",
                file=sys.stderr,
            )

    # 4. Clone Locally
    if not args.no_clone:
        gitea_host = (
            creds.gitea_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        )
        clone_locally(
            gitea_host=gitea_host,
            user=creds.gitea_user,
            repo_name=repo_name,
            target_dir=Path.cwd() / repo_name,
        )

    # 5. Final Summary
    print("\n🎉 Provisioning complete!")
    print(f"   Primary Forge: {gitea_html_url}")
    if github_html_url:
        print(f"   Backup Mirror: {github_html_url}")
    if not args.no_clone:
        print(f"   Working Tree:  {Path.cwd() / repo_name}")


if __name__ == "__main__":
    main()
