# ADR 004: Existing Repository Linking and Auto-Detection

## Status
Accepted (2026-10-10)

## Context
Initial iterations of `new-repo` assumed a purely greenfield workflow:
1. Create a remote repository on Gitea with `auto_init=True` (generating an initial commit with a README and license).
2. Create an empty backup repository on GitHub.
3. Register Gitea push mirroring.
4. Clone the newly generated repository into `$PWD/<name>`.

However, in daily developer workflows, repositories frequently already exist locally:
- A developer starts prototyping code locally before provisioning remotes.
- An existing standalone repository needs sovereign Gitea hosting and GitHub offsite backup attached.

If executed against an existing local repository:
- Creating the remote on Gitea with `auto_init=True` generates a remote initial commit that diverges from local commits, causing git history conflicts (`fatal: refusing to merge unrelated histories`).
- Skipping the local clone step without adding `origin` or pushing commits leaves the developer with an empty remote and unlinked local working tree.

## Decision
We will add **first-class existing repository support and auto-detection** to `new-repo`:
1. **Target & Name Resolution**:
   - If `name` is omitted or passed as `.`, `new-repo` uses the current working directory name and targets `$PWD`.
   - If `name` matches the current directory or points to an existing directory containing `.git`, the utility switches into *Existing Repository Mode*.
2. **Empty Remote Creation**:
   - When linking an existing repository, Gitea is provisioned with `auto_init=False` and no license generation. Both Gitea and GitHub remotes are initialized completely empty.
3. **Local Remote Wiring & Push**:
   - `new-repo` configures `origin` on the local repository pointing to `git@<gitea-host>:<user>/<repo>.git`.
   - The tool detects the current active branch (defaulting to `main`) and pushes with upstream tracking (`git push -u origin <branch>`).
   - Server-side, Gitea automatically synchronizes all pushed commits to GitHub via the registered push mirror.

## Consequences
### Good
- **Unified Command**: `new-repo` handles both greenfield project generation and remote attachment for existing local code with the same interface.
- **Commit Lineage Preserved**: Eliminates divergent history conflicts by ensuring remote forges remain empty until the local branch is pushed.
- **Developer Ergonomics**: Running `new-repo` inside any existing git project immediately binds it to sovereign dual-forge infrastructure in one step.

### Bad
- The script now requires read/write access to local `.git` configuration and executes local `git remote` and `git push` commands.

### Neutral
- If `origin` already exists, `new-repo` updates the remote URL to the primary Gitea instance.
