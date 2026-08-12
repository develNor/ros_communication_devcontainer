# RFC 0007 — Performance regression gate: two-sided budgets, ratchet & blocking lanes

**Status:** Draft (design accepted; core machinery §1–§3 implemented — band
schema v2, two-sided `compare`, `ratchet` — remaining items staged as follow-up
issues in the operator harness roadmap) · **Scope:** turning the *deterministic* slice of
the benchmark suite into a gate — committed two-sided performance bands, the
ratchet workflow, noise calibration, lane placement, boundary must-fail rows,
and the living-findings hook · extends
[RFC 0005](0005-benchmark-genres-and-ci.md) (budgets, gate-vs-monitor), consumes
[RFC 0003](0003-metric-backbone.md) (the oracle) and
[RFC 0004](0004-network-profiles.md) (the environments)

## Summary

RFC 0005 gave benchmarks budgets and a regression compare, then placed every
benchmark as a nightly **monitor**: it trends, it alerts, it never blocks. That
was right for long sweeps; it is wrong as the *only* mode. A monitor no one is
forced to read is silent drift — especially in a solo-maintained codebase where
most changes are written by agents and CI is the guardrail.

This RFC upgrades the deterministic subset (emulated profiles, replay, loopback
— exactly what RFC 0005's own determinism rule already allows to gate) into a
**performance regression gate**:

- every gated row asserts its metrics against a **committed two-sided band**:
  leaving the band toward *worse* is a regression and fails; leaving it toward
  *better* **also fails** — with a happy message naming the exact **ratchet**
  command that tightens the band in the same change;
- bands are **calibrated, not chosen**: width comes from measured run-to-run
  variance on the runner class that executes the row;
- rows span the **supported RMW variants**, the canonical emulated profiles
  **including deliberately tight ones**, synthetic and anonymized-replay loads,
  and **boundary pairs whose bad side must keep failing**.

Green then means something strong: *this commit performs within the tightest
band the noise allows, on every supported configuration, and the documented
working envelope is still exactly where the docs say it is.*

## Motivation

Four failure modes of monitor-only budgets, all observed or foreseeable here:

1. **Silent drift.** An alert nobody is forced to act on is a log line. With
   per-commit development largely delegated to agents, "a human notices the
   trend" is not a mechanism.
2. **Improvements evaporate.** When a change makes latency or loss better,
   a one-sided budget stays slack; a later regression back to the old level is
   then *within budget* and invisible. Two-sided bands bank every improvement.
3. **Documented claims rot.** Measured boundaries and quirks (loss boundaries,
   discovery-burst spikes) are written down once and drift out of truth as code
   and configs change. Claims need re-proving, not re-reading.
4. **Realistic loads are not covered by point checks.** "The heartbeat matrix
   is green" says little about a GOP-shaped camera stream on a tight uplink;
   the gate must run realistic (replay) rows, not only synthetic steady load.

## Design

### 1. Two-sided bands, committed in-repo

Budget entries (RFC 0005 `budgets.jsonl`) become **bands**: per
`(row, profile, metric)` an interval `[lo, hi]` plus the metric's
better-direction. Verdicts: `REGRESSED` (out on the worse side), `IMPROVED`
(out on the better side), `WITHIN`. In gate lanes both exits are red.

Why not "compare against the previous commit's run" — the naive form of
*never gets worse*?

- **2× cost**: every gate run would need a parent-commit rerun;
- **compounding drift**: per-commit tolerance `t` admits a monotone slide of
  `n·t` over `n` commits; a committed band admits `t` in total until a human
  ratchets;
- **noise on both sides** of the comparison instead of one;
- **invisible in review**: a band change is a diff in the PR; a parent-relative
  pass is not reviewable;
- **irreproducible**: the accepted envelope lives nowhere.

"Not worse than the previous commit (minus noise)" follows transitively from
"within the last ratcheted band" — and is strictly stronger over time.

### 2. The ratchet workflow

`benchmark ratchet` rewrites bands from one or more `result.json` runs
(center from the runs, width per §3). Three uses:

- **Improvement red in a PR lane** — the failure message prints the exact
  ratchet command; the author runs it and commits the tightened band **in the
  same PR**, with a one-line cause note ("gop default changed → smaller
  keyframes"). The improvement is banked, visible, reviewed.
- **Ambient nightly improvement** — a next-morning ratchet PR (agent task),
  same cause-note rule.
- **Deliberate band moves** (new runner class, profile recalibration) —
  `ratchet --recalibrate` from K fresh repeats; bands are never hand-edited.

### 3. Bands are calibrated: noise, windows, fingerprints

A band is meaningless without provenance. Each band records:

- **runner-class fingerprint** (e.g. GitHub-hosted vs the private bench pair):
  `compare` refuses a mismatched fingerprint — a runner change forces a visible
  recalibration, never a silent shift;
- **window length** — with a per-metric minimum: short windows lie (the
  operator baseline: 100 s cellular runs swung 0–21 % loss). Emulated rows are
  steadier, but a gated window must still span ≥2 discovery periods (Cyclone
  SPDP default 30 s — RFC 0005 "discovery traffic is real load") or explicitly
  annotate the exclusion;
- **repeats and width** — width = `max(k·σ, floor)` with σ measured from K
  calibration repeats on the target runner class; floors keep one lucky
  calibration from minting an impossibly tight band.

Metric policy — prefer metrics whose value is pinned by the *emulated
bottleneck*, not by host timing:

| Metric class | Examples | Shared CI runners | Quiet private hosts |
|---|---|---|---|
| bottleneck-dominated | loss %, completeness, capacity breakpoint, recovery counts (lost-during-outage, backlog), wire/payload overhead | tight bands, **gate** | tight bands, gate |
| host-timing-dominated | latency p50/p99, jitter, wall-clock `t_recover` | wide bands or monitor-only | tight bands (harness lanes) |

### 4. Lanes: what gates where

Refines the RFC 0005 cadence table. The determinism rule is unchanged — real
links never gate; what changes is that deterministic rows now **block**:

| Trigger | What runs | Verdict |
|---|---|---|
| per-PR (merge gate) | existing point-check suite **+ one minutes-scale regression row** (default RMW × one rate-limited profile, loss/capacity band) | **gate** |
| nightly | the **regression matrix**: supported RMW variants × canonical profiles (nominal **and** tight) × loads (steady, GOP/a-b pattern, single-stream anonymized replay) + **boundary pairs** — band-asserted, except replay rows awaiting calibration, which assert their bag's whole-bag expect fragment instead | **gate** (red nightly = fix-first; promotion requires the candidate's nightly gate green) |
| nightly | long sweeps, ramps, frontier searches | monitor (unchanged) |
| on-demand | real WLAN/cellular characterization | monitor; calibrates the emulation (unchanged) |

The benched set is **curated and discoverable** — a registry the workflows and
the CLI read — so adding an RMW variant or profile extends the matrix without
editing CI, and the matrix stays deliberately small: a matrix nobody can afford
to keep green is a monitor with extra steps.

"Blocking nightly" concretely: a red nightly gate is treated like a red merge
gate — fixing (or ratcheting) it outranks feature work — and the operator's
private promotion pipeline (develop → main) refuses a candidate whose nightly
gate was not green. This repo's job is to expose a machine-readable verdict per
run; the promotion wiring is the harness's.

### 5. Boundary must-fail rows (negative results as assertions)

The suite documents *working-envelope boundaries* as good/bad pairs (e.g.
18 KB @ 20 Hz: 3.2 Mbit/s zero-loss vs 3.1 Mbit/s lossy; 18 ms vs 27 ms
jitter). A boundary row runs **both sides**:

- the good side must pass its oracle, like any gated row;
- the bad side must **fail** its oracle — the row asserts the documented
  failure signature (e.g. `loss ≥ x %`), not merely "did not pass".

If the bad side suddenly meets the good oracle, the envelope has widened: the
row goes **red with a happy message** — "3.1 Mbit/s now zero-loss: tighten the
boundary (move the bad-side profile), update the corresponding finding,
ratchet." Boundaries stay calibrated, documented limits cannot silently become
false, and genuine transport improvements are detected — and celebrated —
mechanically.

### 6. Living findings (the hook)

Findings — one reproducible effect per file — gain a **Verification** entry:
the CI row or test that re-proves the claim, or `manual: <why automation is
impossible>`. Findings whose Setup reproduces from public material (synthetic
load + a committable profile) live in **this repo's ledger** and are re-proven
by this repo's nightly rows — the code that could invalidate a finding fails
that finding's check in the same repo. Real-link and private-material findings
remain in the operator harness's ledger, re-proven only by owner
characterization runs. Quirks are findings too: e.g. Cyclone SPDP's default
30 s discovery burst producing periodic p99/max latency spikes on tight links
(RFC 0005 "honest limits"; `result.json` already carries the SPDP diagnostics)
becomes a finding with a row, instead of living only in RFC prose.

## Honest limits

- **Shared-runner variance bounds tightness.** GitHub-hosted runners set the
  floor for public bands; calibrate there, don't wish. Metrics that stay noisy
  after calibration get wide bands or stay monitors — a flaky gate is worse
  than none.
- **Two-sidedness has a fatigue cost.** Every ambient improvement demands a
  ratchet action. Floors keep bands from becoming hair-triggers; nightly
  ratchets may batch. A metric that ratchets weekly from ambient drift was
  never calibrated — recalibrate it.
- **The ratchet can be gamed** (ratchet-on-red without looking). Mitigations
  are procedural: the band diff is part of the reviewed change and requires
  the one-line cause note; a ratchet without an explanation is a review red
  flag.
- **Determinism is per-seed, not metaphysical** (RFC 0004/0005): seeded netem
  plus seeded load makes rows repeatable enough to gate; it does not make the
  emulation true. Real-link truth stays with calibration runs, monitor-only.
- **Public runners cannot seed netem.** The `tc` on GitHub-hosted runners
  rejects the netem `seed` option (observed during the canary exercise: `netem
  … loss 8% seed 1 -> What is "seed"?`), so *random* netem (loss/jitter/reorder)
  is not reproducible there and cannot gate. Public gate profiles therefore use
  **deterministic** shaping only — rate/delay bottlenecks, which are repeatable
  without a seed (contract-tested: `test_public_gate_profiles_use_deterministic_netem`).
  The seeded-*load* half of the policy (sized_publisher interval jitter) is
  application-level and works everywhere; seeded random netem stays in the
  operator harness lanes on a tc that supports it.
- **Matrix growth is the failure mode.** RMW variants × profiles × loads ×
  metrics multiplies budgets; the benched set must stay curated (the registry
  is a whitelist, not a crawler), and every row needs an operator-visible
  reason to exist.

## Implementation checklist

Ordered; each item lands with its validation. The operator harness roadmap
slices these into issues.

- [x] **Band schema v2** in `budgets.jsonl`: two-sided `[lo, hi]`,
  better-direction, runner-class fingerprint, window length, repeats, σ/floor
  provenance; clean rewrite of existing entries (repo rule: no backward
  compatibility). *(`benchmark.Band`/`BandProvenance`/`save_bands`/`load_bands`;
  every `result.json` now carries `sha` + `runner.fingerprint`; the packaged
  `budgets.jsonl` is rewritten with fingerprint `uncalibrated`, so it refuses to
  gate until the calibration step mints measured bands. Workflow doc:
  [docs/performance-bands.md](../performance-bands.md).)*
- [x] **Two-sided `benchmark compare`**: verdicts `REGRESSED` / `IMPROVED` /
  `WITHIN`, CI exit codes, improvement failure prints the exact ratchet
  command; refuses fingerprint mismatches. *(`cli_benchmark.benchmark_compare`;
  exit 0 WITHIN / 1 REGRESSED-or-refusal / 2 IMPROVED, `--monitor` reports
  without blocking.)*
- [x] **`benchmark ratchet`**: rewrite bands from `result.json` run(s);
  `--recalibrate` from K repeats; refuses to *widen* without `--recalibrate`.
  *(`benchmark.ratchet_band` + `cli_benchmark.benchmark_ratchet`; a plain
  ratchet also refuses runs from another runner class and preserves the
  calibrated width/provenance.)*
- [x] **Calibration**: measure run-to-run σ per metric on the CI runner class
  (K repeats of the canonical rows); commit the initial bands with provenance.
  *(`.github/workflows/benchmark-calibrate.yml` ran K=5 independent repeats per
  row on `github-hosted-linux-x86_64`; `benchmark calibrate` minted
  `budgets.jsonl` with full σ/floor/window provenance and the per-row evidence
  in [`calibration/`](../../calibration/README.md). Metric-policy outcomes recorded
  in Open questions: loss gates, latency is monitor-only, Zenoh overload-loss
  was demoted to a clean-arrival gate.)*
- [x] **Benched-set registry**: curated row list (config/RMW × profile × load ×
  metrics) discoverable by workflows and the `benchmark` CLI.
  *(`resources/benched-set.yaml` + `benched_set.py`; CLI `benchmark rows` /
  `row` / `calibrate` / `gate-summary`; committed per-row seeds, per-metric
  width floors, and the ≥60 s window rule are schema-enforced. Probe rows band
  `loss_pct`; latency percentiles ride along as monitor metrics per §3.)*
- [x] **Merge-gate row**: upgrade the existing benchmark-capacity E2E to
  band-asserting; keep it minutes-scale.
  *(`tests/e2e/test_benchmark_capacity.py::test_merge_gate_row_is_band_asserted`
  runs every `merge-gate` registry row via `benchmark row`. Reality check: the
  lane's previous single pytest invocation applied `-k link_latency` to all
  files, silently deselecting the entire benchmark E2E — the "existing
  merge-gate benchmark" had not run in the merge gate at all. The justfile now
  uses two invocations, and a contract test pins the selection.)*
- [x] **Nightly regression matrix**: blocking workflow over the registry (RMW
  variants × nominal + tight profiles × loads incl. single-stream anonymized
  replay), artifacts plus a machine-readable verdict the promotion gate can
  consume.
  *(`.github/workflows/benchmark-gate.yml`: schedule + dispatch, matrix from
  `benchmark rows --format ids`, per-row `result.json` + `verdict.json`
  artifacts, aggregated `benchmark-gate-summary` artifact via
  `benchmark gate-summary`; setup failure, `REGRESSED`, and unbanked
  `IMPROVED` are all red. S2 anonymized costmap/camera replay rows now cover
  nominal and tight profiles, carrying whole-bag expect fragments and GOP-aware
  metadata.)*
- [x] **Replay genre**: `genre: replay` for rows whose load is a measurement of
  a recording rather than a chosen number — the registry checks the load against
  that recording's provenance (cadence, window, mean payload, interval jitter),
  `benchmark row` asserts the whole-bag `expect` fragment on every gating run,
  and `delivered_count` / `delivered_hz` join the genre's metric set.
  *(`benched_set.GENRES`/`_validate_replay_load_matches_bag`/
  `replay_expect_failures`, `cli_benchmark.drive_replay` and the `EXPECT_FAILED`
  verdict, `benchmark._replay_metrics_from_result`. The four public S2 rows are
  `genre: replay`.)*
  **Why it is not `probe` with extra fields:** as probe rows the provenance was
  decoration — nothing checked the load against the bag, and the `expect`
  fragment was never read by anything that runs. The genre is what makes both
  assertions exist.
  **Public limit (honest):** the anonymized S2 bags are not public, so a public
  row replays the bag's *measured shape*, not its bytes. Byte-faithful replay
  stays in the operator harness (`_run_private_replay_rows`, harness #34).
- [ ] **Replay row bands**: calibrate the four S2 rows on the runner class
  (`.github/workflows/benchmark-calibrate.yml`, K repeats) and promote their
  gated metrics out of `monitor` in the same change that commits the bands.
  They land unbanded on purpose — a band is a measurement of the runner class
  and must not be hand-written, while the bag-derived expect fragment gates from
  day one. `tests/contract/test_benched_set_registry.py::test_public_s2_replay_rows_await_their_calibration`
  fails the moment a band appears without the promotion.
- [x] **Boundary must-fail rows**: oracle inversion (assert the failure
  signature) + happy-red messaging; seed with the documented 18 KB @ 20 Hz
  pairs. *(`GateRow.kind=boundary` + `benchmark row` now run good/bad sides,
  assert `good_oracle` and `failure_signature`, and emit `BOUNDARY_WIDENED`
  with finding/profile pointers plus the good-side ratchet command. The public
  nightly seed is the deterministic bandwidth pair
  `boundary-loss-18kb20hz-bandwidth-cyclone`; jitter-profile pairs remain
  explicit manual/operator checks until the runner class can install seeded
  random netem.)*
- [x] **Findings ledger here** with the `Verification:` field and a schema
  check; migrate the emulated-reproducible findings from the operator harness;
  seed the SPDP-30 s quirk finding.

## Validation checklist

- [x] **Two-sided compare** — unit tests for `REGRESSED` / `IMPROVED` /
  `WITHIN` in both better-directions; the improvement message contains the
  ratchet command.
  *(`test_benchmark.test_band_verdicts_are_two_sided_for_both_better_directions`,
  `test_cli_benchmark.test_compare_gates_both_sides_and_banks_improvements` —
  the latter also executes the printed ratchet command and re-compares.)*
- [x] **Ratchet** — unit roundtrip: run → ratchet → compare is `WITHIN`;
  refuses widening without `--recalibrate`; preserves provenance.
  *(`test_cli_benchmark.test_capacity_run_ratchet_compare_is_within`,
  `test_benchmark.test_ratchet_recenters_within_calibrated_width_and_preserves_provenance`,
  `test_benchmark.test_ratchet_refuses_moving_toward_worse_without_recalibrate`,
  `test_benchmark.test_recalibrate_mints_width_from_repeats_with_floor_guard`.)*
- [x] **Fingerprint** — unit test: compare across runner classes fails with
  the recalibration instruction.
  *(`test_benchmark.test_compare_refuses_cross_runner_fingerprints_with_recalibration_instruction`,
  `test_cli_benchmark.test_compare_refuses_bands_from_another_runner_class`.)*
- [x] **Boundary rows** — unit test: a bad side meeting the good oracle yields
  the happy-red verdict with finding/profile pointers.
  *(`test_cli_benchmark.test_boundary_row_requires_the_bad_side_to_stay_bad`
  and
  `test_cli_benchmark.test_boundary_row_happy_reds_when_bad_side_meets_the_good_oracle`.)*
- [x] **Registry** — contract test: every row names an existing profile, load
  and metric set; workflows consume the registry (no hardcoded row lists).
  *(`tests/unit/test_benched_set.py` for the schema;
  `tests/contract/test_benched_set_registry.py` for the cross-file contracts:
  profiles exist and use deterministic netem (public runners cannot seed it),
  every gated metric has a calibrated band, no orphan bands, workflows consume
  the registry and hardcode no row id, and the merge-gate lane cannot silently
  deselect the benchmark E2E.)*
- [x] **Merge-gate row** — exercised by the merge gate itself (band-asserted
  benchmark-capacity E2E), runtime bounded by the workflow timeout.
  *(`test_benchmark_capacity.py::test_merge_gate_row_is_band_asserted` in the
  `e2e-runtime-tools` lane of `pr-merge-gate.yml`; a 60 s probe window plus
  session setup keeps it minutes-scale.)*
- [x] **Nightly matrix** — one deliberately injected regression (canary
  branch) turns the lane red end-to-end; documented one-off exercise.
  *(Canary branch `issue-179-canary`, `benchmark-gate.yml` run 28957477074:
  gate-nominal gained unseeded netem `loss 8%`, which the 12 KB messages
  amplify through fragmentation to 59.6 % measured loss; `probe-loss-nominal-
  cyclone` REGRESSED its `[-0.5, 0.5]` band (exit 1) while every other row
  stayed WITHIN, so `benchmark-gate-summary` went `overall: red`
  (`red_rows: [probe-loss-nominal-cyclone]`) and the workflow exited red. Branch
  deleted after. The exercise surfaced two findings, now in Honest limits: a
  rate-limit injection **buffers** (17 s latency, monitor-only) rather than
  dropping — loss needs per-packet netem drop; and github-hosted `tc` rejects
  the netem `seed`, so a seeded-loss injection instead failed setup and turned
  the lane red via the MISSING-verdict path — itself a demonstration that a row
  which cannot run is red, not silently green.)*
- [x] **S2 anonymized replay rows** — contract and unit tests pin the public
  costmap/camera rows in the nightly lane, their whole-bag expect fragments, GOP
  metadata, and the delivery metrics that turn replay loss into a canary failure.
  *(`tests/contract/test_benched_set_registry.py::test_public_s2_replay_rows_carry_whole_bag_expect_metadata`,
  `tests/unit/test_benchmark.py::test_probe_metrics_include_completeness_and_payload_bandwidth`.)*
- [x] **Replay load stays the bag's shape** — the registry refuses a replay row
  whose cadence, window, mean payload or interval jitter has drifted from the
  provenance it commits, and refuses an `expect.min_count` the bag's own message
  count does not justify.
  *(`tests/unit/test_benched_set.py::test_replay_load_must_stay_the_bag_shape`,
  `::test_replay_expect_min_count_must_come_from_the_bag`,
  `::test_replay_metadata_is_refused_on_a_synthetic_probe_row`.)*
- [x] **Replay expect gates without a band** — an unbanded replay row runs
  against an empty band store instead of refusing, reports `WITHIN` when the
  whole bag arrived, and `EXPECT_FAILED` (exit 1, per-threshold reasons in the
  verdict) when it did not; `--monitor` reports the same verdict without
  blocking.
  *(`tests/unit/test_cli_benchmark.py::test_replay_row_gates_on_the_whole_bag_expect_without_any_band`,
  `tests/unit/test_benched_set.py::test_replay_expect_names_every_threshold_that_did_not_hold`.
  Exercised for real once against Docker on a dev laptop:
  `replay-costmap-nominal-cyclone` on `gate-nominal` delivered 1386 msgs at
  9.9 Hz with 0 % loss and 674 kbit/s payload bandwidth → `WITHIN`, exit 0,
  `bands: {}`; the same row pointed at a copy of the profile carrying netem
  `loss 40%` delivered 28 msgs at 0.20 Hz → `EXPECT_FAILED`, exit 1, all three
  thresholds named in the log and in `verdict.json`. That is the whole claim of
  landing before calibration: the row is already a gate.)*
- [ ] **Replay bands reviewed** — the calibration evidence for the four S2 rows
  (K repeats on the runner class) is reviewed and committed together with the
  promotion out of `monitor`. Pending: no calibration run has been made for
  them; `::test_public_s2_replay_rows_await_their_calibration` holds the step
  open. The `--no-compare` + `benchmark calibrate` path they need is exercised
  by `tests/unit/test_cli_benchmark.py::test_replay_calibration_run_reports_spread_for_the_metrics_a_band_would_take`.
- [x] **Findings checker** — contract test: a finding without `Verification:`
  fails the ledger check.
- [x] **Manual (explicit):** the first calibration is reviewed — band widths
  sane against the known noise findings (short-run untrustworthiness). *(Agent
  review of the K=5 evidence in `calibration/`: clean rows (nominal, gop,
  sub-capacity zenoh) σ≈0 → floor-limited ±0.5 pp bands; overload rows σ
  3.3–6.1 pp → ±10–18 pp; the one row whose overload loss swung 23–61 %
  (σ 14.6 pp) was demoted to a clean gate rather than banded on noise; latency
  p95 σ up to 212 ms with multi-second medians → monitor-only. Recorded in Open
  questions; owner may re-review the committed `budgets.jsonl` + reports.)*

## Open questions

- **Width formula** — `k·σ` with which k; σ vs robust quantiles; per-metric
  floors. *Settled for the public rows (first calibration, K=5,
  `github-hosted-linux-x86_64`):* `k=3` (3σ), median center, sample stdev, and
  a committed per-row `floor` on `loss_pct` (0.5 pp) so the clean rows
  (measured σ≈0) still get a real width instead of a hair-trigger; capacity
  keeps the 2 %-of-center `floor_frac`. Robust quantiles were unnecessary at
  K=5 — the median already absorbs the one visible outlier per row.
- **K** — how many calibration repeats per row class (cost vs confidence).
  *Settled at K=5 for the public rows:* enough to separate the clean rows
  (loss σ≈0) from the overload rows (loss σ 3.3–6.1 pp) and to expose the one
  row whose overload loss would not hold still (see below); the calibration
  workflow keeps K a dispatch input for re-tuning.
- **Latency on shared runners** — wide band vs monitor-only; decide from the
  calibration numbers. *Settled: monitor-only.* The measured p95 σ on the
  shared runner spanned 0.3 ms (clean rows) to 212 ms (Cyclone overload), and
  the overload rows sit at multi-second p95 medians from buffer bloat —
  host-timing- and buffer-dominated, not bottleneck-pinned. Latency percentiles
  are recorded in every verdict's `monitor_metrics` and never gate publicly;
  the operator's quiet-host harness lanes may still band them (§3 table).
- **Overload-loss as a public gate** — *decided per RMW from the calibration.*
  Loss under *sustained overload* is bottleneck-pinned enough to gate for
  Cyclone (σ 6.1 pp → ±18 pp band) and FastDDS single-datagram (σ 3.3 pp →
  ±10 pp) — wide but real bands that catch a graceful-shedding regression. It
  is **not** stable enough for Zenoh: at 2× uplink its loss swung 23–61 % across
  K=5 (σ 14.6 pp, banding to a meaningless `[-10, 78]`), so that row was moved
  to a sub-capacity clean-arrival gate (now 0 % loss, σ 0). Zenoh's overload
  behaviour is left as a finding to characterize, not a flaky gate
  (honest-limits rule).
- **Full anonymized replay (S3) as a public row** — size/licensing permitting,
  or keep single-stream (S2) rows public and full replay in the harness.
- **Ratchet cadence for ambient nightly improvements** — immediate agent PR vs
  a weekly batch.
