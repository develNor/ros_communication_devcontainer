# RFC 0004 — Network profiles & the fidelity ladder

**Status:** Draft — design agreed, not yet implemented · **Scope:** the
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

- **static** — constant `{rate, delay, jitter, distribution, loss,
  loss_correlation, reorder, duplicate}` per direction. `loss_correlation` (netem
  state/`gemodel`) makes loss bursty rather than independent — closer to real
  radio loss.
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
- **Bursty loss needs the state model.** Default `netem loss` is independent; use
  `loss_correlation` / `gemodel` to approximate real burst loss.
- **Outage semantics are a choice.** `netem loss 100%` keeps the interface up (DDS
  endpoints survive, recovery is "catch up") vs link-down (forces RMW
  re-discovery, a harsher recovery). They test different recoveries — see Open
  questions.
- **Run-to-run variation.** `delay`/`loss` draws are RNG-driven; a profile bounds
  behaviour, it does not make runs byte-identical. That is acceptable (and the
  conditional bands are calibrated with margin); seed only if exact replay is ever
  needed.
- **Asymmetry, not absolute one-way truth.** Per-direction *shaping* is exact, but
  reading per-direction *latency* back out still depends on the clock-offset
  handling in RFC 0003.

## Implementation checklist

Roughly in dependency order; the fail-safe teardown (second item) is the highest
risk — a stuck `qdisc` silently corrupts every later result on the machine.

- [ ] Define the profile schema (static + per-direction `{rate, delay, jitter,
  distribution, loss, loss_correlation, reorder, duplicate}`) at project scope, and
  resolve selection via `--profile <name>` / `shared.profile` / `none`.
- [ ] Arm a named static profile on the OTA-interface egress with fail-safe
  teardown (revert on stop, on error, and via a safety max-duration); target the
  data interface only, never the SSH/control interface.
- [ ] Apply profiles per direction — `uplink` on the sending peer's egress,
  `downlink` on the receiver's — and reject profile selection on a real-deployment
  run.
- [ ] Add `expect.per_profile` overrides and the invariant/conditional split to
  `status_eval`, so `rosotacom test --profile P` asserts the invariant block plus
  `P`'s conditional band (default conditional where `P` has no override).
- [ ] Add timeline profiles (ordered segments + `outage`) — the substrate for the
  recovery genre in RFC 0005.
- [ ] Add `rosotacom calibrate --profile P` to emit per-profile conditional bands
  from a reference replay.
- [ ] Confirm the `/proc/net/dev` link sampler (RFC 0003 / `link_bytes.py`) reports
  post-shaping wire bytes so link-overhead stays meaningful under a profile.
- [ ] Cover schema parsing, arm/teardown (including crash teardown), per-direction
  application, and `per_profile` evaluation with tests.

## Open questions

- **Config home.** Project-scoped `profiles.yaml` referenced from `rosotacom.yaml`
  (reusable across sessions) vs inline `shared.profiles`. Leaning project-scoped —
  a profile is environment, reused across many sessions, not part of one session's
  contract.
- **Outage = `loss 100%` vs link-down.** Which models the real reconnect the
  baseline describes (multi-second backlog, simultaneous arrival)? Possibly both,
  as two named outage kinds.
- **Where the metric backbone reads the link during emulation.** The same
  `/proc/net/dev` link sampler (RFC 0003 / `link_bytes.py`) measures the shaped
  interface — confirm `tbf`/`netem` byte counts reflect post-shaping wire bytes so
  link-overhead stays meaningful under a profile.
- **Calibration drift.** When does a per-profile band need re-calibrating — on
  workload change only, or on a schedule? (Ties into RFC 0005 nightly.)
