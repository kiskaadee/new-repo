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
        ssh_key: Optional path to dedicated SSH identity file for git operations.
    """

    gitea_url: str
    gitea_user: str
    gitea_token: str
    github_user: str
    github_token: str
    ssh_key: str | None = None


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
    def _parse_tea_logins(tea_cfg: Path) -> list[dict[str, Any]]:
        """Parse ~/.config/tea/config.yml into discrete login dictionaries."""
        if not tea_cfg.is_file():
            return []
        logins: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        in_logins = False
        try:
            with open(tea_cfg, "r", encoding="utf-8") as f:
                for raw_line in f:
                    stripped = raw_line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if stripped == "logins:":
                        in_logins = True
                        continue
                    if in_logins and not raw_line.startswith((" ", "\t", "-")):
                        break
                    if not in_logins:
                        continue

                    if stripped.startswith("- "):
                        if current is not None:
                            logins.append(current)
                        current = {}
                        rest = stripped[2:].strip()
                        if ":" in rest:
                            k, v = rest.split(":", 1)
                            val = v.strip().strip("'\"")
                            current[k.strip()] = True if val.lower() == "true" else False if val.lower() == "false" else val
                    elif current is not None and ":" in stripped:
                        k, v = stripped.split(":", 1)
                        val = v.strip().strip("'\"")
                        current[k.strip()] = True if val.lower() == "true" else False if val.lower() == "false" else val
                if current is not None:
                    logins.append(current)
        except OSError:
            return []
        return logins

    @classmethod
    def _extract_tea_login(
        cls, target_persona: str | None = None
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Extract Gitea credentials from ~/.config/tea/config.yml for a target persona or default.

        Returns:
            A tuple of (token, url, user, ssh_key), or (None, None, None, None).
        """
        tea_cfg = Path.home() / ".config" / "tea" / "config.yml"
        logins = cls._parse_tea_logins(tea_cfg)
        if not logins:
            return None, None, None, None

        selected: dict[str, Any] | None = None
        if target_persona:
            target_norm = target_persona.strip().lower()
            for entry in logins:
                name = str(entry.get("name", "")).strip().lower()
                user = str(entry.get("user", "")).strip().lower()
                if target_norm in (name, user):
                    selected = entry
                    break
            if not selected:
                available = ", ".join(str(e.get("name", "")) for e in logins)
                print(
                    f"❌ Error: Persona / login '{target_persona}' not found in tea configuration ({tea_cfg}).\n"
                    f"   Available tea logins: {available}",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            for entry in logins:
                if entry.get("default") is True:
                    selected = entry
                    break
            if not selected and logins:
                selected = logins[0]

        if not selected:
            return None, None, None, None

        return (
            selected.get("token"),
            selected.get("url"),
            selected.get("user") or selected.get("name"),
            selected.get("ssh_key"),
        )

    @classmethod
    def resolve(cls, as_persona: str | None = None) -> Credentials:
        """Resolve all necessary credentials following the multi-tier hierarchy.

        Args:
            as_persona: Optional persona/login name (e.g. 'anneux').

        Raises:
            SystemExit: If Gitea or GitHub tokens cannot be resolved from any source.
        """
        # Load local .env if present
        dotenv = cls._load_dotenv(Path.cwd() / ".env")

        def get_val(key: str) -> str | None:
            return os.environ.get(key) or dotenv.get(key)

        persona = as_persona or get_val("NEW_REPO_AS") or get_val("TEA_LOGIN")

        secrets_file_env = os.environ.get("SECRETS_FILE")
        config_secrets = (
            Path(secrets_file_env)
            if secrets_file_env
            else Path.home() / "Config" / "secrets.yaml"
        )
        homelab_secrets = (
            Path(os.environ["HOMELAB_SECRETS_FILE"])
            if "HOMELAB_SECRETS_FILE" in os.environ
            else Path.home() / "Homelab" / "Core" / "nixos" / "secrets.yaml"
        )

        tea_token, tea_url, tea_user, tea_ssh_key = cls._extract_tea_login(target_persona=persona)

        persona_sops: Path | None = None
        if persona:
            candidates = [
                Path.home() / "Config" / f"{persona}-secrets.yaml",
                Path.home() / "Config" / f"{persona.rstrip('ux')}-secrets.yaml",
            ]
            for c in candidates:
                if c.is_file():
                    persona_sops = c
                    break

        gitea_url = get_val("GITEA_URL") or tea_url or "https://gitea.roadtotech.me"

        if persona:
            gitea_user = get_val("GITEA_USER") or tea_user or persona
            gitea_token = (
                get_val("GITEA_TOKEN")
                or (cls._extract_sops_key(["gitea", "token"], persona_sops) if persona_sops else None)
                or (cls._extract_sops_key(["gitea", "api_token"], persona_sops) if persona_sops else None)
                or tea_token
            )
            github_user = get_val("GITHUB_USER") or persona
            github_token = (
                get_val("GITHUB_TOKEN")
                or (cls._extract_sops_key(["github", "token"], persona_sops) if persona_sops else None)
                or cls._extract_sops_key(["system", "gh_repo_token"], config_secrets)
            )
        else:
            gitea_user = get_val("GITEA_USER") or tea_user or "kiskaadee"
            gitea_token = (
                get_val("GITEA_TOKEN")
                or cls._extract_sops_key(["gitea", "api_token"], config_secrets)
                or tea_token
            )
            github_user = get_val("GITHUB_USER") or "kiskaadee"
            github_token = (
                get_val("GITHUB_TOKEN")
                or cls._extract_sops_key(["system", "gh_repo_token"], config_secrets)
                or cls._extract_sops_key(["system", "gh_repo_token"], homelab_secrets)
            )

        if not gitea_token:
            print(
                f"Error: Unable to resolve Gitea API token for persona '{gitea_user}'.\n"
                "To provide credentials, either:\n"
                "  1. Export GITEA_TOKEN in your environment (or define in a .env file)\n"
                "  2. Add 'gitea.api_token' to ~/Config/secrets.yaml via SOPS\n"
                "  3. Configure tea CLI via 'tea login add'\n"
                "\n"
                "Note: In Gitea, generate a token at: User Settings -> Applications -> Manage Access Tokens.",
                file=sys.stderr,
            )
            sys.exit(1)

        if not github_token:
            print(
                f"Error: Unable to resolve GitHub API token for persona '{github_user}'.\n"
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
            ssh_key=tea_ssh_key,
        )


# ==============================================================================
# License Registry & Normalization
# ==============================================================================

GITEA_LICENSES: dict[str, str] = {
    "0bsd": "0BSD",
    "agpl-3.0": "AGPL-3.0",
    "apache-2.0": "Apache-2.0",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "bsd-3-clause-clear": "BSD-3-Clause-Clear",
    "bsl-1.0": "BSL-1.0",
    "cc-by-4.0": "CC-BY-4.0",
    "cc-by-sa-4.0": "CC-BY-SA-4.0",
    "cc0-1.0": "CC0-1.0",
    "epl-1.0": "EPL-1.0",
    "epl-2.0": "EPL-2.0",
    "eupl-1.2": "EUPL-1.2",
    "gpl-2.0": "GPL-2.0",
    "gpl-3.0": "GPL-3.0",
    "isc": "ISC",
    "lgpl-2.1": "LGPL-2.1",
    "lgpl-3.0": "LGPL-3.0",
    "mit": "MIT",
    "mit-0": "MIT-0",
    "mpl-2.0": "MPL-2.0",
    "mulanpsl-2.0": "MulanPSL-2.0",
    "ofl-1.1": "OFL-1.1",
    "osl-3.0": "OSL-3.0",
    "upl-1.0": "UPL-1.0",
    "unlicense": "Unlicense",
    "wtfpl": "WTFPL",
    "zlib": "Zlib",
}


def normalize_license(license_name: str | None) -> str:
    """Normalize license identifier to match Gitea's case-sensitive template registry.

    Gitea resolves license templates by exact filename in its internal assets.
    For example, 'UNLICENSE' fails with a 500 error while 'Unlicense' succeeds.
    """
    if not license_name:
        return ""
    clean = license_name.strip()
    if not clean:
        return ""
    return GITEA_LICENSES.get(clean.lower(), clean)


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

    def get_authenticated_user(self) -> str:
        """Return the authenticated username associated with the token."""
        try:
            res = self._request("GET", "/api/v1/user")
            return res.get("username") or res.get("login") or ""
        except Exception:
            return ""

    def org_exists(self, org: str) -> bool:
        """Check if an organization exists on Gitea."""
        url = f"{self.base_url}/api/v1/orgs/{org}"
        req = urllib.request.Request(
            url=url,
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raw_err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Gitea API error checking org '{org}' ({e.code} {e.reason}): {raw_err}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Gitea network error: {e.reason}") from e

    def create_org(
        self,
        org: str,
        visibility: str = "private",
    ) -> dict[str, Any]:
        """Create a new organization on Gitea."""
        payload: dict[str, Any] = {
            "username": org,
            "visibility": visibility,
        }
        return self._request("POST", "/api/v1/orgs", payload)

    def ensure_org(self, org: str, private: bool = False) -> bool:
        """Ensure a Gitea organization exists, creating it if missing.

        Returns:
            True if a new organization was created, False if it already existed.
        """
        if self.org_exists(org):
            return False
        visibility = "private" if private else "public"
        try:
            self.create_org(org, visibility=visibility)
            return True
        except RuntimeError as e:
            raise RuntimeError(
                f"Failed to create Gitea organization '{org}': {e}\n"
                "Hint: Ensure your GITEA_TOKEN has 'write:organization' (or 'admin:org') permission scope."
            ) from e

    def create_repo(
        self,
        name: str,
        private: bool = False,
        description: str = "",
        auto_init: bool = True,
        license_name: str = "Unlicense",
        org: str | None = None,
    ) -> dict[str, Any]:
        """Create a new repository under the authenticated user or specified organization."""
        payload: dict[str, Any] = {
            "name": name,
            "private": private,
            "description": description,
            "auto_init": auto_init,
            "default_branch": "main",
        }
        normalized_license = normalize_license(license_name)
        if normalized_license:
            payload["license"] = normalized_license

        endpoint = f"/api/v1/orgs/{org}/repos" if org else "/api/v1/user/repos"
        return self._request("POST", endpoint, payload)

    def generate_from_template(
        self,
        template_owner: str,
        template_name: str,
        name: str,
        private: bool = False,
        description: str = "",
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Generate a new repository from an existing Gitea template repository."""
        payload = {
            "owner": owner or self.user,
            "name": name,
            "private": private,
            "description": description,
        }
        path = f"/api/v1/repos/{template_owner}/{template_name}/generate"
        return self._request("POST", path, payload)

    def repo_exists(self, owner: str, name: str) -> bool:
        """Check if a repository already exists on Gitea under the given owner."""
        url = f"{self.base_url}/api/v1/repos/{owner}/{name}"
        req = urllib.request.Request(
            url=url,
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raw_err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Gitea API error ({e.code} {e.reason}): {raw_err}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Gitea network error: {e.reason}") from e

    def migrate_repo(
        self,
        clone_addr: str,
        repo_name: str,
        github_token: str,
        private: bool = False,
        description: str = "",
        repo_owner: str | None = None,
    ) -> dict[str, Any]:
        """Import an existing GitHub repository server-side into Gitea.

        Uses Gitea's POST /api/v1/repos/migrate with mirror=False to perform
        a complete one-time import of all git branches, tags, and commits.
        """
        payload = {
            "clone_addr": clone_addr,
            "auth_token": github_token,
            "repo_name": repo_name,
            "repo_owner": repo_owner or self.user,
            "service": "github",
            "mirror": False,
            "private": private,
            "description": description,
        }
        return self._request("POST", "/api/v1/repos/migrate", payload)

    def create_push_mirror(
        self,
        repo_name: str,
        remote_address: str,
        remote_username: str,
        remote_password: str,
        sync_on_commit: bool = True,
        interval: str = "8h",
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Register a push mirror for a Gitea repository to an external Git target."""
        payload = {
            "remote_address": remote_address,
            "remote_username": remote_username,
            "remote_password": remote_password,
            "sync_on_commit": sync_on_commit,
            "interval": interval,
        }
        target_owner = owner or self.user
        path = f"/api/v1/repos/{target_owner}/{repo_name}/push_mirrors"
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
        private: bool = False,
        description: str = "",
        org: str | None = None,
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
        endpoint = (
            f"{self.api_url}/orgs/{org}/repos" if org else f"{self.api_url}/user/repos"
        )

        req = urllib.request.Request(
            url=endpoint,
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


def clone_locally(
    gitea_host: str,
    user: str,
    repo_name: str,
    target_dir: Path,
    clone_url: str | None = None,
    ssh_key: str | None = None,
) -> None:
    """Clones the newly created repository into the target directory."""
    if target_dir.exists():
        print(f"⚠️  Target directory '{target_dir}' already exists. Skipping clone.")
        return

    final_clone_url = clone_url or f"git@{gitea_host}:{user}/{repo_name}.git"
    print(f"📦 Cloning repository locally into '{target_dir.name}'...")
    env = os.environ.copy()
    if ssh_key and Path(ssh_key).is_file():
        env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key} -o IdentitiesOnly=yes"
    try:
        subprocess.run(
            ["git", "clone", final_clone_url, str(target_dir)],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        print(f"✅ Cloned successfully to {target_dir}")
    except subprocess.CalledProcessError as e:
        print(f"⚠️  Failed to clone repository: {e.stderr.strip()}", file=sys.stderr)


def link_and_push_existing(
    gitea_host: str,
    user: str,
    repo_name: str,
    target_dir: Path,
    clone_url: str | None = None,
    ssh_key: str | None = None,
) -> None:
    """Links an existing local git repository to the new Gitea remote and pushes commits."""
    final_clone_url = clone_url or f"git@{gitea_host}:{user}/{repo_name}.git"
    print(f"🔗 Setting remote 'origin' to {final_clone_url}...")
    env = os.environ.copy()
    if ssh_key and Path(ssh_key).is_file():
        env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key} -o IdentitiesOnly=yes"
    try:
        remotes_out = subprocess.run(
            ["git", "remote"],
            cwd=target_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        remotes = remotes_out.stdout.split()
        if "origin" in remotes:
            subprocess.run(
                ["git", "remote", "set-url", "origin", final_clone_url],
                cwd=target_dir,
                check=True,
            )
            print("   Updated existing 'origin' remote URL.")
        else:
            subprocess.run(
                ["git", "remote", "add", "origin", final_clone_url],
                cwd=target_dir,
                check=True,
            )
            print("   Added 'origin' remote.")

        # Detect current branch
        branch_res = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=target_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        current_branch = branch_res.stdout.strip() or "main"
        print(f"🚀 Pushing branch '{current_branch}' to origin...")
        subprocess.run(
            ["git", "push", "-u", "origin", current_branch],
            cwd=target_dir,
            check=True,
            env=env,
        )
        print(f"✅ Successfully pushed '{current_branch}' to Gitea!")
    except subprocess.CalledProcessError as e:
        print(f"⚠️  Failed to link/push repository: {e}", file=sys.stderr)


def resolve_name_and_org(
    name_arg: str | None,
    org_arg: str | None = None,
) -> tuple[str | None, str | None]:
    """Parse repository name and optional Gitea organization from CLI arguments.

    Supports both explicit `--org <org>` and positional `org/repo` shorthand.

    Raises:
        ValueError: If `name_arg` has an invalid slash format or conflicts with `org_arg`.
    """
    explicit_org = org_arg.strip() if org_arg and org_arg.strip() else None

    if not name_arg or name_arg.strip() == ".":
        return None, explicit_org

    raw_name = name_arg.strip()
    if "/" in raw_name:
        parts = [p.strip() for p in raw_name.split("/") if p.strip()]
        if len(parts) != 2 or raw_name.startswith("/") or raw_name.endswith("/"):
            raise ValueError(
                f"Invalid repository target '{raw_name}'. Expected '<repo>' or '<org>/<repo>'."
            )
        shorthand_org, repo_name = parts[0], parts[1]
        if explicit_org and explicit_org != shorthand_org:
            raise ValueError(
                f"Conflicting organizations specified: positional '{shorthand_org}' vs --org '{explicit_org}'."
            )
        return repo_name, shorthand_org

    return raw_name, explicit_org


def determine_target(
    name_arg: str | None,
    org_arg: str | None = None,
    cwd: Path | None = None,
) -> tuple[str, Path, bool, str | None]:
    """Determine (repo_name, target_dir, is_existing_git, target_org).

    Returns:
        repo_name: The clean repository name for Gitea and GitHub.
        target_dir: The flat directory path on disk.
        is_existing_git: True if target_dir is an existing git repository.
        target_org: The resolved Gitea organization name, or None for personal user.
    """
    base_dir = cwd if cwd is not None else Path.cwd()
    parsed_name, target_org = resolve_name_and_org(name_arg, org_arg)

    if not parsed_name:
        repo_name = base_dir.name
        target_dir = base_dir
    else:
        if parsed_name == base_dir.name:
            repo_name = parsed_name
            target_dir = base_dir
        else:
            repo_name = parsed_name
            target_dir = base_dir / parsed_name

    is_existing_git = (target_dir / ".git").is_dir()
    return repo_name, target_dir, is_existing_git, target_org


def parse_github_target(target_str: str | None, cwd: Path) -> tuple[str, str, str]:
    """Parse a GitHub target string or detect it from local git remotes.

    Args:
        target_str: GitHub URL (HTTPS, SSH) or shorthand ('owner/repo').
            If None or empty, auto-detects from the local 'origin' remote in cwd.
        cwd: Current working directory for auto-detection.

    Returns:
        tuple of (repo_name, github_owner, github_clone_url)

    Raises:
        ValueError: If target string cannot be parsed as a valid GitHub repository.
        RuntimeError: If auto-detection fails because cwd lacks a GitHub remote.
    """
    raw = target_str.strip() if target_str else ""

    if not raw:
        # Auto-detect from local git repo
        if not (cwd / ".git").is_dir():
            raise RuntimeError(
                "No GitHub URL provided, and current directory is not a Git repository. "
                "Please specify a target (e.g. --migrate https://github.com/owner/repo)."
            )
        try:
            origin_url = subprocess.check_output(
                ["git", "remote", "get-url", "origin"],
                cwd=cwd,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            raw = origin_url
            print(f"🔍 Auto-detected GitHub target from local origin: {raw}")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                "Could not detect 'origin' remote from current Git repository. "
                "Please specify a target (e.g. --migrate https://github.com/owner/repo)."
            ) from e

    clean = raw.strip().rstrip("/").removesuffix(".git")

    owner: str = ""
    name: str = ""

    if "github.com" in clean:
        # Matches https://github.com/owner/repo or git@github.com:owner/repo
        if ":" in clean and "@" in clean:
            path_part = clean.split(":", 1)[1]
        else:
            path_part = clean.split("github.com/", 1)[1]

        parts = [p for p in path_part.split("/") if p]
        if len(parts) >= 2:
            owner, name = parts[0], parts[1]
    elif "/" in clean:
        parts = [p for p in clean.split("/") if p]
        if len(parts) == 2:
            owner, name = parts[0], parts[1]

    if not owner or not name:
        raise ValueError(
            f"Could not parse GitHub repository owner and name from '{raw}'. "
            "Expected formats: https://github.com/owner/repo, git@github.com:owner/repo.git, or owner/repo."
        )

    clone_url = f"https://github.com/{owner}/{name}.git"
    return name, owner, clone_url


def reconfigure_local_remotes(target_dir: Path, gitea_clone_url: str) -> None:
    """Reconfigures local git remotes for a migrated repository.

    Renames existing 'origin' to 'github', sets Gitea as the new 'origin',
    and updates upstream tracking for the active branch.
    """
    try:
        remotes_out = subprocess.run(
            ["git", "remote"],
            cwd=target_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        remotes = remotes_out.stdout.split()

        if "origin" in remotes:
            if "github" in remotes:
                print("   Remote 'github' already exists. Removing legacy 'origin'...")
                subprocess.run(
                    ["git", "remote", "remove", "origin"],
                    cwd=target_dir,
                    check=True,
                )
            else:
                print("🔄 Renaming existing remote 'origin' to 'github'...")
                subprocess.run(
                    ["git", "remote", "rename", "origin", "github"],
                    cwd=target_dir,
                    check=True,
                )

        print(f"🔗 Setting new primary remote 'origin' to {gitea_clone_url}...")
        subprocess.run(
            ["git", "remote", "add", "origin", gitea_clone_url],
            cwd=target_dir,
            check=True,
        )

        # Detect active branch
        branch_res = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=target_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        active_branch = branch_res.stdout.strip() or "main"
        print(
            f"🚀 Updating upstream tracking for branch '{active_branch}' to origin..."
        )
        subprocess.run(
            [
                "git",
                "branch",
                f"--set-upstream-to=origin/{active_branch}",
                active_branch,
            ],
            cwd=target_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        print("✅ Local remotes reconfigured successfully!")
    except subprocess.CalledProcessError as e:
        print(f"⚠️  Failed to reconfigure local remotes: {e}", file=sys.stderr)


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
        nargs="?",
        default=None,
        type=str,
        help="Name of the repository to create ('[org/]repo', defaults to current directory if omitted or '.')",
    )
    parser.add_argument(
        "-o",
        "--org",
        type=str,
        default=None,
        help="Target Gitea organization to own the repository (auto-created if missing)",
    )
    parser.add_argument(
        "--github-org",
        type=str,
        default=None,
        help="Optional GitHub organization for the offsite backup mirror (defaults to personal GITHUB_USER)",
    )
    parser.add_argument(
        "-p",
        "--private",
        action="store_true",
        default=False,
        help="Make repository private (default: public)",
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
        default="Unlicense",
        help="License identifier template (default: Unlicense, pass empty string to disable)",
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
    parser.add_argument(
        "--as",
        dest="as_persona",
        type=str,
        default=None,
        help="Persona / login identity to provision as (matches tea login name or SOPS persona, e.g. anneux)",
    )
    parser.add_argument(
        "--migrate",
        nargs="?",
        const="",
        default=None,
        metavar="GITHUB_URL",
        help="Migrate an existing GitHub repository to Gitea with automated push mirroring (URL or auto-detect from local origin)",
    )
    return parser.parse_args()


def main() -> None:
    """Main orchestration flow."""
    args = parse_args()
    is_private: bool = args.private

    # 1. Resolve Credentials
    creds = SecretProvider.resolve(as_persona=args.as_persona)
    gitea_client = GiteaClient(
        base_url=creds.gitea_url,
        user=creds.gitea_user,
        token=creds.gitea_token,
    )
    github_client = GitHubClient(
        user=creds.github_user,
        token=creds.github_token,
    )
    gitea_host = creds.gitea_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]

    # --------------------------------------------------------------------------
    # Migration Workflow (--migrate)
    # --------------------------------------------------------------------------
    if args.migrate is not None:
        try:
            migrated_name, gh_owner, gh_clone_url = parse_github_target(
                args.migrate, Path.cwd()
            )
            override_name, target_org = resolve_name_and_org(args.name, args.org)
        except (ValueError, RuntimeError) as e:
            print(f"❌ Error: {e}", file=sys.stderr)
            sys.exit(1)

        dest_name = override_name or migrated_name
        gitea_owner = target_org or creds.gitea_user

        # Align with token authenticated user if targeting user namespace
        if not target_org:
            auth_user = gitea_client.get_authenticated_user()
            if auth_user and auth_user.lower() != gitea_owner.lower():
                print(
                    f"⚠️  Identity Warning: Gitea token belongs to '{auth_user}', but session persona is '{gitea_owner}'.\n"
                    f"   Aligning primary owner to token owner '{auth_user}'.",
                    file=sys.stderr,
                )
                gitea_owner = auth_user

        print(
            f"🚀 Initiating repository migration: '{gh_owner}/{migrated_name}' -> Gitea '{gitea_owner}/{dest_name}'"
        )
        print(f"   Visibility: {'Private' if is_private else 'Public'}")

        if target_org:
            try:
                created_org = gitea_client.ensure_org(target_org, private=is_private)
                if created_org:
                    print(f"🏢 Created new Gitea organization '{target_org}'")
            except RuntimeError as e:
                print(f"❌ Error: {e}", file=sys.stderr)
                sys.exit(1)

        # Fail-safe conflict check
        if gitea_client.repo_exists(gitea_owner, dest_name):
            print(
                f"❌ Error: Repository '{dest_name}' already exists on Gitea under '{gitea_owner}'.\n"
                f"   Inspect at: {creds.gitea_url}/{gitea_owner}/{dest_name}\n"
                "   Aborting migration to prevent overwriting existing repositories.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Step 1: Server-side import via Gitea Migration API
        print(
            "🏗️  Importing full repository history from GitHub via Gitea Migration API..."
        )
        try:
            res = gitea_client.migrate_repo(
                clone_addr=gh_clone_url,
                repo_name=dest_name,
                github_token=creds.github_token,
                private=is_private,
                description=args.description,
                repo_owner=gitea_owner,
            )
            actual_owner = res.get("owner", {}).get("username") or gitea_owner
            gitea_html_url = res.get(
                "html_url", f"{creds.gitea_url}/{actual_owner}/{dest_name}"
            )
            print(f"✅ Gitea repository created from GitHub: {gitea_html_url}")
        except RuntimeError as e:
            print(f"❌ Migration failed: {e}", file=sys.stderr)
            sys.exit(1)

        # Step 2: Register automated push mirror to GitHub
        if not args.no_mirror:
            print("🔗 Registering automated push mirror to GitHub...")
            try:
                gitea_client.create_push_mirror(
                    repo_name=dest_name,
                    remote_address=gh_clone_url,
                    remote_username=creds.github_user,
                    remote_password=creds.github_token,
                    sync_on_commit=True,
                    interval="8h",
                    owner=actual_owner,
                )
                print("✅ Gitea push mirror registered (sync on commit enabled)")
            except RuntimeError as e:
                print(f"⚠️  Push mirror registration failed: {e}", file=sys.stderr)

        # Step 3: Reconfigure local working tree if inside a Git repository
        gitea_clone_target = res.get("ssh_url") or f"git@{gitea_host}:{actual_owner}/{dest_name}.git"
        if (Path.cwd() / ".git").is_dir():
            reconfigure_local_remotes(Path.cwd(), gitea_clone_target)
        elif not args.no_clone:
            clone_locally(
                gitea_host=gitea_host,
                user=actual_owner,
                repo_name=dest_name,
                target_dir=Path.cwd() / dest_name,
                clone_url=gitea_clone_target,
                ssh_key=creds.ssh_key,
            )

        print("\n🎉 Migration complete!")
        print(f"   Primary Forge (Gitea):  {gitea_html_url}")
        print(f"   Backup Mirror (GitHub): {gh_clone_url}")
        return

    # --------------------------------------------------------------------------
    # Standard Provisioning Workflow
    # --------------------------------------------------------------------------
    try:
        repo_name, target_dir, is_existing_git, target_org = determine_target(
            args.name, args.org
        )
    except ValueError as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not repo_name:
        print("Error: Repository name cannot be determined.", file=sys.stderr)
        sys.exit(1)

    gitea_owner = target_org or creds.gitea_user
    github_org = (
        args.github_org.strip() if args.github_org and args.github_org.strip() else None
    )
    github_owner = github_org or creds.github_user

    # Align with token authenticated user if targeting user namespace
    if not target_org:
        auth_user = gitea_client.get_authenticated_user()
        if auth_user and auth_user.lower() != gitea_owner.lower():
            print(
                f"⚠️  Identity Warning: Gitea token belongs to '{auth_user}', but session persona is '{gitea_owner}'.\n"
                f"   Aligning primary owner to token owner '{auth_user}'.",
                file=sys.stderr,
            )
            gitea_owner = auth_user

    print(f"🚀 Initializing repository provisioning: '{gitea_owner}/{repo_name}'")
    print(f"   Visibility: {'Private' if is_private else 'Public'}")
    if is_existing_git:
        print(f"   Mode: Existing local repository at '{target_dir}'")
    else:
        print(f"   Mode: Greenfield creation at '{target_dir}'")

    # 2. Provision Gitea Primary Repository
    gitea_html_url: str | None = None
    try:
        if target_org:
            created_org = gitea_client.ensure_org(target_org, private=is_private)
            if created_org:
                print(f"🏢 Created new Gitea organization '{target_org}'")

        if is_existing_git:
            print(
                "🏗️  Creating empty primary repository on Gitea (preserving local history)..."
            )
            res = gitea_client.create_repo(
                name=repo_name,
                private=is_private,
                description=args.description,
                auto_init=False,  # CRUCIAL: Do not create remote README/license to prevent divergent commit histories
                license_name="",
                org=target_org,
            )
        elif args.template:
            template_parts = args.template.split("/", 1)
            if len(template_parts) == 2:
                tmpl_owner, tmpl_name = template_parts[0], template_parts[1]
            else:
                tmpl_owner, tmpl_name = gitea_owner, template_parts[0]

            print(
                f"🏗️  Generating Gitea repository from template '{tmpl_owner}/{tmpl_name}'..."
            )
            res = gitea_client.generate_from_template(
                template_owner=tmpl_owner,
                template_name=tmpl_name,
                name=repo_name,
                private=is_private,
                description=args.description,
                owner=gitea_owner,
            )
        else:
            print("🏗️  Creating primary repository on Gitea...")
            res = gitea_client.create_repo(
                name=repo_name,
                private=is_private,
                description=args.description,
                auto_init=True,
                license_name=args.license,
                org=target_org,
            )

        actual_owner = res.get("owner", {}).get("username") or gitea_owner
        actual_clone_url = res.get("ssh_url") or f"git@{gitea_host}:{actual_owner}/{repo_name}.git"
        gitea_html_url = res.get(
            "html_url", f"{creds.gitea_url}/{actual_owner}/{repo_name}"
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
                org=github_org,
            )
            github_html_url = gh_res.get(
                "html_url", f"https://github.com/{github_owner}/{repo_name}"
            )
            print(f"✅ GitHub mirror target created: {github_html_url}")

            print("🔗 Registering automated push mirror on Gitea...")
            github_clone_target = f"https://github.com/{github_owner}/{repo_name}.git"
            gitea_client.create_push_mirror(
                repo_name=repo_name,
                remote_address=github_clone_target,
                remote_username=creds.github_user,
                remote_password=creds.github_token,
                sync_on_commit=True,
                interval="8h",
                owner=actual_owner,
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

    # 4. Local Git Workflow
    if is_existing_git:
        link_and_push_existing(
            gitea_host=gitea_host,
            user=actual_owner,
            repo_name=repo_name,
            target_dir=target_dir,
            clone_url=actual_clone_url,
            ssh_key=creds.ssh_key,
        )
    elif not args.no_clone:
        clone_locally(
            gitea_host=gitea_host,
            user=actual_owner,
            repo_name=repo_name,
            target_dir=target_dir,
            clone_url=actual_clone_url,
            ssh_key=creds.ssh_key,
        )

    # 5. Final Summary
    print("\n🎉 Provisioning complete!")
    print(f"   Primary Forge: {gitea_html_url}")
    if github_html_url:
        print(f"   Backup Mirror: {github_html_url}")
    print(f"   Working Tree:  {target_dir}")


if __name__ == "__main__":
    main()
