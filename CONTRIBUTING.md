# Contributing

This is the canonical development workflow for `rosotacom`.

## Development Setup

Prerequisites:

- Python 3.10 through 3.14 when mirroring CI.
- Docker for runtime and E2E checks.
- `just` for local workflow commands.
- GitHub CLI `gh` for issue and PR workflows.

Set up the repository from the root:

```bash
just setup
```

This creates `.venv`, installs `.[dev]`, and installs pre-commit hooks.

## Local Checks

Run individual checks from the repository root:

```bash
just lint
just typecheck
just test-unit
just test-contract
just test-nondocker-cov
just docs
just package
just test-e2e-smoke
```

Before opening or updating a ready PR, run:

```bash
just check
just test-e2e-smoke
```

`just check` runs linting, type checking, host tests under coverage, docs checks,
and package validation. `just test-e2e-smoke` runs the required Docker-backed
heartbeat smoke matrix separately so failures are easier to inspect.

## Branch And Merge Policy

`main` is protected. Do not push directly to `main`.

All changes must go through a pull request. Start each change from an up-to-date
`origin/main` topic branch:

```bash
git fetch --prune
git switch -c <type>/<short-topic> origin/main
```

Keep the change small and coherent. Update tests, README, docs, examples, and
packaging metadata when CLI, config, package, Docker, or public runtime behavior
changes.

## CI Policy

The required merge gate for `main` is:

```text
ci-success
```

Draft PRs run lightweight checks. Ready PRs, pushes to `main`, and merge queue
entries run the full gate: Python 3.10 through 3.14 non-Docker checks, coverage,
package validation, and Docker smoke.

Codecov uses the uploaded `nondocker` report. Docker E2E remains behavioral
validation and is not collected for coverage.

See [docs/ci.md](docs/ci.md) for details and
[docs/github-repository-settings.md](docs/github-repository-settings.md) for
repository settings that live outside git.

## Release Workflow

Releases are built from tags in the form `vX.Y.Z`. Release PRs must include
`docs/release-notes/vX.Y.Z.md`, copied from
[docs/release-notes/TEMPLATE.md](docs/release-notes/TEMPLATE.md).

After the release PR has merged:

```bash
git switch main
git fetch --prune
git pull --ff-only
git tag vX.Y.Z
git push origin vX.Y.Z
```

Tag pushes publish to PyPI through Trusted Publishing and create a GitHub
Release. Manual release workflow dispatch publishes to TestPyPI.
