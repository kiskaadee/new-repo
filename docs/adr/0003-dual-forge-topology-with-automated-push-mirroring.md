# ADR 003: Dual-Forge Topology with Automated Push Mirroring and Retain-and-Report Error Policy

## Status
Accepted (2026-10-09)

## Context
In a sovereign, self-hosted developer environment:
1. **Local Autonomy**: Developers need primary repositories hosted on a private forge (`gitea.roadtotech.me`) to eliminate dependencies on third-party cloud availability, allow high-bandwidth local pushes, and enable local CI/CD pipelines.
2. **Cloud Redundancy**: Offsite disaster recovery is required. If local storage fails or the self-hosted server experiences downtime, code should be preserved on a redundant offsite cloud provider (GitHub).
3. **Operational Overhead**: Manually maintaining dual remotes locally (`git push origin && git push github`) creates cognitive load, is prone to developer forgetfulness, and breaks single-remote automation.
4. **Failure Modes**: When automating a multi-stage remote provisioning pipeline across two external network services, network partitions, authentication errors, or rate limits can cause a failure midway through the sequence.

## Decision
1. **Asymmetric Push Mirror Architecture**:
   - Gitea is the authoritative primary forge for all new repositories.
   - GitHub is an automated backup mirror target.
   - When provisioning, `new-repo` creates the repository on Gitea (with initial license and commit) and creates a completely empty target repository on GitHub (`auto_init=False`).
   - `new-repo` configures Gitea's internal push mirror daemon (`POST /api/v1/repos/{owner}/{repo}/push_mirrors`) with `sync_on_commit=True`.
   - The developer interacts exclusively with Gitea (`origin`). Gitea automatically synchronizes all branches, tags, and commits to GitHub in the background.

2. **Auto-Clone by Default**:
   - Following remote provisioning, `new-repo` clones the repository locally into `$PWD/<name>` by default, enabling the developer to immediately begin work. A `--no-clone` flag is provided to bypass local filesystem operations.

3. **Retain & Report Error Recovery Policy**:
   - If GitHub repository creation or push mirror registration fails after Gitea repository creation has succeeded, the tool will **not** roll back or delete the Gitea repository.
   - Deleting remote repositories autonomously on partial failure poses catastrophic data-loss risks if the failure was transient.
   - Instead, the tool reports actionable diagnostic warnings, prints the URL of the surviving Gitea repository, and exits cleanly.

## Consequences
### Good
- **Single Remote Simplicity**: The developer pushes to `origin` (Gitea) as normal; cloud backups happen asynchronously and server-side.
- **Disaster Proof**: Cloud backup is guaranteed without relying on local git hooks or manual multi-remote pushes.
- **Safe Recovery**: Transient network or GitHub quota errors do not blow away newly created repositories.
- **Immediate Ergonomics**: Running `new-repo my-app` leaves the user inside their cloned directory ready to code.

### Bad
- GitHub Personal Access Token must be stored inside Gitea's push mirror configuration so Gitea's worker can authenticate against GitHub.

### Neutral
- If a push mirror is disrupted, sync status must be inspected via Gitea's repository settings web interface.
