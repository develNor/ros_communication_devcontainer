# Testing

> **Note:** an expectation-driven, OTA-first rework of this model is proposed in
> `docs/rfcs/0001-expectation-driven-test-suite.md` (per-topic `expect` contracts,
> `rosotacom test` reading the session self-report, fewer markers). This document
> describes the model in effect today.

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
| OTA e2e | *external* | yes | two hosts over a real link | operator-started external gate |

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

The single-machine smoke test still *runs* the isolation assertion (publishing a
local-only topic and confirming it does not cross) — there the distinct domain
IDs make it hold trivially, but it exercises the same assertion code that proves
the real guarantee multi-machine, so the two tiers cannot diverge.

## Per-session capability marker

Every session under `examples/sessions/<name>/` is meaningful in some tiers and
not others. Each `session-definition.yaml` declares this with a `test_tiers`
block (the **single source of truth** — both tiers derive their session list
from it, so coverage cannot silently drift):

```yaml
test_tiers:
  single_machine: ok       # ok | na
  multi_machine: required  # ok | required | na
```

| Marker | Meaning |
|---|---|
| `single_machine: ok` | runs and is asserted in the single-machine smoke matrix |
| `single_machine: na` | not meaningful on one host (skip in smoke) |
| `multi_machine: ok` | valid across two hosts |
| `multi_machine: required` | the behavior can *only* be proven across two hosts |
| `multi_machine: na` | not exercised by the multi-machine suite |

The single-machine smoke matrix (`tests/e2e/test_smoke.py`) is derived from the
sessions marked `single_machine: ok`; the multi-machine set is derived from
`multi_machine in {ok, required}`. `rosotacom.cli.session_test_markers()` /
`sessions_in_tier()` read the markers, and
`tests/contract/test_security_maintenance_config.py` guards both directions so a
new or changed session must carry a valid marker. **To change a session's tier
coverage, edit its `test_tiers` marker** — the matrices follow automatically.

## OTA tier (external)

Multi-machine tests need a real two-host link, which public CI cannot provide,
so a repository operator may run them on an **external runner** that supplies
the hosts. This is a deliberate promotion gate, not a per-commit trigger: an
operator explicitly selects the candidate, starts the suite, reviews the
evidence, and separately confirms any branch promotion. The runner is
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

The set of sessions an OTA runner exercises is the sessions marked
`multi_machine: ok` or `multi_machine: required`. Keeping the markers in this
repo lets any external runner derive its scenario list without hardcoding it.

Both assertions in step 4 are the **same code** the single-machine smoke test
runs. After starting each peer, the runner calls, per host over SSH:

- `rosotacom verify --identity <peer>` — delivery of the crossed topics within
  the shared rate/latency bounds;
- `rosotacom probe-publish --identity a` then `rosotacom probe-check --identity b
  --expect absent` — isolation (a local-only topic must not cross).

These verbs probe inside the running session container, so the host itself needs
no ROS environment. The bounds and the probe topic live in `rosotacom` (single
source), so both tiers assert identically — only the transport (one loopback host
vs. two hosts over the wire) differs.
