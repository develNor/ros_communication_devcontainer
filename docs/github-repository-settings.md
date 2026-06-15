# GitHub Repository Settings

These settings live outside git and must be verified in GitHub. This document
records the expected contract for repository `develNor/rosotacom`; the GitHub
UI, CLI, and API remain the live source of truth.

## Branch Protection

Expected state:

- `main` is protected.
- Pull requests are required.
- The required status check is `ci-success`.
- Squash merges are allowed.
- CodeQL default setup is enabled for Actions and Python.

Verify:

```bash
gh ruleset list --repo develNor/rosotacom --parents
gh ruleset check main --repo develNor/rosotacom
gh api repos/develNor/rosotacom/code-scanning/default-setup
```

## Repository Metadata

Expected topics include `ros2`, `docker`, `robotics`, `teleoperation`, and
`communication`.

Verify:

```bash
gh repo view develNor/rosotacom --json description,homepageUrl,repositoryTopics
```

## Drift Handling

If GitHub state differs from this document, either update the live setting to
match the documented contract or update this document in a PR if policy changed.
