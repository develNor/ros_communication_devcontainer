# RFC 0006 — Dynamic QoS (mirror correctness, shape bandwidth)

**Status:** Draft — design agreed, not yet implemented · **Scope:** QoS resolution
across the OTA hop · extends [RFC 0002](0002-expectation-concepts.md) (the
`remote_assist` workload it simplifies); shares the correctness/performance split
with [RFC 0004](0004-network-profiles.md)

## Summary

Today every topic's QoS across the OTA hop is hand-written. The FZI-private
`remote_assist` session repeats, for **each** latched topic, the same block —
`durability: transient_local` + `for_role.ota_pub/ota_sub` + a long `lifespan` —
because a held value must survive the hop. This is boilerplate, and getting it
wrong is a whole bug class (RFC 0002: `domain_bridge` auto-detecting a topic's QoS
and racing it to `volatile`, silently dropping every latched value).

This RFC adds **`qos: dynamic`** (the default): rosotacom introspects the *native*
publisher's QoS — from the bag's `offered_qos` on replay, from the live endpoint
otherwise — and derives the OTA QoS automatically, on one principle:

- **mirror the correctness-bearing QoS** (durability / latched-ness) across every
  stage, including the hop;
- **keep the bandwidth-bearing QoS a policy** (reliability / depth / lifespan on
  streams), because re-shaping it to fit the link is the OTA hop's whole job.

Explicit per-topic / per-role `qos:` still wins (the existing `qos.py` merge).

## Motivation

- **Boilerplate + a bug class.** The repetitive `transient_local` blocks are
  mechanical, and the one time they are wrong the held value vanishes with no error
  (RFC 0002's `domain_bridge` durability race). Deriving them removes both.
- **The information already exists.** `bag_ground_truth.py` already parses each bag
  topic's `offered_qos` (durability, reliability); the live graph exposes the same
  via `get_publishers_info_by_topic`. Nothing new is measured — only consumed.

## The principle: QoS has the same correctness/performance split

The split that runs through the whole system (motivation.md; RFC 0004's
invariant/conditional expectations) applies to QoS too:

| QoS field | Kind | Dynamic behaviour |
|---|---|---|
| `durability` (latched-ness) | **correctness** | **mirror** native across all stages + hold it across the hop (transient_local + long `lifespan` on the OTA pub) + set `expect.mode: latched` |
| `reliability` | **load-dependent** (see below) | mirror when the topic fits the budget; force `best_effort` when it must be decimated |
| `depth` / `history` | **performance** | policy: `keep_last` / `depth 1` for streams unless overridden |
| `lifespan` (on streams) | **performance** | policy: short, to bound the reconnect backlog (RFC 0003/0005) |

`dynamic` is therefore **not transparent pass-through** — the OTA hop deliberately
*changes* the performance QoS (a 100 Hz reliable stream becomes best_effort /
depth-1 to fit the pipe) while *preserving* the correctness QoS (a latched value
stays latched end-to-end).

## Resolution

1. **Read native QoS.** Replay: `offered_qos` from the bag metadata
   (`bag_ground_truth.py`). Live: the publisher endpoint(s) via
   `get_publishers_info_by_topic`.
2. **Classify latched-ness.** `durability == transient_local` (corroborated on
   replay by a low message count ⇒ publish-on-change) ⇒ latched.
3. **Derive:**
   - latched ⇒ `transient_local` on the local relay pubs, the OTA pub/sub, and the
     receiver; long `lifespan` on the OTA pub so a late subscriber still gets the
     held value; `expect.mode: latched`.
   - non-latched ⇒ the default streaming policy (best_effort / keep_last / depth 1
     / short lifespan).
4. **Reliability by load (the subtle part).** A low-rate reliable topic (e.g. a
   command) is a *correctness* concern — a dropped command is bad — and fits the
   budget, so mirror `reliable`. A high-rate reliable stream is a *bandwidth*
   concern, so force `best_effort` regardless of native (the decimation is
   intended). The discriminator is the offered load (rate × size) vs the per-topic
   budget.
5. **Explicit overrides win**, per field and per role, via the existing merge.

## Implementation checklist

- [ ] Read native QoS from the bag `offered_qos` (replay) and from live publisher
  endpoints (`get_publishers_info_by_topic`).
- [ ] Add `qos: dynamic` and make it the default when a topic gives no explicit
  `qos:`.
- [ ] Mirror correctness QoS: propagate `durability` across all stages incl. the
  OTA hop, hold latched values (transient_local + long OTA-pub `lifespan`), and set
  `expect.mode: latched` automatically.
- [ ] Apply the default streaming policy (best_effort / keep_last / depth 1 / short
  lifespan) to non-latched topics; never inherit native depth / large history for
  streams.
- [ ] Decide `reliability` by offered load: mirror `reliable` for low-rate topics
  within budget, force `best_effort` above it.
- [ ] Keep explicit per-topic / per-role `qos:` overriding dynamic resolution, per
  field (the existing `qos.py` merge order).
- [ ] Handle the live discovery race (resolve lazily when the publisher appears)
  and multi-publisher QoS conflict (defined precedence — most restrictive).
- [ ] Migrate `remote_assist`: drop the repetitive per-latched-topic
  `transient_local` / `for_role` / `lifespan` blocks now derived by `dynamic`, and
  confirm delivery is unchanged.
- [ ] Tests: latched mirroring + hold-across-hop, stream policy, load-based
  reliability, override precedence, replay-vs-live source, conflict resolution.

## Honest limits

- **Live discovery race.** Live resolution needs the native publisher to exist when
  rosotacom resolves QoS → resolve lazily (the wrapper already subscribes
  dynamically), not eagerly at generate-time. Replay is clean (the bag metadata is
  known upfront).
- **Latched detection is a heuristic.** `durability` alone can mis-read a high-rate
  `transient_local` topic as latched; replay corroborates with the count, live must
  infer from the observed rate.
- **Multi-publisher conflict.** Several publishers offering different QoS need a
  rule (most-restrictive); `bag_ground_truth` currently takes the first profile.
- **The non-obvious core.** "Dynamic" mirrors correctness QoS and *re-shapes*
  performance QoS — it is **not** transparent inheritance. Mirroring native
  reliability / depth onto a high-rate stream would blow the OTA budget, the
  opposite of the intent.

## Open questions

- **Latched discriminator:** `durability` alone, or `durability` + low-rate /
  low-count? (Replay has the count; live must infer.)
- **Reliability-by-load threshold:** a simple rate/size threshold first, or the
  full per-topic-budget comparison (ties into RFC 0004/0005 budgets)?
- **Resolution time:** lazy run-time for live vs generate-time for replay — one path
  or two?
- **Override granularity:** per-field merge (as `qos.py` already does) vs
  all-or-nothing when an explicit `qos:` is present.
