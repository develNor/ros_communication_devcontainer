# CI

This repository keeps contributor checks aligned with `justfile` targets so the
same commands can run locally and in GitHub Actions.

## Pull Requests

Draft PRs run `.github/workflows/pr-lightweight.yml`:

```bash
just lint
just typecheck
just test-unit
```

Ready PRs, merge queue entries, and pushes to `main` run
`.github/workflows/pr-merge-gate.yml`. The required aggregate branch protection
check is:

```text
ci-success
```

The merge gate runs Python 3.10 through 3.14 non-Docker checks, package
validation, and the Docker heartbeat smoke matrix:

```bash
just lint
just typecheck
just test-nondocker-cov
just docs
just package
just test-e2e-smoke
```

The Python 3.12 leg uploads `coverage.xml` to Codecov with the `nondocker` flag.
Docker E2E is not collected for coverage.

## Docker E2E

`just test-e2e-smoke` runs the local heartbeat smoke matrix through Docker. It
is a required merge-gate job because `rosotacom` exists to orchestrate
Docker-backed ROS communication sessions. Each smoke run writes generated
config, catmux pane logs, Docker logs when available, and the smoke verification
log under `session-instances/`; collect that directory as the first debugging
artifact when an E2E job fails.

## Nightly And Maintenance

`.github/workflows/nightly-e2e.yml` runs Docker E2E on a schedule and by manual
dispatch. `.github/workflows/image-scan.yml` builds the default communication
image and uploads an advisory Trivy report.

Dependabot is configured for weekly grouped GitHub Actions and Python dependency
updates.
