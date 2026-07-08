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
| nightly | the **regression matrix**: supported RMW variants × canonical profiles (nominal **and** tight) × loads (steady, GOP/a-b pattern, single-stream anonymized replay) + **boundary pairs** — all band-asserted | **gate** (red nightly = fix-first; promotion requires the candidate's nightly gate green) |
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
- [ ] **Calibration**: measure run-to-run σ per metric on the CI runner class
  (K repeats of the canonical rows); commit the initial bands with provenance.
- [ ] **Benched-set registry**: curated row list (config/RMW × profile × load ×
  metrics) discoverable by workflows and the `benchmark` CLI.
- [ ] **Merge-gate row**: upgrade the existing benchmark-capacity E2E to
  band-asserting; keep it minutes-scale.
- [ ] **Nightly regression matrix**: blocking workflow over the registry (RMW
  variants × nominal + tight profiles × loads incl. single-stream anonymized
  replay), artifacts plus a machine-readable verdict the promotion gate can
  consume.
- [ ] **Boundary must-fail rows**: oracle inversion (assert the failure
  signature) + happy-red messaging; seed with the documented 18 KB @ 20 Hz
  pairs.
- [ ] **Findings ledger here** with the `Verification:` field and a schema
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
- [ ] **Boundary rows** — unit test: a bad side meeting the good oracle yields
  the happy-red verdict with finding/profile pointers.
- [ ] **Registry** — contract test: every row names an existing profile, load
  and metric set; workflows consume the registry (no hardcoded row lists).
- [ ] **Merge-gate row** — exercised by the merge gate itself (band-asserted
  benchmark-capacity E2E), runtime bounded by the workflow timeout.
- [ ] **Nightly matrix** — one deliberately injected regression (canary
  branch) turns the lane red end-to-end; documented one-off exercise.
- [ ] **Findings checker** — contract test: a finding without `Verification:`
  fails the ledger check.
- [ ] **Manual (explicit):** the first calibration is reviewed by the operator
  — band widths sane against the known noise findings (short-run
  untrustworthiness).

## Open questions

- **Width formula** — `k·σ` with which k; σ vs robust quantiles; per-metric
  floors. Settle empirically in the calibration step. *(The core ships
  overridable defaults per ratchet invocation: `--k 3`, `--floor-frac 0.02`,
  `--floor 0`; the center is the median of the runs, σ is the sample stdev.)*
- **K** — how many calibration repeats per row class (cost vs confidence).
- **Latency on shared runners** — wide band vs monitor-only; decide from the
  calibration numbers.
- **Full anonymized replay (S3) as a public row** — size/licensing permitting,
  or keep single-stream (S2) rows public and full replay in the harness.
- **Ratchet cadence for ambient nightly improvements** — immediate agent PR vs
  a weekly batch.
