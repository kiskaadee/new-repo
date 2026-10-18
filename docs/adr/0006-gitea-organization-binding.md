# ADR 006: Gitea Organization Binding and Auto-Provisioning

## Status
Accepted (2026-10-18)

## Context
As projects scale beyond flat personal repositories into structured domains (such as `homelab` infrastructure, `sites`, or shared tooling), organizing repositories under Gitea organizations provides logical grouping, cleaner namespace separation, and fine-grained team/access control.

Previously, `new-repo` hardcoded `creds.gitea_user` across all Gitea API endpoints (`/api/v1/user/repos`, template generation, migration, and push mirror registration) and local SSH remote URLs (`git@<host>:<user>/<repo>.git`). Creating a repository under a Gitea organization required manual creation via the Gitea web UI followed by manual remote and mirror configuration.

## Decision
We extend `new-repo` to support binding repositories to a Gitea organization across all provisioning workflows (standard greenfield creation, existing local repository linking, template generation, and GitHub-to-Gitea migration):

1. **Flexible CLI Target Specification**:
   - Support both an explicit `-o` / `--org <ORG>` flag and positional `<org>/<repo>` shorthand (e.g., `new-repo homelab/core` or `new-repo core -o homelab`).
   - Validate and reject conflicting organization names if both forms are provided with mismatched values.

2. **Automatic Organization Provisioning on Gitea**:
   - Before provisioning or migrating a repository under a target organization, `new-repo` checks whether the organization exists via `GET /api/v1/orgs/{org}`.
   - If the organization returns `404 Not Found`, `new-repo` automatically provisions it via `POST /api/v1/orgs` with visibility matching the repository's privacy flag (`private` by default, or `public` when `--public` is passed).

3. **Endpoint Routing Across Workflows**:
   - **Standard & Existing Git Creation**: Routes to `POST /api/v1/orgs/{org}/repos` when an organization is specified, or `POST /api/v1/user/repos` otherwise.
   - **Template Generation**: Passes `"owner": gitea_owner` to `POST /api/v1/repos/{tmpl_owner}/{tmpl_name}/generate`.
   - **GitHub Migration**: Passes `"repo_owner": gitea_owner` to `POST /api/v1/repos/migrate` and checks `GET /api/v1/repos/{gitea_owner}/{dest_name}` for pre-flight conflict detection.
   - **Push Mirroring**: Registers push mirrors at `POST /api/v1/repos/{gitea_owner}/{repo_name}/push_mirrors`.

4. **Decoupled GitHub Mirror Ownership**:
   - Offsite GitHub backup mirrors continue to default to the personal `GITHUB_USER` account (`POST /user/repos`) so users do not need matching GitHub organizations for internal Gitea namespaces.
   - An optional `--github-org <GITHUB_ORG>` flag allows routing the GitHub mirror target to `POST /orgs/{org}/repos` when desired.

5. **Flat Local Directory Resolution**:
   - Local clones and working tree detection remain flat (`<cwd>/<repo_name>` or `<cwd>` if already inside `<repo_name>`), matching standard `git clone` ergonomics regardless of whether an organization prefix was used.

## Consequences

### Good
- **Zero-Friction Namespace Organization**: Repositories can be grouped under existing or brand-new Gitea organizations in a single CLI invocation.
- **Ergonomic CLI**: Supports both intuitive `org/repo` shorthand and explicit `-o org` flags (useful when inferring the repo name from the current directory via `new-repo -o homelab`).
- **Decoupled Forge Topologies**: Gitea organizations can be used freely for self-hosted categorization without forcing corresponding organization setup on GitHub.

### Bad
- Requires the Gitea Personal Access Token (`GITEA_TOKEN`) to include `write:organization` (or `admin:org`) scope when checking or auto-creating organizations.

### Neutral
- If two different Gitea organizations contain repositories with the exact same name and both mirror to the same personal GitHub account, a naming collision on GitHub will occur (mitigated by `new-repo`'s retain-and-report policy or `--no-mirror` / `--github-org`).
