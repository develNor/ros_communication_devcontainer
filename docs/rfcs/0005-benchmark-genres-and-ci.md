# RFC 0005 — Benchmark genres & CI distribution

**Status:** Draft — design agreed, not yet implemented · **Scope:** the
characterize/sweep question-mode and its place in the cadence · extends
[RFC 0001](0001-expectation-driven-test-suite.md) (cadence), consumes
[RFC 0003](0003-metric-backbone.md) (the oracle) and
[RFC 0004](0004-network-profiles.md) (the environments it runs against)

## Summary

The current suite asks **"does config X meet contract C?"** — a *point check* at a
fixed condition (RFC 0001/0002). It cannot answer the operator's
characterization questions: *what is the largest message that still arrives? at
what load does latency become unbearable? how fast does it recover from an
outage?* Those are a different genre — a **search over a parameter**, not a check
at a point — and they produce **numbers tracked over time** (a budget/baseline),
not a pass/fail.

This RFC defines that genre as two shapes — **sweep/capacity** and
**perturbation/recovery** — built from ingredients that already exist (the
`sized_publisher`, the metric backbone, network profiles), and places it in the CI
cadence with one hard rule: **only reproducible conditions may gate.**

## Motivation

The operator already runs these by hand — size→latency sweeps, "largest reliable
message" searches, long-duration capacity (iperf) runs, and outage/recovery
observations (all recorded in the private network baseline). They are the most
valuable characterization work and the least reproducible as manual notes. The
point-check suite (0001/0002), the metric backbone (0003) and network profiles
(0004) are the *ingredients*; what is missing is the **driver** that varies one
knob and records the response, and the **budget** that flags regression.

## The genre is one cell of the grid

A benchmark is the **characterize/sweep** cell: the same instrument as a point
check, with a *driver* on top that varies a knob and records the response. Every
benchmark is:

> **environment driver (a profile, RFC 0004) × load driver (sized/patterned
> publisher) × the metric backbone as oracle (RFC 0003) → a recorded result.**

No new conceptual machinery — two ways of driving it.

### Genre 1 — Sweep / capacity (boundary search)

A control parameter (message **size**, **rate**, topic **count**, aggregate
**bandwidth**) and a pass/fail oracle from the backbone (`loss < p` **and**
`latency < L` over a window). Output: the **capacity** — the highest value that
still passes, per profile.

