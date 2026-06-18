# GitHub Repository Settings

These settings live outside git and must be verified separately in every
repository using this shared codebase. The GitHub UI, CLI, and API remain the
live source of truth.

## Branch Protection

Expected shared state:

- `main` is the protected release line.
- The repository's active development/default branch is protected.
- Pull requests are required.
- The required status check is `ci-success`.
- Squash merges are allowed.
- CodeQL default setup is enabled for Actions and Python.
- The `release` environment and `PYPI_PUBLISH_URL` variable point at this
  repository's intended package index.

Verify:

```bash
REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
DEFAULT_BRANCH="$(gh repo view --repo "$REPO" --json defaultBranchRef --jq .defaultBranchRef.name)"
gh ruleset list --repo "$REPO" --parents
gh ruleset check "$DEFAULT_BRANCH" --repo "$REPO"
gh ruleset check main --repo "$REPO"
gh api "repos/$REPO/code-scanning/default-setup"
gh variable get PYPI_PUBLISH_URL --repo "$REPO"
```

## Repository Metadata

Expected topics include `ros2`, `docker`, `robotics`, `teleoperation`, and
`communication`.

Verify:

```bash
gh repo view --json description,homepageUrl,repositoryTopics
```

## Drift Handling

If GitHub state differs from this document, either update the live setting to
match the documented contract or update this document in a PR if policy changed.
