# Owner Runbook

This runbook collects the owner-facing entrypoints for `rosotacom`. Everything
under `.github/ISSUE_TEMPLATE/` is the execution layer used after one of these
entrypoints is chosen.

## Run A Quality Pass

Use this prompt when the repository needs a maintenance pass, especially before a
release or after a series of feature PRs:

```text
Run the repository quality workflow autonomously: start with the
repository-diagnosis pass, then create and complete the focused PRs it triages in
priority order. Use one fresh origin/develop branch per issue, open ready PRs,
enable auto-merge when repository policy allows it, and stop if diagnosis finds
nothing actionable. Report each issue, PR URL, and CI status.
```

The pass diagnoses first and may create zero PRs. That is acceptable when the
diagnosis records the probes that found no action.

## Prepare A Release

Use [docs/release.md](release.md) and the
[release-process](../.github/ISSUE_TEMPLATE/release-process.md) template. A
release is owner-gated:

- Choose and validate the `main` commit deliberately.
- Record the external OTA promotion evidence when the repository operator
  requires it.
- Prepare release notes before tagging.
- Push the `vX.Y.Z` tag only after the release PR has merged.
- Review the release workflow, package deployment, artifacts, checksums, and
  GitHub Release before any downstream synchronization.

## Coordinate External OTA Promotion

Public GitHub CI does not exercise the operator's full two-host OTA deployment.
Before promotion to `main` or downstream synchronization, use the external gate
described in [testing.md](testing.md) and record only generic evidence in public
repository artifacts: workflow/run link, target commit, result, and date. Keep
private hostnames, credentials, and deployment details outside this repository.

## Owner-Only Gates

Some actions intentionally stay with repository owners or maintainers:

- Changing protected-branch, ruleset, environment, or Trusted Publisher settings.
- Approving code-owner or maintainer-required reviews, when enabled.
- Promoting validated work to `main`.
- Creating and pushing release tags.
- Confirming package publication and release artifacts.
- Authorizing downstream synchronization after release evidence is reviewed.

For settings that live outside git, see
[github-repository-settings.md](github-repository-settings.md).
