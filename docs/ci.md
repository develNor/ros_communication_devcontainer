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

The merge gate runs workflow lint, dependency/security review, runtime/build asset lint, Python 3.10 through 3.14 non-Docker checks, package validation, and the Docker single-machine smoke matrix (one matrix job per slice, thirteen in parallel):

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
just test-e2e-slice heartbeat
just test-e2e-slice chatter
just test-e2e-slice occupancy-grid
just test-e2e-slice sized-payload
just test-e2e-slice remote-assist
just test-e2e-slice remote-assist-streams
just test-e2e-slice runtime-tools
just test-e2e-slice media
just test-e2e-slice concurrency
just test-e2e-slice benchmark-ab
just test-e2e-slice benchmark-replay
just test-e2e-slice benchmark-capacity
just test-e2e-slice benchmark-capacity-cases
```

The Python 3.12 leg uploads `coverage.xml` to Codecov with the `nondocker` flag.
Docker E2E is not collected for coverage.

## Docker E2E

`just test-e2e-smoke` runs the entire local single-machine smoke matrix through Docker. In the CI merge gate, the same collection runs as thirteen parallel
jobs, one per slice, named `e2e-<slice>`. Each smoke run writes generated
config, catmux pane logs, Docker logs when available, and the smoke verification
log under `session-instances/`; collect that directory as the first debugging
artifact when an E2E job fails.

### The e2e image is published once, not rebuilt per job

Every e2e job used to build the project image from scratch. #236 measured that
at 154s per job — 57s pulling the multi-GB ROS base from Docker Hub, 40s
exporting layers, 47s of apt and pip — which at thirteen slices is 33 minutes of
runner time and thirteen Hub pulls on the critical path of every merge gate.

The `image` job now publishes those images to
`ghcr.io/<owner>/rosotacom-e2e` and each slice pulls instead of building. GHCR
is co-located with the runners and has no Hub rate limit, so this is one
transfer where there used to be a transfer plus a build. Measured on run
31678648935: **154s → 70s per job**, and the slowest slice's pytest run fell
from 8m19s to 6m50s.

The tag is the whole safety argument. It is a SHA-256 of everything that decides
what the image contains — the `docker build` command ros2docker renders from a
config (base image and its pinned digest, `APT_PACKAGES`, `PIP_PACKAGES`, the
build UID) and every file in the context it stages (Dockerfile, entrypoint, any
baked packages). Change any of them and the name changes, so "the published
image is stale" is not a state this can reach, which is the objection #226
raised against caching the image inline.

Nothing ever *accepts* a reference. `rosotacom image references` derives them;
the publisher builds and pushes only what is missing, and only under the name
its own inputs hash to (`rosotacom image build --reference`). A consumer is told
a repository, in `ROSOTACOM_IMAGE_CACHE`, and derives the tag itself:

```bash
# What this project's build inputs are called
rosotacom image references --repository ghcr.io/owner/rosotacom-e2e

# Adopt them instead of building, anywhere rosotacom would build
ROSOTACOM_IMAGE_CACHE=ghcr.io/owner/rosotacom-e2e just test-e2e-slice heartbeat
```

With the variable unset, nothing changes: rosotacom builds as it always did. Set
and the image absent, the job fails rather than falling back — a miss means the
publisher did not run or did not agree with this tree, and a silent rebuild
would hide both while costing exactly what this removes. The one place that
decision is made is the `image` job, which reports its repository only after
confirming every reference resolves; a read-only `GITHUB_TOKEN` (a fork PR,
Dependabot) therefore leaves the slices building as before.

Two properties are worth keeping in mind when changing this:

- **Publisher and consumer read one list.** `2_native_chatter` builds a second
  image from a different package list, so `rosotacom image references` walks the
  project config *and* every scenario application. Publishing only the project
  image would leave that slice adopting something nobody published.
- **The release and nightly lanes still build from scratch**, as does the
  nightly `image-scan`. That is deliberate: they are what would notice if a
  published image and a fresh build ever stopped agreeing.

Content-addressed tags accumulate in the GHCR package — one per distinct set of
build inputs, so roughly one per change to the base digest or package lists.
Nothing prunes them yet.

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

The costs are warm pytest `call` durations — medians over seven merge-gate runs
of 2026-08-11/12. On top of them each job pays a fixed
`RUNNER_SETUP_SECONDS + IMAGE_BUILD_SECONDS` (2m05s: 55s of runner setup, then
the project image obtained inside whichever test runs first). Because that
constant is per job and not per test, balancing warm costs balances wall clock.

`IMAGE_BUILD_SECONDS` models the merge gate, where that image is pulled rather
than built (see above); the release and nightly lanes still build and pay the
older 154s. One slice does not currently reproduce its recorded split: `media`
spends about 24s more than its two costs predict, and it is the critical path,
so it is the first thing to remeasure.

### How many slices

`floor_seconds()` is `fixed + slowest single test` — the fastest any partition
of this suite could be, because one job has to run that test and pays the fixed
cost like every other. It is currently **6m32s**, of which 1m10s is the image
(7m56s before #236 published it instead of rebuilding it per job).

Thirteen slices sit at 7m14s, 1.11x the floor. That is the point of choosing N
from the numbers rather than from taste: six balanced slices were 13m37s,
twelve reach the floor, and past twelve every extra job is pure cost. The
contract test asserts distance to the floor rather than the spread between
slices — spread is compressed toward 1.0 by the fixed cost as N grows, so it
keeps reading fine while the gate stops improving.

Runner minutes are not the constraint here (this repository is public, so
standard GitHub-hosted runners are free); the concurrent-job cap is, and
thirteen stays under it.

To refresh the numbers, read a merge-gate run: every e2e invocation runs
`--durations=0`, so each job prints the cost of every test it ran. A test that
ran first in its job carries the image build and needs `IMAGE_BUILD_SECONDS`
subtracted. `tests/contract/test_workflow_contracts.py::test_e2e_slices_stay_close_to_the_floor`
fails when the slowest slice passes 1.25x the floor.

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
