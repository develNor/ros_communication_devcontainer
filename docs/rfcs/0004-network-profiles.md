# RFC 0004 — Network profiles & the fidelity ladder

**Status:** Implemented (pure half) — the schema, `tc`/`netem` command generation,
fail-safe arm/teardown controller, timeline expansion, both outage kinds,
`--profile` selection and the `expect.per_profile` invariant/conditional split are
in `rosotacom.network_profiles` / `network_shaper` / `status_eval`, host-tested.
The link-trace converter lives in `rosotacom.trace_profiles` and emits the same
profiles-file schema. The
**privileged per-direction live arming** (wiring the controller into the ota-smoke
SSH path) and the **manual bench checks** (crash teardown, post-shaping link bytes,
real two-shaped-interface application) remain. · **Scope:** the
environment / fidelity axis (the condition the transport runs *in*) · extends
[RFC 0002](0002-expectation-concepts.md) (expectations), consumes
[RFC 0003](0003-metric-backbone.md) (the measured metrics it asserts on), feeds
RFC 0005 (benchmark genres & CI)

## Summary

An `expect` contract (RFC 0001/0002) is only meaningful **relative to a declared
network condition**: "≤ 200 ms latency" is a pass on LAN and a nonsense floor on
cellular. Today a session has one implicit, undeclared environment — whatever link
the run happened to use — so its performance expectations are either too strict
(fail on a worse link) or too loose (never catch a regression).

This RFC makes the environment a **first-class, named, reproducible axis**:

- a **fidelity ladder** that names the rungs from loopback to real cellular;
- **network profiles** — declarative `tc`/`netem` conditions (static or a
  time-varying timeline, per-direction) applied *below* rosotacom on the bench;
- a split of every `expect` into **invariant** (correctness — holds under every
  profile) and **conditional** (performance — asserted *per profile*);
- **per-profile calibration**, so the live cellular contract is the calibrated
  cellular profile widened by a margin, not a guess.

It formalizes the manual `tc`/`netem` work the operator already does (the static
and timeline profiles recorded in the private network baseline) into a declared,
toggleable, gate-able capability.

## Motivation

- **Expectations depend on the link.** RFC 0002's performance axes (`hz`,
  `latency_ms`, `loss_pct`, `completeness`) are link-conditional; without a
  declared condition they cannot be set honestly. This is the unanswered question
  from the project's origin: *how do I derive live expectations?*
- **Real cellular is not reproducible.** The baseline shows the same 100 s test
  swinging 0 %→21 % loss; only an *emulated* condition gives comparable,
  regression-grade results. Emulation is the only rung that can gate.
- **The shaping already exists, ad hoc.** The operator applies `tc qdisc … tbf …
  netem delay … loss …` by hand and tears it down by hand. A forgotten teardown
  silently corrupts every later result on that machine. This must be declared and
  fail-safe, not manual.

## The fidelity ladder

The organizing structure for the whole environment axis (realism ↑,
reproducibility ↓):

| Rung | Condition | Reproducible? | Role |
|---|---|---|---|
| 0 | loopback (one host, distinct domains) | total | correctness, fast |
| 1 | replay/live over LAN | high | correctness + first performance |
| 2 | **replay/live + emulated profile** (this RFC) | high | reproducible "cellular"; **the rung that gates** |
| 3 | real WLAN | medium | is the wireless path itself healthy? |
| 4 | real cellular (SIM / hotspot) | low | **truth** — calibrates the emulation |

A **profile** is the knob within rung 2. Rungs 0–1 run with no profile (or the
`none` profile); rungs 3–4 run with no emulation — the "profile" is whatever the
real link is, and is *measured*, never *imposed*.

## Profile model

A profile is a declarative environment spec, defined once at project scope and
selected per run. Two kinds, both **per-direction** (uplink ≠ downlink — the
defining feature of cellular, and the dominant direction is the vehicle→center
uplink that carries telemetry):

```yaml
# profiles.yaml (project-scoped, referenced from rosotacom.yaml)
profiles:

  cellular-typical:                      # static
    uplink:   { rate: 4mbit,  delay: 120ms, jitter: 30ms, distribution: normal,
                loss: 2%, loss_correlation: 25% }
    downlink: { rate: 25mbit, delay: 60ms,  jitter: 20ms, loss: 0.5% }

  cellular-handover:                     # timeline (for recovery / degradation)
    timeline:
      - { for: 30s, uplink: { rate: 4mbit,   delay: 60ms,  loss: 0.1% } }
      - { for: 15s, uplink: { rate: 1mbit,   delay: 180ms, loss: 3%   } }
      - { for: 5s,  outage: true }
      - { for: 40s, uplink: { rate: 2.5mbit, delay: 90ms,  loss: 1%   } }
```

