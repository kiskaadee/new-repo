# ADR 002: Environment Variables as Primary Configuration Interface with Pluggable Secret Resolvers

## Status
Accepted (2026-10-09)

## Context
When automating infrastructure and repository creation across multiple git forges, credentials (API tokens) are required for both Gitea and GitHub.

In the author's primary workstation environment, secrets are managed securely using Mozilla SOPS and Age encryption keys, ensuring tokens never touch disk in plaintext.

However, hard-coding a strict dependency on SOPS, specific filesystem paths (like `~/Config/secrets.yaml`), or NixOS packaging creates tight environmental coupling:
1. If a collaborator, CI pipeline, or developer outside Nix wants to run `new-repo`, they would be blocked by missing SOPS keys and hardcoded local paths.
2. If the author runs the script in temporary containers, GitHub Actions, or alternative workstations, it would fail unless SOPS was configured.

We need a design that satisfies two seemingly conflicting requirements:
- **Maximum Portability**: The tool must run anywhere with standard, universal configuration conventions.
- **Zero Friction on Host**: The author should not have to manually export sensitive tokens in every shell session on their primary workstation.

## Decision
We will establish **12-Factor Environment Variables as the primary configuration contract** for `new-repo`:
- `GITEA_TOKEN`: Personal access token for Gitea REST API.
- `GITHUB_TOKEN`: Personal access token for GitHub REST API.
- `GITEA_URL`: Base URL of the Gitea instance (defaults to `https://gitea.roadtotech.me`).
- `GITEA_USER`: Gitea account owner (defaults to `kiskaadee`).
- `GITHUB_USER`: GitHub account owner (defaults to `kiskaadee`).

To provide zero-friction ergonomics on the author's workstation, the `SecretProvider` will implement a multi-tier fallback resolution hierarchy:
1. **Tier 1 (Explicit Environment)**: Use environment variables if set in the current process.
2. **Tier 2 (Local `.env` File)**: Check for a local `.env` file if present.
3. **Tier 3 (SOPS Decoupled Workstation Secrets)**: If environment variables are absent, invoke `sops -d --extract` against `~/Config/secrets.yaml`.
4. **Tier 4 (Local Client Configurations)**:
   - For Gitea: Check `~/.config/tea/config.yml` (official Gitea CLI login).
   - For GitHub: Fall back to legacy `~/Homelab/Core/nixos/secrets.yaml` if workstation secrets have not yet migrated.

## Consequences
### Good
- **Universal Portability**: Anyone outside NixOS or without SOPS can execute the script immediately simply by running `export GITEA_TOKEN=... GITHUB_TOKEN=...; new-repo my-app`.
- **Zero Host Friction**: On the author's laptop, the script resolves tokens silently and safely from SOPS without manual shell exports.
- **Process Memory Safety**: Regardless of whether tokens come from environment variables or SOPS, they are handled strictly in memory and injected via HTTPS request headers, never exposed to `/proc/$PID/cmdline` or subprocess arguments.
- **CI/CD Compatibility**: In automated pipelines, standard secret injection (`GITEA_TOKEN`, `GITHUB_TOKEN`) works without modification.

### Bad
- The script includes fallback extraction logic (~40 lines) for SOPS and tea YAML files, which is unused when pure environment variables are provided.

### Neutral
- Standardized environment variable naming conventions (`GITEA_TOKEN`, `GITHUB_TOKEN`) must be clearly documented in the project README.
