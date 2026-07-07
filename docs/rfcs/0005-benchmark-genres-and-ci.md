# RFC 0005 — Benchmark genres & CI distribution

**Status:** Implemented (pure driver/verdict logic + host tests in
`rosotacom.benchmark`; live runs and the nightly runner remain FZI-private) ·
**Scope:** the characterize/sweep question-mode and its place in the cadence · extends
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
- **Bounded, not unbounded.** Every sweep stops at an operationally-relevant
  ceiling (a configured max size / rate / bandwidth), not "until it breaks at any
  cost." These are **OTA tests**: the goal is behaviour under realistic —
  especially **simulated-degraded** — networks, not the theoretical maximum of a
  pristine link. On the unshaped / LAN rung in particular the sweep must stay
  within a bounded budget and **never saturate the shared LAN**; push the limits on
  the emulated bad-network profiles instead, where the profile's own rate cap
  bounds the offered load.

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
- **Discovery traffic is real load.** Cyclone DDS SPDP discovery uses the same
  OTA link as payload data. Keeping the default `SPDPInterval=30s` is honest for
  end-to-end DDS behavior, but tight shaped probes can show regular p99/max
  latency spikes from SPDP bursts. A longer benchmark-only SPDP interval is useful
  for payload characterization, but it makes the run less representative and
  delays stale-peer/reconnect detection.

## Implementation checklist

Roughly in dependency order; the capacity driver is the smallest self-contained
slice and reuses RFC 0003 + 0004.

- [x] Extend `sized_publisher` with an a/b size pattern (e.g. `4×0 B + 1×70 KB`) to
  drive irregular-size load (`sized_publisher` `pattern`/`size_a`/`size_b`; pure
  expansion in `benchmark.expand_size_pattern`).
- [x] Build the capacity binary-search driver: sweep size/rate against the backbone
  oracle (`loss < p` **and** `latency < L` over a window) → the breakpoint per
  profile, with the slice (size/rate) stated (`benchmark.capacity_binary_search`,
  `find_capacity`, `oracle_passes` / `oracle_passes_topic`).
- [x] Bound every sweep with a configured max (size / rate / bandwidth) and a
  shared-link guard, so an unshaped / LAN run never saturates the shared network;
  the sweep's focus is the emulated degraded profiles, not a pristine link's
  ceiling (`benchmark.SweepBounds`, `size_ceiling`, `guard_shared_link`).
- [x] Add the budget store (per `(SHA, profile, genre)`) and the regression compare
  against a recorded baseline ± tolerance (`benchmark.BudgetEntry`/`save_budget`/
  `load_budget`/`find_baseline`, `compare_to_budget`).
- [x] Make live benchmark sessions RMW-selectable per invocation and default the
  packaged benchmark sessions to Cyclone DDS, while keeping `--rmw fastdds` and
  other supported session RMW values available for explicit comparisons
  (`cli_benchmark --rmw`, artifact-backed session copy).
- [x] Keep Cyclone DDS `SPDPInterval=30s` as the tuned default, add an explicit
  benchmark/session override for quiet-discovery probes, and annotate fixed
  probes when SPDP traffic can plausibly distort tight-link tail latency
  (`--cyclone-spdp-interval`, `shared.rmw.ota.cyclone.spdp_interval`,
  `result.json.context.diagnostics.cyclonedds_spdp`).
- [x] Persist a self-contained per-run `result.json` with selected RMW, local/OTA
  mode, configured load, offered bandwidth, profile shaping context, thresholds,
  verdict, and per-topic loss/latency/jitter metrics; keep `budgets.jsonl` as the
  budget/baseline feed (`cli_benchmark._write_benchmark_result`).
- [x] Make OTA benchmark runs default to the selected genre's benchmark session
  instead of a hard-coded project target, while allowing `--target` /
  `--target-type` for project-specific sessions or scenarios
  (`cli_benchmark._benchmark_ota_target`).
- [x] Add a first-class `rosotacom ota-benchmark` command so OTA runs use the
  same benchmark grammar as local runs and only need deployment peer bindings
  such as `--peer a=seat_tks --peer b=majestic_tks` for the common case.
- [x] Add an interactive benchmark operator view that opens a local tmux session
  with a high-level run window, one fullscreen catmux attach window per local
  peer, a network window split into qdisc status and tc/netem command logs, and a
  one-shot final result window (`cli_benchmark --interactive`).
- [x] Let shaped OTA benchmark runs choose explicit sudo handling:
  passwordless non-interactive sudo for unattended runs, or a local per-peer
  askpass prompt that feeds `sudo -S` over SSH stdin for attended operator runs
  (`--sudo-mode`, `cli._ota_preflight`, `cli._peer_command_runner`).
- [x] Build the recovery driver on timeline profiles (RFC 0004) + the recovery
  metric set (`t_recover`, `t_steady`, backlog/burst, lost-during-outage, latched
  re-arrival) (`benchmark.recovery_metrics`; arming the timeline profile is RFC
  0004 / the harness, the metric extraction is here).
- [x] Add coarse linear-ramp curves (latency-vs-load) for trend
  (`benchmark.linear_ramp`).
- [ ] Wire the nightly benchmark run as a **monitor** (alerts on budget regression,
  never blocks); the harness wires the actual runner. *(FZI-private — see below.)*
- [x] Cover the driver oracle, budget compare, and recovery metrics with tests
  (`tests/unit/test_benchmark.py`).

## Validation checklist

How each capability will be proven once built (forward-looking). The drivers and
verdict logic are pure and host-testable; the runs themselves are **nightly
monitors, not gates** (the determinism rule), so their *output* is trended/manually
reviewed rather than asserted in a blocking test.

