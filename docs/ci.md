# CI

This repository keeps contributor checks aligned with `justfile` targets so the
same commands can run locally and in GitHub Actions. For how the test tiers fit
together, see [testing.md](testing.md).

## Branch model

This shared codebase is used by repositories with different default branches,
but one stable release line:

- topic PRs are gated into the repository's active development branch by the
  GitHub checks below;
- every commit that lands there with green CI is published as a `X.Y.devN`
  pre-release, so any commit is consumable without a release decision;
- **`main`** is the stable line a repository synchronises upstream from;
- moving development work to `main` is a deliberate maintainer operation and
  may require an external OTA gate;
- releases are tags `vX.Y.Z` cut from a validated commit on the release line —
  in this repository `develop`, its default branch (see
  [release.md](release.md)).

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

The merge gate runs workflow lint, dependency/security review, runtime/build asset lint, Python 3.10 through 3.14 non-Docker checks, package validation, and the Docker single-machine smoke matrix (one matrix job per slice, six in parallel):

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
# Parallel E2E lanes, one per slice:
just test-e2e-slice core
just test-e2e-slice transforms
just test-e2e-slice remote-assist
just test-e2e-slice runtime-tools
just test-e2e-slice benchmark-capacity
just test-e2e-slice media-concurrency
```

The Python 3.12 leg uploads `coverage.xml` to Codecov with the `nondocker` flag.
Docker E2E is not collected for coverage.

## Docker E2E

`just test-e2e-smoke` runs the entire local single-machine smoke matrix through Docker. In the CI merge gate, the same collection runs as six parallel jobs
(`e2e-core`, `e2e-transforms`, `e2e-remote-assist`, `e2e-runtime-tools`,
`e2e-benchmark-capacity`, `e2e-media-concurrency`). Each smoke run writes generated
config, catmux pane logs, Docker logs when available, and the smoke verification
log under `session-instances/`; collect that directory as the first debugging
artifact when an E2E job fails.

### Balancing the e2e slices

A slice is not its own pytest invocation. `just test-e2e-slice <name>` runs the
monolith's collection with `--e2e-slice=<name>`, and everything the named slice
does not own is deselected. Which tests a slice owns, and what each one costs,
is `E2E_SLICES` in `tests/e2e/conftest.py`; `just e2e-slice-costs` prints the
resulting balance. Moving a test between slices is one line there — the
workflow matrices only name slices.

That shape exists because the previous one, per-slice file lists and `-k`
filters, could not say what it ran. `-k "remote_assist"` silently missed the
`[remote-assist-anonymized-*]` parameters, and `-k "remote_assist"` and
`-k "anonymized"` silently *shared* one 262s test, which the gate therefore ran
twice a run. A partition can do neither: an unowned test fails every slice job
with a usage error, and a test owned twice fails
`test_e2e_slices_partition_the_whole_suite`.

The costs are warm pytest `call` durations — medians over six merge-gate runs
of 2026-08-11/12. On top of them each job pays a fixed
`RUNNER_SETUP_SECONDS + IMAGE_BUILD_SECONDS` (~3m30s: runner setup, then the
project image built inside whichever test runs first). Because that constant is
per job and not per test, balancing warm costs balances wall clock, and adding
slices buys less than the arithmetic suggests: six balanced slices are
predicted at ~13m30s against ~10m for twelve, at twice the runner minutes.

To refresh the numbers, read a merge-gate run: every e2e invocation runs
`--durations=0`, so each job prints the cost of every test it ran. A test that
ran first in its job carries the image build and needs `IMAGE_BUILD_SECONDS`
subtracted. `tests/contract/test_workflow_contracts.py::test_e2e_slices_stay_balanced`
fails when the recorded spread passes 1.5x.

## Performance Regression Gate

The deterministic benchmark rows gate against committed two-sided bands
([performance-bands.md](performance-bands.md), RFC 0007):

- the `merge-gate` rows of the benched-set registry run inside the
  `e2e-benchmark-capacity` lane (band-asserted benchmark-capacity E2E);
- `.github/workflows/benchmark-gate.yml` runs the `nightly` rows on schedule
  and on manual dispatch. Its matrix comes from
  `rosotacom benchmark rows --format ids`; each row uploads `result.json` plus
  a machine-readable `verdict.json`, and the `verdict` job aggregates them into
  the `benchmark-gate-summary` artifact. A red gate — setup failure,
  `REGRESSED`, or an unbanked `IMPROVED` — is fix-first, like a red merge gate;
- `.github/workflows/benchmark-calibrate.yml` (manual dispatch) reruns the
  runner-class calibration: K repeats per row, then `benchmark calibrate`
  mints `budgets.jsonl` and the [calibration reports](../calibration/README.md)
  as the `calibrated-bands` artifact to review and commit.

## Nightly And Maintenance Checks

`.github/workflows/nightly-e2e.yml` runs the local smoke slice plus the generated
RMW matrix nightly on GitHub-hosted infrastructure and also supports manual
dispatch. It is not the private OTA promotion gate.
`.github/workflows/image-scan.yml` builds the default communication image and
uploads an advisory Trivy report.

Dependabot is configured for weekly grouped GitHub Actions and Python dependency
updates.