- **static** — constant `{rate, delay, jitter, distribution, distribution_file,
  loss, loss_correlation, loss_gemodel, reorder, duplicate, seed}` per
  direction. Two loss models, mutually exclusive per direction:
  `loss` (+ optional `loss_correlation`) is netem's independent/correlated
  per-packet loss; `loss_gemodel: {p, r, loss_bad, loss_good}` is the
  Gilbert–Elliott state model (`tc netem loss gemodel`), the vocabulary a real
  cellular link needs — rare transitions into a short intense bad state
  (`p`/`r` per packet) with distinct loss probabilities inside (`loss_bad`)
  and outside (`loss_good`) of it. The 2026-08-17 CCNG field fit is the
  reference: `p 0.12%, r 2.9%, loss_bad 2.7%, loss_good 0.098%` reproduces
  113 ms bad states every ~2.8 s. `distribution` accepts the iproute2
  builtins (`normal|pareto|paretonormal`) or a custom table name whose
  `distribution_file` (a `.dist` built by `python -m rosotacom.netem_dist`
  from measured delay samples) the runner installs into the shaping netns as
  `/usr/lib/tc/<name>.dist` — because a field link's delay is
  lognormal-tailed and `normal` under-tails p99 by ~2x. `seed` maps to
  `tc netem seed SEED` when supported, making the generated random
  delay/loss draw replayable.
- **timeline** — an ordered list of `{ for: <dur>, <static-params> | outage }`
  segments. A recovery benchmark (RFC 0005) *is* a timeline profile; `outage` is
  the step whose recovery is measured.

The numbers above are **illustrative**; calibrated per-link profiles live in the
operator's private baseline (`docs/network-baseline.md` in the harness), not in
this public repo.

## Application & lifecycle

A profile is **environment, not transport** — it is applied *below* rosotacom on
the OTA interface, so the transport sees a realistic pipe and is unaware of it
(consistent with the transport/measurement separation in RFC 0003).

- **Where:** `netem` shapes *egress*, so each peer shapes **its own outgoing
  direction** — the vehicle peer's OTA-interface egress carries the `uplink`
  profile, the center peer's carries the `downlink`. This maps directly onto the
  per-direction model and reuses the SSH/staging path `ota-smoke` already has.
- **Target the data interface only.** Shaping must hit the pinned OTA interface,
  never the SSH/control interface — otherwise it breaks the orchestration that
  applies it.
- **Fail-safe teardown (hard requirement).** A profile must never outlive its run:
  arm with an automatic revert on stop, on error, *and* a safety max-duration, so
  a crashed run cannot leave a `qdisc` shaping every later result. `tc qdisc del
  dev <if> root` is idempotent and always runs on teardown.
