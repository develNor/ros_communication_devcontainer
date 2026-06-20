# RFC 0002 — Richer expectation concepts (presence / mode / completeness / overhead)

**Status:** Partially implemented · **Scope:** test/example architecture · extends
[RFC 0001](0001-expectation-driven-test-suite.md)

## Summary

RFC 0001 gave every crossed topic a per-topic `expect` contract asserted by both
the live status overview and `rosotacom test`, with two axes: **hz** and
**latency_ms**. Real applications need more. This RFC adds:

- `presence: required | optional` — optional topics that do not deliver are not
  failures.
- `mode: stream | latched | existence` — not every topic is a steady stream; some
  are latched/static (assert the *held value arrived*, not a rate), some only
  need to *exist* in the graph.
- (designed, not yet implemented) `min_count` / completeness — assert that *all*
  (or N) of a finite source's messages arrived.
- (designed, not yet implemented) **link-overhead** assertion — assert the OTA
  link carries ~the ROS payload and not substantial overhead (retransmits, shadow
  connections, bad RMW/QoS). **The measurement already exists** — see below; the
  remaining work is only to surface both numbers and assert their ratio.

The motivating workload is the FZI-private `remote_assist` session — a real,
feature-rich remote-driving contract that predates the current rosotacom and is
far more advanced than the packaged examples. It stays private (real bag + config),
but **every feature it uses should become a small, public, curated example with
its own test**, so the framework is exercised feature-by-feature and the private
`remote_assist` ota-smoke turns green on top of it.

## The remote_assist feature inventory (what must be testable)

Captured from the production contract and its motivation:

- **Latched / transient_local delivery.** Natively-latched topics (e.g.
  `/tf_static`) and topics the preprocessing *makes* latched. A peer (the center)
  whose com-container starts *after* an old latched publisher must still receive
  the held value.
- **Bandwidth shaping** — because vehicles publish without caring about the link:
  - **drop** N of M native messages (`drop: {drop_count, window_size}`).
  - **throttle** to a max rate (`throttle_hz`).
  - **latch-to-save-bandwidth**: convert a ~1–10 Hz *non-latched* static stream
    into publish-on-change (`latch: true`). Big OTA saving.
- **restamp** — on bag playback the recorded (old) header stamps are rewritten to
  "now" so downstream/visualization and latency measurement are meaningful.
- **trickle** — the *receiving* side (center) synthetically re-publishes static /
  latched values at a steady low rate (`trickle_hz`) *for the local native app
  only*, because Foxglove/Lichtblick timeline plots render rarely-changing values
  poorly. Visual nicety, not an OTA concern.
- **Video / image transport** (ffmpeg), **framebridge** (global↔local frame
  remap with a per-vehicle prefix), **target-prefix** addressing. Advanced; may be
  hard to assert beyond existence/throughput — see TODO.

## Gap analysis: two axes are not enough

`hz` + `latency_ms` only fit **regular, headered, restamped** streams:

- **Headerless types** (`std_msgs/Float32|Float64|String`, `MarkerArray`,
  `TFMessage`) have no `header.stamp`, so the monitor cannot measure latency
  (it reports `None`) — a `latency_ms` expect always fails. (topic_monitor.py:297)
- **Un-restamped** replayed topics carry months-old stamps → the monitor labels
  them "Clocks unsynced" (>1000 s) → latency `None`.
- **Latched / static topics publish on change** and then idle. The status rollup
  only marks a topic `OK` when its *final stage is currently FLOWING*
  (status_overview_core.py `rollup`), so a correctly-delivered-and-held latched
  topic is reported `STALLED` ("stopped Xs ago"). hz is meaningless for them.
- **Irregular / sparse topics**: neither a rate nor a single held value is the
  right assertion — sometimes you only want *existence*, or *did all the bag's
  messages arrive*.
- **Optional topics**: a bidirectional contract may declare topics that a given
  test does not exercise (e.g. center→vehicle commands during a vehicle-telemetry
  replay). Their absence must not be a failure.

## New concepts

