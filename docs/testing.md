# Testing

This project separates two things that are easy to conflate:

- **Examples** exist to *teach a user* how to run a session. They live in
  `src/rosotacom/resources/examples/` and are copied out with
  `rosotacom examples create`. They are documentation-by-doing, not tests.
- **Tests** exist to *verify the system*. They are organized into tiers, and
  each session declares which tiers are meaningful for it.

## Test tiers

| Tier | Location | Docker | Network | Runs in |
|---|---|---|---|---|
| host / unit | `tests/unit` | no | none | draft + ready PR |
| contract | `tests/contract` | no | none | ready PR (merge gate) |
| single-machine e2e (smoke) | `tests/e2e` | yes | loopback | merge gate + nightly |
| multi-machine e2e | *external* | yes | two hosts over a real link | external runner |

`just lint typecheck test-nondocker-cov docs package` plus `just test-e2e-smoke`
is the full pre-merge suite; `just check` runs everything except the Docker
smoke. See [ci.md](ci.md) for which CI job runs which tier.

## Single-machine vs multi-machine: why they differ

On a single host there is no real interface separation — every container shares
the same loopback. Single-machine tests therefore get their isolation from
**distinct `ROS_DOMAIN_ID`s**: that is a *test-only* arrangement that keeps the
two peers from hearing each other by accident.

The core OTA guarantee — *only the explicitly bridged topics reach the peer; your
local application topics never leak across the link* — cannot be proven that way,
because the domain split is doing the isolation for you. It can only be proven
**across two machines on a shared domain**, where the bridge configuration is the
only thing standing between a local topic and the wire. Some transport setups
(e.g. single-domain CycloneDDS topic hiding, or a split-domain `domain_bridge`)
are therefore **only meaningful multi-machine**.

## Per-session capability marker

Every session under `examples/sessions/<name>/` is meaningful in some tiers and
not others. Declare that with a marker so coverage cannot silently drift:

| Marker | Meaning |
|---|---|
| `single_machine: ok` | runs and is asserted in the single-machine smoke matrix |
| `single_machine: na` | not meaningful on one host (skip in smoke) |
| `multi_machine: ok` | valid across two hosts |
| `multi_machine: required` | the behavior can *only* be proven across two hosts |

Today the single-machine smoke set is the heartbeat-RMW matrix in
`tests/e2e/test_smoke.py`, guarded by
`tests/contract/test_security_maintenance_config.py` so the list cannot drift
unnoticed. When a session's tier coverage changes, update that matrix (and its
multi-machine counterpart, below) in the same change.

## Multi-machine tier (external)

Multi-machine tests need a real two-host link, which public CI cannot provide,
so they run on an **external runner** that supplies the hosts. That runner is
parameterized purely by **host addresses / SSH targets** — it does not live in
this repository, and this repository carries no host names, addresses, or
network details.

A multi-machine run, generically:

1. brings both hosts to the same commit and installs the checkout-local CLI,
2. wires the example `data_dict.json` to each host's reachable address (instead
   of loopback),
3. starts identity `a` on host A and identity `b` on host B,
4. asserts the bridged topics arrive within rate/latency bounds **and** that a
   local-only probe topic published on A never appears on B (isolation),
5. collects each host's `session-instances/` logs.

The set of sessions a multi-machine runner exercises is the sessions marked
`multi_machine: ok` or `multi_machine: required`. Keeping the markers in this
repo lets any external runner derive its scenario list without hardcoding it.