- **Seamless timeline stepping (issue #124).** Timeline steps `tc qdisc replace`
  the *same* qdisc tree in place — never del+add, which drops everything queued
  inside `netem` and leaves a brief unshaped window at every boundary (observed
  as lost plus under-delayed messages, even between identical segments). The tree
  shape is fixed per timeline (tbf `1:` + netem `10:` when any segment
  rate-limits, else netem `10:` root); an unused stage is armed as a pass-through
  (effectively-unlimited tbf / bare netem) instead of being removed. `catchup`
  outages fold in as an in-place `netem loss 100%`; `reconnect` stays link-down
  (disruptive by design), restored by the following step.
- **Bench-only, privileged.** `tc`/`netem` needs `CAP_NET_ADMIN` and applies on
  the controllable bench. The real fleet is never shaped — there the link *is* the
  condition and is measured. Selecting a profile on a real-deployment run is an
  error.

Selection is per run: `--profile cellular-typical` on `ota-smoke` / `test` /
benchmark commands; a project may set `shared.profile` as a default; `--profile
none` (or omission) is the unshaped rung.

## Expectations bound to profiles

The payload that connects this RFC to RFC 0002. Every `expect` splits in two:

- **invariant** (correctness) — `presence`, `mode`, `existence`, isolation,
  content integrity. Asserted under **every** profile, including the harshest.
- **conditional** (performance) — `hz`, `latency_ms`, `loss_pct`, `completeness`.
  Asserted **per profile**; the bound under `cellular-typical` is laxer than LAN.

The conditional bounds carry per-profile overrides; the invariant part stays
top-level and profile-independent:

```yaml
expect:
  presence: required                 # invariant — every profile
  mode: stream                       # invariant
  hz: { min: 9 }                     # conditional — default (best case / LAN)
  latency_ms: { max: 200 }
  per_profile:
    cellular-typical:  { hz: { min: 6 }, latency_ms: { max: 600 },  loss_pct: { max: 5 } }
    cellular-handover: { latency_ms: { max: 2000 }, completeness: { min_ratio: 0.5 } }
```

`rosotacom test --profile P` asserts the invariant block plus `P`'s conditional
block (falling back to the default conditional where `P` has no override).
Unshaped runs use the default conditional. A correctness regression fails on every
rung; a performance regression fails on the rung where it matters.

## Per-profile calibration

This is the principled answer to *"how do I get sensible live expectations?"* Reuse
RFC 0002's `calibrate` / `--suggest` machinery, **per profile**:

1. Author the **invariant** expectations once (correctness).
2. For each profile, run a reference replay under it →
   `rosotacom calibrate --profile P --bag <ref>` emits `P`'s **conditional** band
   (a margin around the observed `hz`/`latency`/`loss`).
3. Gate: invariant under all profiles; each profile's conditional under its band.
4. **The live deployment contract = the calibrated `cellular-typical` band,
   widened by a margin** — derived, not guessed, and regenerated when the profile
   or the workload changes.

## Honest limits

- **Emulation ≠ truth.** `netem` is an idealized pipe (token bucket + i.i.d.
  delay/loss draws). Real cellular adds bufferbloat, radio ARQ, scheduler effects
  and time-correlated loss; the baseline's non-monotonic loss-vs-rate is exactly
  the fluctuation `netem` does not reproduce by default. Emulation buys
  **reproducibility, not truth** — rung 4 (real cellular) calibrates the
  *shape* of the emulated profile; the profile then gives repeatable gating.
- **Bursty loss needs the state model.** Default `netem loss` is independent; the
  2-arg `loss p% corr%` cannot express short-bad/long-good either. `loss_gemodel`
  carries the Gilbert–Elliott parameters directly (see the schema section); fit
  `p`/`r` from the measured bad/good run lengths (per packet) and the two loss
  probabilities from the loss rate inside/outside bad states.
- **Outage semantics are a choice.** `netem loss 100%` keeps the interface up (DDS
  endpoints survive, recovery is "catch up") vs link-down (forces RMW
  re-discovery, a harsher recovery). They test different recoveries — see Open
  questions.
- **Run-to-run variation.** `delay`/`loss` draws are RNG-driven; an unseeded
  profile bounds behaviour, it does not make runs byte-identical. Use `seed` for
  replayable netem draws when calibrating tight zero-loss boundaries. Local Docker
  benchmarks can copy a seed-capable host `tc` into benchmark containers when the
  container distro `tc` does not understand `netem seed`.
- **Asymmetry, not absolute one-way truth.** Per-direction *shaping* is exact, but
  reading per-direction *latency* back out still depends on the clock-offset
  handling in RFC 0003.

## Implementation checklist

Roughly in dependency order; the fail-safe teardown (second item) is the highest
risk — a stuck `qdisc` silently corrupts every later result on the machine.

- [x] Define the profile schema (static + per-direction `{rate, delay, jitter,
  distribution, loss, loss_correlation, reorder, duplicate}`) at project scope
  (`network_profiles.parse_profiles` / `load_profiles_file`; project-scoped
  `profiles.yaml` referenced from `rosotacom.yaml`), and resolve selection via
  `--profile <name>` / `shared.profile` / `none` (`resolve_profile_selection`,
  `cli._resolve_active_profile`).
- [x] Extend the loss/delay vocabulary to what field links actually need
  (issue #278): `loss_gemodel` (Gilbert–Elliott state loss, exclusive with
  `loss`) and custom delay-distribution tables (`distribution` beyond the
  builtins + `distribution_file`, generated from measured samples by
  `rosotacom.netem_dist`, installed into the shaping netns by the lab runner
  before arming).
- [x] Arm a named static profile on the OTA-interface egress with fail-safe
  teardown (revert on stop, on error, and via a safety max-duration watchdog);
  target the data interface only, never the SSH/control interface
  (`network_shaper.ProfileShaper`, `network_profiles.safety_teardown_command`). The
  controller logic + command generation are host-tested; arming it over the live
  ota-smoke SSH path is the remaining wiring.
- [ ] Apply profiles per direction — `uplink` on the sending peer's egress,
  `downlink` on the receiver's — and reject profile selection on a real-deployment
  run. *(Per-direction `expand_timeline` and the `allow_shaping` reject rule exist
  and are host-tested; the live wiring into ota-smoke is pending.)*
- [x] Add `expect.per_profile` overrides and the invariant/conditional split to
  `status_eval`, so `rosotacom test --profile P` asserts the invariant block plus
  `P`'s conditional band (default conditional where `P` has no override)
  (`status_eval.resolve_expect_for_profile`; threaded through `evaluate_report(s)`).
- [x] Add timeline profiles (ordered segments + `outage`) — the substrate for the
  recovery genre in RFC 0005 (`network_profiles.TimelineSegment` / `expand_timeline`;
  both `outage` kinds — see Open questions).
- [x] Make timeline stepping seamless (issue #124): steps `tc qdisc replace` one
  constant tree in place instead of del+add, so a boundary drops no queued packets
  and never runs unshaped (`expand_timeline`, `_tbf_stage_command` /
  `_netem_stage_command`; outages folded into the same tree).
- [x] Add per-profile calibration — realized as `rosotacom test --suggest --profile
  P`, which emits `P`'s conditional band nested under `per_profile`
  (`status_eval.suggest_profile_band`), reusing the RFC 0002 `--suggest` machinery.
- [x] Convert recorded `link_trace.jsonl` files into profile YAML via
  `rosotacom profile from-trace`: timeline replay with change-point segmentation
  and outage steps, plus static distillation from a full trace or window
  (`trace_profiles.convert_trace_to_profile_yaml`). Passive throughput is omitted
  as `rate` unless the sample is marked saturated/probed/capacity-like.
- [ ] Confirm the `/proc/net/dev` link sampler (RFC 0003 / `link_bytes.py`) reports
  post-shaping wire bytes so link-overhead stays meaningful under a profile. *(Bench
  check.)*
- [x] Cover schema parsing, command generation, the arm/teardown controller
  (including missing-qdisc/crash teardown and the control-interface guard),
  selection resolution, and `per_profile` evaluation with host tests
  (`test_network_profiles.py`, `test_network_shaper.py`, `test_status_eval.py`).
  Live per-direction application + crash teardown on real hardware stay a bench check.

## Validation checklist

How each capability will be proven once built (forward-looking — fill in and check
off during implementation). Notes whether automation is feasible; privileged
`tc`/`netem` arming is the part that resists pure host testing.

- [x] **Profile schema parsing** (static + per-direction fields) — host unit test
  on the parser (`test_network_profiles.py::test_parse_*`). Done.
- [x] **Selection resolution** (`--profile` / `shared.profile` / `none`, and the
  *reject a profile on a real-deployment run* rule) — host unit test
  (`test_resolve_profile_selection`). Done.
- [x] **`tc`/`netem` command generation** — host unit test asserting the argv built
  for a given profile/direction (rate/delay/jitter/loss/correlation → `tbf`+`netem`
  string), without touching a real interface (`test_shaping_commands_*`,
  `test_netem_arg_ordering_is_valid`). Done.
- [x] **Gilbert–Elliott loss (`loss_gemodel`)** — host unit tests on the schema
  (required/unknown keys, exclusivity against `loss`) and on the argv (all four
  probabilities always emitted): `test_gemodel_parse_and_exclusivity`,
  `test_gemodel_argv_emits_all_four_probabilities`. The *statistical* fidelity of
  the emitted process (bad-run length, in-state loss rates) stays a bench check
  against a field fit — the 2026-08-17 CCNG fragment replay is the reference.
- [x] **Custom delay-distribution tables** — host unit tests: a non-builtin
  `distribution` requires `distribution_file` and resolves relative to the
  profiles file (`test_custom_distribution_requires_a_file_and_builtin_does_not`,
  `test_load_profiles_file_resolves_distribution_files_and_lists_installs`);
  table generation is normalized, monotonic, tail-preserving
  (`test_netem_dist.py`). The lab runner installs required tables into the
  shaping netns before arming (`required_dist_installs` consumed in
  `cli_benchmark`); that a netem referencing the installed name actually loads
  it is part of the same bench check as arming itself.
- [~] **Fail-safe teardown** (revert on stop, on error, on safety max-duration; the
  idempotent `tc qdisc del … root` always runs; data-interface-only, never the
  SSH/control interface) — host unit test on the teardown/targeting logic done
  (`test_network_shaper.py`: revert on stop/error, missing-qdisc tolerance,
  control-interface refusal, watchdog command); **the manual bench check** that a
  killed run leaves no `qdisc` behind is still **required, not optional**.
- [~] **Per-direction application** (uplink on the sender's OTA egress, downlink on
  the receiver's) — host unit test on the peer→direction mapping done
  (`expand_timeline(direction=…)`); live application is the pending **operator bench
  check** (privileged, needs two shaped interfaces).
- [x] **Invariant/conditional split + `expect.per_profile` evaluation** — host unit
  test in `status_eval` (`test_status_eval.py`: invariant asserted under every
  profile; `P`'s conditional used under `--profile P`, default conditional where
  `P` has no override; invariant/unknown overrides rejected). Done.
- [x] **Timeline profiles** (ordered segments + `outage`) — host unit test on the
  schedule expansion (`test_expand_timeline_*`, both outage kinds); live stepping is
  a **bench check** (and the substrate for the RFC 0005 recovery genre).
- [x] **Seamless timeline stepping** (in-place `replace` of one constant tree — no
  queue drop, no unshaped window at step boundaries) — host unit tests on the argv
  (`test_expand_timeline_replaces_one_constant_tree_no_teardown`,
  `test_timeline_catchup_is_in_place_full_loss_interface_up`,
  `test_timeline_reconnect_downs_link_and_next_step_restores_it_first`) plus the
  Docker e2e test proving the kernel changes the tree in place — stats/queue
  persist across a boundary, the netem child survives a tbf-root replace, a bare
  netem step resets stale parameters (`tests/e2e/test_timeline_stepping.py`, in the
  fast-e2e lane). Bench confirmation: a probe run under a stepping profile shows no
  boundary loss/under-delay.
- [x] **Per-profile calibration** (`rosotacom test --suggest --profile P`) — host
  unit test that a reference status fixture yields `P`'s conditional band nested
  under `per_profile` (`test_suggest_profile_band_*`), reusing the RFC 0002
  `--suggest` machinery. Done.
- [x] **Trace-to-profile conversion** (`rosotacom profile from-trace`) — host unit
  tests on synthetic traces recover known timeline segments, detect sample-gap
  reconnect outages, compute static percentiles exactly, omit passive-only rates,
  load generated YAML through `load_profiles_file`, and feed the resulting
  timeline into dry-run `ProfileShaper` command generation
  (`tests/unit/test_trace_profiles.py`). Done.
- [ ] **Emulated-profile gate (rung 2)** — an example session run under one
  canonical profile in the per-promotion CI smoke, asserting a conditional bound
  that differs from the unshaped run. Automatable in the smoke matrix.
- [ ] **Link sampler reads post-shaping wire bytes** — **manual bench check** under
  an armed `tbf`/`netem` interface that `/proc/net/dev` byte counts reflect shaped
  bytes (can't be meaningfully emulated in a host test).
- [ ] **Emulation-vs-truth calibration (rungs 3–4)** — **operator manual /
  monitor-only**; real WLAN/cellular characterization calibrates the profile shape
  and never gates (non-deterministic by design).

## Open questions

- **Config home.** *Resolved:* project-scoped `profiles.yaml` referenced from
  `rosotacom.yaml` (`profiles:` key) — a profile is environment, reused across many
  sessions, not part of one session's contract.
- **Outage = `loss 100%` vs link-down.** *Resolved (2026-06):* **both, as two named
  kinds.** `outage: catchup` is `loss 100%` with the interface up (DDS endpoints
  survive → recovery is "catch up"); `outage: reconnect` is link-down (forces RMW
  re-discovery → the harsher reconnect with the multi-second backlog/simultaneous
  arrival the baseline describes). A bare `outage: true` defaults to the milder
  `catchup`. Implemented in `network_profiles.expand_timeline` / `OUTAGE_KINDS`.
- **Where the metric backbone reads the link during emulation.** The same
  `/proc/net/dev` link sampler (RFC 0003 / `link_bytes.py`) measures the shaped
  interface — confirm `tbf`/`netem` byte counts reflect post-shaping wire bytes so
  link-overhead stays meaningful under a profile.
- **Calibration drift.** When does a per-profile band need re-calibrating — on
  workload change only, or on a schedule? (Ties into RFC 0005 nightly.)
