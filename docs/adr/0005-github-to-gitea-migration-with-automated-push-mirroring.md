# ADR 005: GitHub-to-Gitea Migration with Automated Push Mirroring

## Status
Accepted (2026-10-10)

## Context
Developers frequently have existing repositories hosted on GitHub—either legacy projects created before establishing a sovereign homelab forge, forks of upstream codebases, or collaborative work.

To integrate these projects into the sovereign dual-forge architecture, the repository must be:
1. Replicated onto the private Gitea instance with complete commit history, branches, and tags.
2. Configured with automated push mirroring so future commits pushed to Gitea automatically sync back to GitHub.
3. Linked locally so active development targets Gitea as the primary remote (`origin`) while retaining GitHub as a secondary remote.

Performing these steps manually involves navigating web UIs, copying tokens, configuring mirror settings, and executing manual Git remote renames and upstream branch tracking commands. Furthermore, performing the import via client-side clone and push consumes client storage and upload bandwidth.

## Decision
We introduce a dedicated `--migrate [GITHUB_URL]` option to `new-repo`:

1. **Target Parsing & Auto-Detection**:
   - Accepts HTTPS URLs (`https://github.com/owner/repo`), SSH URLs (`git@github.com:owner/repo.git`), and shorthands (`owner/repo`).
   - If the target URL is omitted, `new-repo` auto-detects the GitHub target from the local repository's `origin` remote.
   - Positional `name` argument can override the repository name on Gitea if desired.

2. **Pre-flight Conflict Validation**:
   - Before attempting migration, `new-repo` queries `GET /api/v1/repos/{owner}/{name}` on Gitea.
   - If a repository with the same name already exists on Gitea, the migration aborts immediately with an informative error to prevent overwriting or state corruption.

3. **Server-Side Import via Gitea Migration API**:
   - Calls Gitea's `POST /api/v1/repos/migrate` with `service="github"`, `mirror=False`, and the GitHub personal access token.
   - Gitea performs a direct server-to-server clone of all branches, tags, and commit history without routing data through the local machine.

4. **Automated Push Mirror Registration**:
   - Unless `--no-mirror` is specified, registers the GitHub repository as a Gitea push mirror with `sync_on_commit: true` and an 8-hour sync interval.
   - This establishes Gitea as the authoritative primary forge and GitHub as the automated replica.

5. **Local Remote Reconfiguration**:
   - If executed inside a Git repository tracking the target GitHub repository:
     - Renames the existing `origin` remote to `github` (preserving direct access to the GitHub remote).
     - Sets Gitea as the new `origin` remote (`git@<gitea-host>:<user>/<repo>.git`).
     - Updates upstream tracking for the active branch (`git branch --set-upstream-to=origin/<branch> <branch>`).
   - If executed outside a Git repository, clones the newly created Gitea repository locally unless `--no-clone` is passed.

## Consequences

### Good
- **Zero Client Bandwidth Overhead**: Full repository history, branches, and tags transfer directly between GitHub and Gitea servers.
- **Atomic Topology Transition**: Converts a GitHub-only repository into a sovereign dual-forge topology with push mirroring in a single command.
- **Preserved Remote Access**: Retaining GitHub as `github` remote maintains convenient access to GitHub-specific tooling or secondary remotes.
- **Fail-Safe Conflict Protection**: Pre-flight existence checks prevent accidental destructive overwrites.

### Bad
- Requires a GitHub personal access token with repository read permissions and a Gitea token with `write:user` and `write:repository` scopes.

### Neutral
- Standardizes on writable repository migration with reverse push mirroring rather than Gitea read-only pull mirrors.
