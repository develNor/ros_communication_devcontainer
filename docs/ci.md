# CI

This repository keeps contributor checks aligned with `justfile` targets so the
same commands can run locally and in GitHub Actions. For how the test tiers fit
together, see [testing.md](testing.md).

## Branch model

This shared codebase is used by repositories with different default branches,
but one stable release line:

- topic PRs are gated into the repository's active development branch by the
  GitHub checks below;
- **`main`** is the stable release line;
- moving development work to `main` is a deliberate maintainer operation and
  may require an external OTA gate;
- releases are tags `vX.Y.Z` cut from `main` (see [release.md](release.md)).

The OTA tier is not part of this repository's public CI. An operator-supplied
external runner provides it as a manual promotion gate, as described generically
in [testing.md](testing.md). A commit, mirror update, or green public CI run does
not by itself authorize an external runner or a `main` promotion.

## Pull Requests

Draft PRs run `.github/workflows/pr-lightweight.yml`:

```bash
just lint
just typecheck
just test-unit
```

Ready PRs, merge queue entries, and pushes to `main` or `develop` run
`.github/workflows/pr-merge-gate.yml`. The required aggregate branch protection
check is:

```text
ci-success
```

The merge gate runs workflow lint, dependency/security review, runtime/build asset lint, Python 3.10 through 3.14 non-Docker checks, package validation, and the Docker single-machine smoke matrix (divided into 5 parallel lanes):

Python 3.10–3.14 are supported for the host CLI; Python 3.12 is the reference
interpreter for packaging and Docker E2E jobs.

```bash
just lint-workflows
just lint-build
just lint
just typecheck
just test-nondocker-cov
just docs
just package
# Parallel E2E lanes:
just test-e2e-core
just test-e2e-transforms
just test-e2e-remote-assist
just test-e2e-runtime-tools
just test-e2e-concurrency
```

The Python 3.12 leg uploads `coverage.xml` to Codecov with the `nondocker` flag.
Docker E2E is not collected for coverage.

## Docker E2E

`just test-e2e-smoke` runs the entire local single-machine smoke matrix through Docker. In the CI merge gate, E2E testing is divided into parallel jobs (`e2e-core`, `e2e-transforms`, `e2e-remote-assist`, `e2e-runtime-tools`, `e2e-concurrency`) which run concurrently to minimize wall-clock time. Each smoke run writes generated
config, catmux pane logs, Docker logs when available, and the smoke verification
log under `session-instances/`; collect that directory as the first debugging
artifact when an E2E job fails.

## Nightly And Maintenance Checks

`.github/workflows/nightly-e2e.yml` runs the local smoke slice plus the generated
RMW matrix nightly on GitHub-hosted infrastructure and also supports manual
dispatch. It is not the private OTA promotion gate.
`.github/workflows/image-scan.yml` builds the default communication image and
uploads an advisory Trivy report.

Dependabot is configured for weekly grouped GitHub Actions and Python dependency
updates.