- **Binary search** for the single breakpoint (cheap — the "largest reliable
  message" number) → gate-able.
- **Linear ramp** for the whole response curve (latency-vs-load) → informative,
  nightly.
- The load surface is **2-D (size × rate)**: the baseline's 10 KB @ 10 Hz ≈ 500 ms
  vs @ 1 Hz ≈ 170 ms proves rate matters independently of size. A "capacity" is
  therefore a **frontier** in (size, rate); a single number is one slice — state
  the slice.
- **Instrument:** the existing `sized_publisher`, extended with an **a/b size
  pattern** (e.g. `4×0 B + 1×70 KB`) so it reproduces the irregular-size
  head-of-line behaviour the baseline identifies as the real failure mode (this
  extension is already on the operator's wishlist).

### Genre 2 — Perturbation / recovery (transient response)

Apply a **step change** to the environment — a *timeline profile* (RFC 0004), e.g.
30 s good → outage → restore — and measure the **recovery dynamics**:

- `t_recover` — time-to-first-message after restore;
- `t_steady` — time-to-steady-state;
- **backlog / burst on recovery** — messages arriving simultaneously (the baseline:
  "the part that actually hurts");
- **lost-during-outage**, per topic, from the seq backbone (RFC 0003);
- did latched values re-arrive; did reliable QoS replay the gap or best-effort drop
  it; did a `lifespan` cap bound the reconnect burst (the baseline's mitigation).

A recovery benchmark **is** a timeline profile plus recovery-specific metrics.

## Budgets & baselines — the verdict for benchmarks

A benchmark does not pass/fail against an absolute bound; it **regresses or not
against a recorded baseline**. Call the recorded envelope a **budget**:

- per `(SHA, profile, genre)`: the capacity numbers / recovery times, with a
  tolerance band;
- regression check = today's envelope vs the baseline ± tolerance → the
  "nothing got materially worse" report the operator wants;
- a budget is the **benchmark analogue of `expect`**: same shape (a contract on a
  measured quantity), different verdict — *"regressed vs baseline"* not *"violated
  an absolute bound"* — and calibrated **per profile**, mirroring the
  conditional-expectation split of RFC 0004 (the same correctness/performance
  symmetry).

Results reuse the forensic home of RFC 0003 (`status.json` / `events.jsonl`
generalized to a small per-run record), so budgets trend over commits.

## CI distribution (extends RFC 0001)

**The hard rule: determinism decides what may gate.** A gate blocks promotion, so
it must be reproducible → only loopback, replay and **emulated-profile** runs
(RFC 0004 rungs 0–2) qualify. Real WLAN/cellular (rungs 3–4) is non-deterministic
→ **monitor only**: it trends and alerts, it never blocks.

| Trigger | What runs | Verdict |
|---|---|---|
| **per-PR** (fast) | unit + contract + a loopback point-check slice | **gate** (RFC 0001) |
| **per-promotion** (manual) | full example/replay suite + one canonical emulated-cellular profile (point checks) | **gate** |
| **nightly** | benchmark sweeps (capacity binary-search + coarse ramp) + recovery-profile runs + a profile grid, vs budget/baseline | **monitor** (budget; alerts, does not block) |
| **on-demand** | real WLAN/cellular characterization | **monitor**; calibrates the emulation |

**Why benchmarks are nightly, not per-commit:** sweeps are long (a ramp visits
many points; a recovery run replays a whole timeline; cellular capacity needs the
long-duration windows the baseline proved necessary — short runs are worthless).
They yield a trend, not a blocking verdict.

The *generic* model lives here; the **specific runner topology** (which machines,
which CI system, single- vs multi-machine split) is FZI-private and lives in the
harness, not in this public repo.

## Honest limits

- **Capacity is a frontier, not a scalar.** Any single "largest reliable X" is a
  slice of (size × rate × profile); always report the slice. Start with a few
  canonical slices, not the full frontier.
- **Binary search assumes monotonicity.** Real cellular's non-monotonic loss (the
  baseline's 5 Mbit/s worse than 6 Mbit/s) can fool a single search → confirm with
  repeats / medians of long runs. The "short runs are worthless" rule applies to
  benchmark windows too.
- **Emulated budgets are reproducible, not true** (RFC 0004). Real-link monitors
  drift and must never gate.
- **Recovery numbers depend on outage semantics** (`loss 100%` vs link-down — the
  open question in RFC 0004); the same `t_recover` means different things under
  each.
- **Averages hide the failure mode.** Head-of-line blocking and reconnect bursts
  are only visible per-message (RFC 0003) and under irregular load (the a/b
  pattern) — a benchmark on averages would miss exactly what matters.

## Build order

1. **Capacity binary-search driver** — `sized_publisher` + a/b pattern + the
   backbone oracle → "largest reliable message" per profile. Smallest slice;
   reuses RFC 0003 + 0004. *(start here)*
2. **Budget store + regression compare** — the nightly monitor verdict.
3. **Recovery driver** on timeline profiles (RFC 0004 step 4) + the recovery metric
   set.
4. **Linear-ramp curves** — the latency-vs-load surface, for trend.
5. **Wire the nightly monitor into CI** — gate-vs-monitor; the harness wires the
   actual runner.

## Open questions

- **Scalar vs frontier reporting.** A few canonical (size, rate) slices, or the
  full frontier? Start with slices.
- **Budget tolerance.** Absolute vs relative band; how many baseline long-runs
  establish it (the long-duration finding implies several).
- **a/b-pattern publisher home.** Extend `sized_publisher` vs a new node.
- **Gate-relevant recovery metrics.** Which of `{t_recover, backlog,
  lost-during-outage, latched-rearrival}` are budgeted vs informational.