- [x] **a/b size-pattern publisher** — host unit test on the pattern generation
  (e.g. `4×0 B + 1×70 KB` sequence); advertised-topic check in CI smoke.
  Automatable. *(`test_size_pattern_generation_matches_a_b_sequence`.)*
- [x] **Capacity binary-search driver + oracle** (`loss < p` **and** `latency < L`
  over a window → breakpoint per profile) — host unit test driving the search
  against a stubbed metric source so the breakpoint is deterministic. Automatable.
  *(`test_capacity_binary_search_finds_the_breakpoint`, `test_oracle_*`.)*
- [x] **Sweep bounds + shared-link guard** (configured max size/rate/bandwidth; an
  unshaped run never saturates the LAN) — host unit test on the bound logic.
  Automatable. *(`test_shared_link_guard_*`,
  `test_find_capacity_never_searches_past_the_shared_link_budget`.)*
- [x] **Budget store + regression compare** (per `(SHA, profile, genre)`, ±
  tolerance) — host unit test on the compare against a recorded baseline fixture.
  Automatable. *(`test_budget_compare_*`, `test_budget_store_roundtrip_*`.)*
- [x] **Benchmark RMW selection** (Cyclone default, explicit per-run override) —
  host unit test for parser defaults and artifact-backed session override; the
  Docker-backed capacity E2E pins `--rmw cyclone`. Automatable.
  *(`test_benchmark_subcommand_arg_parsing`,
  `test_benchmark_session_copy_pins_requested_rmw`,
  `test_benchmark_capacity_*`.)*
- [x] **Cyclone DDS SPDP benchmark awareness** (default 30s retained, optional
  override, fixed-probe diagnostic warning for tight links) — host unit tests for
  XML rendering, session copy, generated plugin parameters, and diagnostic
  classification. Automatable. *(`test_ota_xml_renders_default_and_overridden_spdp_interval`,
  `test_benchmark_session_copy_applies_cyclone_spdp_override`,
  `test_run_session_passes_cyclone_spdp_interval_to_plugin`,
  `test_probe_spdp_diagnostics_warn_on_tight_cyclone_profile`.)*
- [x] **Self-contained benchmark result artifact** (`result.json` with context,
  metrics, verdict, and artifact references) — host unit tests assert the capacity,
  ramp, and sweep result files and the CI-readable metric output. Automatable.
  *(`test_capacity_driver_finds_breakpoint_with_stubbed_probe`,
  `test_ramp_driver_builds_curve_with_stubbed_probe`,
  `test_sweep_driver_runs_grid_with_stubbed_probe`.)*
- [x] **OTA target selection** (default to benchmark genre session, explicit
  target override for sessions/scenarios) — host unit test for default and
  override resolution. Automatable. *(`test_benchmark_ota_target_defaults_to_benchmark_session`.)*
- [x] **Simple OTA benchmark command** (`rosotacom ota-benchmark ... --peer
  a=... --peer b=...`) — host unit test for parser defaults and peer bindings.
  Automatable. *(`test_benchmark_subcommand_arg_parsing`.)*
- [x] **Interactive operator view** (tmux high-level run window, fullscreen
  per-peer catmux attach windows, network status plus command-log panes, one-shot
  final result window) — host unit test for the dry-run command plan, peer catmux
  attach script, and parser flags; live tmux attachment remains an operator
  workflow. Automatable for command planning, manual for actual attachment.
  *(`test_interactive_benchmark_dry_run_prints_operator_view`,
  `test_peer_catmux_attach_script_waits_for_container_and_tmux`,
  `test_benchmark_subcommand_arg_parsing`.)*
- [x] **OTA profile shaping privilege handling** (passwordless non-interactive
  sudo or local askpass prompt for `tc`/`ip`) — host unit tests assert the
  preflight checks, `sudo -n` wrapping, `sudo -S` stdin handling, and per-peer
  prompt flow. Automatable.
  *(`test_ota_preflight_can_require_network_shaping_sudo`,
  `test_ota_preflight_askpass_authenticates_via_stdin`,
  `test_ota_profile_shaping_uses_noninteractive_sudo`,
  `test_ota_profile_shaping_askpass_uses_stdin`,
  `test_ota_askpass_prompts_once_per_peer`.)*
- [x] **Recovery driver + metric set** (`t_recover`, `t_steady`, backlog/burst,
  lost-during-outage, latched re-arrival) — host unit test extracting the metrics
  from a synthetic timeline of transit records (RFC 0003); the **live recovery run
  is a nightly monitor** + operator review. *(`test_recovery_metrics_*`.)*
- [x] **Coarse linear-ramp curves** — curve builder host-tested
  (`test_linear_ramp_builds_the_response_curve`); the trend output stays
  **monitor-only**: nightly, reviewed, not gated.
- [ ] **Nightly benchmark run wired as a monitor** (alerts on budget regression,
  never blocks) — **operator/harness check**: the public repo defines the genre;
  the actual runner topology is FZI-private, so the wiring is confirmed manually in
  the harness, not by a public CI assertion.

## Open questions

- **Scalar vs frontier reporting.** A few canonical (size, rate) slices, or the
  full frontier? Start with slices.
- **Budget tolerance.** Absolute vs relative band; how many baseline long-runs
  establish it (the long-duration finding implies several).
- **a/b-pattern publisher home.** Extend `sized_publisher` vs a new node.
- **Gate-relevant recovery metrics.** Which of `{t_recover, backlog,
  lost-during-outage, latched-rearrival}` are budgeted vs informational.