Per-topic `expect` (all optional, back-compatible — absent ⇒ today's behaviour):

```yaml
expect:
  presence: required | optional      # default required
  mode: stream | latched | existence # default stream
  hz: { min, max }                   # stream only
  latency_ms: { max }                # stream only
  # min_count: N                     # DESIGNED, not yet implemented (see TODO)
```

- **stream** (default): deliver end-to-end, currently FLOWING; assert hz / latency
  / quality. Today's behaviour.
- **latched**: assert the value *was delivered at least once* (final stage
  FLOWING or STALE) — the held value arrived; rate is not asserted.
- **existence**: assert only that the topic is present in the graph (a publisher
  exists). For irregular topics where neither rate nor a held value is meaningful.
- **presence: optional**: a topic that does not reach OK (and is not a satisfied
  latched/existence case) is *not* a failure.

Implemented in `status_eval.py` (`evaluate_report`, pure + unit-tested in
`tests/unit/test_status_eval.py`). `expect` already accepts arbitrary keys
(generate_session_files validates it only as a mapping), so no schema change was
needed. NOTE: this changes only the **test verdict** (`rosotacom test`). The live
status overview (`status_overview_core.py`, in-container) is also being made
mode-aware so the live display agrees with the verdict (latched/existence show OK).

## Live vs replay (rosbag) tests

These are two fundamentally different test situations, separated by **how much of
the future is known**:

| | **Live** | **Replay (rosbag)** |
|---|---|---|
| Source | real running system | a recorded, finite, deterministic bag |
| Future | unknown | known exactly (every message, count, size, time) |
| Core question | "is it within bounds *right now*?" | "did the *known* input arrive intact, and is the link healthy?" |
| Assertions | hz / latency / presence bounds (monitoring) | all of live **plus** completeness, loss%, content, contract-vs-ground-truth |
| A failure means | the system degraded | a transmission problem **or** a wrong expectation **or** a config bug |

The current `expect` model is a *live* model: it watches an open-ended stream and
warns on out-of-bounds. That model is shared by both situations — but a replay
test *knows the ground truth*, which unlocks assertions a live test fundamentally
cannot make:

- **Completeness / loss.** Only a known finite source lets you assert "all N
  messages of topic X arrived" or "loss ≤ p%". Live can only infer from rate dips.
- **Contract self-validation (calibration).** A replay run knows each topic's
  actual delivered hz / size / latency, so it can *report* them to author or
  verify the `expect` block — catching wrong expectations (exactly our `/tf`
  `hz>=40` vs an 18 Hz reality). A reference replay can even *emit* a suggested
  `expect`.
- **Content integrity.** Received messages can be compared to the sent ground
  truth (byte-equal, or equal after a declared transform). Live has no oracle.
- **True link latency vs payload-stamp latency.** Live latency is
  `now − header.stamp` and needs clock sync. In replay the bag's stamps are
  historical and `restamp` rewrites them to "now", so payload-stamp latency only
  measures the *local* pipeline, not the link. Measuring true OTA latency in
  replay needs a send-time injected at the relay (a sidecar stamp), independent
  of the payload header.
- **Determinism.** Replay is reproducible, so flakiness isolates a transmission /
  RMW / QoS problem from source variability — the right shape for a CI gate.

**Does the distinction need to surface in the framework?** Partly. The per-topic
`expect` (mode/presence/hz/latency) stays shared. Replay adds a *layer* of
ground-truth assertions (completeness/loss/content/calibration) and a different
latency source. The framework can detect replay mode from the presence of a bag
source (a scenario application that replays a bag) or an explicit session flag,
and only then enable the replay-only assertions. See the TODOs below.

## Feature → example → test roadmap

Each row should become a curated public example session (small, deterministic,
no private bag) plus an e2e smoke assertion, so the feature is covered in
isolation. `2_native_chatter` / `1_heartbeat*` already cover plain stream
delivery + isolation.

| Feature | Concept exercised | Example session | Status |
|---|---|---|---|
| stream delivery, isolation | hz/latency | 1_heartbeat, 2_native_chatter | done |
| compression, sized payload | hz + size preserved | 3_/5_/4_/6_ | done |
| **latched / transient_local** | `mode: latched`, late-subscriber held value | TODO: `7_latched_static` | **TODO** |
| **drop N of M** | resulting hz within bounds | TODO: `8_drop` | **TODO** |
| **throttle** | resulting hz ≤ max | TODO: `9_throttle` | **TODO** |
| **restamp** | latency measurable after restamp | TODO: `10_restamp` | **TODO** |
| **trickle (receive side)** | local re-publish hz (native only) | TODO: `11_trickle` | **TODO** |
| **optional / required** | `presence: optional` absent ⇒ pass | TODO: fold into above | **TODO** |
| **completeness** | `min_count` / all bag msgs arrived | needs overview counts | **TODO** |
| **link overhead** | OTA bytes ≈ ROS payload bytes | needs link-bytes probe | **TODO** |
| framebridge, ffmpeg video | existence / throughput | maybe out of scope pass 1 | **TODO** |

## remote_assist status (private parent repo)

OTA transport works (cyclone) and the streaming contract passes after
recalibration (`restamp_if` added where headers exist; `latency_ms` dropped from
headerless topics; `/tf` rate floored). The latched topics carry
`mode: latched` + `presence: optional` and the `a_to_b` commands carry
`presence: optional` (+ a mock center publisher), so ota-smoke is unblocked at the
**contract** level. Two delivery bugs remain (flip the `optional`s to required
once fixed):

1. **Latched OTA delivery — FIXED** (on-change latch preserved; no
   retransmission). Root cause found via the b-side catmux logs:
   `ota_bridge_out` logged `New publisher ... '/com/out/b/site/latched', offering
   incompatible QoS ... DURABILITY` for *every* latched topic. The stock ROS
   `domain_bridge` auto-detects a topic's QoS from the live publisher and **races
   it**, defaulting to **volatile** when it bridges `/com/out` across domains
   47↔48 — which both drops the once-published held value and mismatches the
   transient_local OTA subscriber. The streaming topics are best_effort, so they
   matched and crossed; the latched ones (transient_local) did not. A second hop:
   the OTA-stage publisher inherited the global streaming `ota_pub` `lifespan:
   0.7s`, expiring a held value before a late receiver subscribed. **Fix:**
   `generate_session_files._db_entry` now pins `qos: {durability: transient_local,
   reliability: reliable}` on the domain_bridge entries for `/latched` topics
   (both directions), and the latched topics override `for_role.ota_pub.lifespan`
   so the held value does not expire. Result: `/site /type /vehicle
   /gnss_reference /mission/debug/drive_to_state` deliver their held value to the
   receiver's `app_in` (OK) with pure on-change latch. (An opt-in
   `shared.latch_keepalive_hz`, default 0, remains as an escape hatch for OTA
   paths that genuinely cannot carry transient_local — but it is NOT used here, as
   re-streaming would defeat the latch's bandwidth purpose.)
   *Still optional:* `/tf_static` (restamp/framebridge, not a latch topic — its
   bag publisher is transient_local so it needs a transient_local `latch_sub`, not
   the volatile one used for 1 Hz statics) and `/execution/debug/switchbox_state`
   (trickle; needs a send-side latch with a bag-matched `latch_sub` durability).
   Both want a per-topic `latch_sub` durability matching their recorded QoS.
2. **Center OUTBOUND relay.** `a_to_b` topics reach `native` (mock publishes) but
   the center's outbound relay does not forward: `/planning/free/reset` produces no
   `/com/out`, and `/move_base_free/goal`'s `framebridge: global_to_local` emits no
   `/globalframe`. The center peer never had outbound traffic before — this path is
   undertested. Needs pipeline debugging.

## Open TODO

- [x] Latched OTA delivery fixed (domain_bridge transient_local + ota_pub lifespan
      + direction-aware `mode: latched`). See the remote_assist section above.
- [x] Live status overview made mode-aware (latched/existence show OK; latched is
      direction-aware) in `status_overview_core.py` rollup.
- [ ] `/tf_static` and `/execution/debug/switchbox_state`: per-topic `latch_sub`
      durability matching the recorded bag QoS so these static one-shots deliver;
      then flip from optional to required.
- [ ] Center OUTBOUND relay (a_to_b): `/planning/free/reset` produces no
      `/com/out`; `/move_base_free/goal` framebridge emits no `/globalframe`.
- [ ] Curated public example sessions per feature (table above) + e2e assertions.
- [ ] `min_count`/completeness: overview must report received vs expected counts
      (and a finite source must declare its count); then assert in `status_eval`.
- [ ] `min_count`/completeness: overview must report received vs expected counts
      (and a finite source must declare its count); then assert in `status_eval`.
- [ ] Link-overhead assertion. **Do not reinvent the measurement** — it already
      exists in `com_py/topic_monitor.py`: `measure_peer_bytes(...)` /
      `_kick_link_measurement()` measure actual link bytes between `host_ip`↔
      `peer_ip` on the OTA interface (→ `_link_last_kbps`), and `print_stats()`
      already computes `ros_topic_bw` = summed per-topic payload Kbit/s. Remaining
      work: surface both into status.json and assert the ratio
      `link_kbps / ros_topic_bw` (≈1 good; ≫1 ⇒ retransmits / shadow connections /
      bad RMW or QoS) — likely a session-level expect, not per-topic.
- [ ] Fix the two remote_assist delivery bugs above; flip its `optional`s to
      required.
- [ ] Decide testability of framebridge / ffmpeg video (existence vs throughput).

### Replay-only (ground-truth) assertions — from "Live vs replay" above

- [ ] Detect replay mode (bag source present, or an explicit session flag) and
      only then enable the replay-only assertions below.
- [ ] Completeness / loss%: with the bag's per-topic message count as ground
      truth, assert `received ≥ ratio · sent` (subsumes `min_count`). Pairs with
      the receiver-side counts the overview must start reporting.
- [ ] Contract calibration: a reference replay run reports each topic's actual
      delivered hz / size / latency, to author and to *validate* `expect` (flag
      contradictory bounds, e.g. an hz floor above the achievable rate). Optional:
      emit a suggested `expect` block from a reference run.
- [ ] True OTA latency under replay: inject a send-time at the relay (sidecar
      stamp) so latency reflects the link, not the restamped payload header.
- [ ] Content integrity (advanced): compare received payloads to the sent
      ground truth (byte-equal, or equal after a declared transform).
