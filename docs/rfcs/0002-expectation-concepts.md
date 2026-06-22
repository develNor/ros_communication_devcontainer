# RFC 0002 — Richer expectation concepts (presence / mode / completeness / overhead)

**Status:** Implemented & verified — every roadmap item below is built and verified
cross-host (examples 1–13 pass; remote_assist 100% green). The only forward-looking
note is content-integrity's transform-aware / non-String *extension*. · **Scope:**
test/example architecture · extends [RFC 0001](0001-expectation-driven-test-suite.md)

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
| **latched / transient_local** | `mode: latched`, held value | `7_latched_static` | done (delivered+held verified) |
| **drop N of M** | resulting hz within bounds | `8_drop` | done (10→5.00 Hz verified) |
| **throttle** | resulting hz ≤ max | `9_throttle` | done (20→4.33 Hz cap) |
| **restamp** | latency measurable after restamp | `10_restamp` | done (1970 stamp → 22 ms verified) |
| **trickle (receive side)** | local re-publish hz (now a stage) | `11_trickle` | done (1→~5 Hz upsample) |
| optional / required | `presence: optional` absent ⇒ pass | remote_assist a_to_b | done |
| **completeness** | `min_count` + per-peer `completeness.min_ratio` | remote_assist `/tf` | done |
| **link overhead** | wire bytes / ROS payload ratio | remote_assist (status `link`) | done |
| framebridge | local↔global transform delivered | remote_assist (`/tf`, `/move_base_free/goal`) | done |
| ffmpeg video | existence / throughput | n/a -- no video feature in the codebase | n/a |

## remote_assist status (private parent repo)

OTA transport works (cyclone) and the streaming contract passes after
recalibration (`restamp_if` added where headers exist; `latency_ms` dropped from
headerless topics; `/tf` rate floored). The latched topics carry `mode: latched`
and the `a_to_b` commands are now `presence: required` (driven by a mock center
publisher). **remote_assist is now 100% green -- every topic `presence: required`,
nothing optional, OK=22 / STALLED=0 on both peers.** The delivery issues that were
once `optional` are all fixed:

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
   *`/tf_static` + `/execution/debug/switchbox_state` are now `presence: required`
   too* — see the Open-TODO item below: the earlier "needs a transient_local
   latch_sub" theory was wrong; they are simply published late in the bag (88 s /
   60 s) and are caught live once the loop reaches them within the test window.
2. **Center OUTBOUND relay — FIXED** (both `a_to_b` commands `presence: required`,
   green on a+b). The center peer never carried outbound traffic before, so two
   generation bugs hid in `generate_session_files`:
   - *target_prefix.* The center uses `target_prefix`, so its app/mock publish
     `/to_b/<topic>` and `relay_out` subscribes the prefixed name — but the OUT
     pipeline's `native`/`processed` stages used the **unprefixed** topic, so
     `native` flowed yet `relay_out` got nothing. Fix: the OUT `native`/`processed`
     stages now carry the same `/to_<remote>` prefix.
   - *global_to_local direction.* This framebridge is a **receiver-side** transform.
     The earlier model treated it like a sender-side one and expected a `/globalframe`
     `processed` stage on a (which a never produces). Correct model: the center's
     application produces the **global** topic directly (`/move_base_free/goal/
     globalframe`), that IS the OTA `final` (so a's `native` = `final`, no sender
     `processed` stage), and **b's** framebridge subscribes `.../globalframe` and
     publishes the local base `/move_base_free/goal` (the inbound `native_in`). This
     matches the framebridge node's `PubSubPair` (sub = base + `/globalframe`,
     pub = base), which was always correct. Verified: on b, `/move_base_free/goal`
     is FLOWING through `native_in`. `/move_base_free/goal` carries `mode: latched`
     (the mock's static PoseStamped has `stamp=0` ⇒ latency is not meaningful);
     `/planning/free/reset` (headerless Empty) is `mode: stream`.

## Open TODO

- [x] Latched OTA delivery fixed (domain_bridge transient_local + ota_pub lifespan
      + direction-aware `mode: latched`). See the remote_assist section above.
- [x] Live status overview made mode-aware (latched/existence show OK; latched is
      direction-aware) in `status_overview_core.py` rollup.
- [x] `/tf_static` and `/execution/debug/switchbox_state` fixed -- and it was NOT a
      QoS bug (every transient_local / bag-play-cache theory was wrong and reverted).
      Reading the MCAP per-message timestamps showed these are published LATE in the
      bag (`/tf_static` at 88.2 s, `/switchbox_state` at 59.8 s of the 100 s recording),
      not at t=0. The smoke collected artifacts the instant the *required* topics went
      green (well before 60 s of bag time), so those two simply had not been published
      yet. Fix: flip them to `presence: required`; the looping bag reaches their publish
      point within the 120 s test window and the held value is caught live (subscribers
      long since connected). Both are now `mode: latched, presence: required` and green
      on a+b. Lesson: when a bag topic looks undeliverable, check WHEN it is published
      (mcap timestamps) before blaming QoS.
- [x] Center OUTBOUND relay (a_to_b) fixed — OUT `native`/`processed` stages carry
      the `/to_<remote>` `target_prefix` so they agree with `relay_out`'s
      subscription; and `global_to_local` is modelled as a receiver-side transform
      (sender ships the global `/globalframe` topic as its native = OTA `final`; b's
      framebridge produces the local base as `native_in`). Both commands are now
      `presence: required` and green on a+b. See the remote_assist section above.
- [x] Curated public example sessions per feature (table above) + e2e assertions.
      All five processing features now have a verified cross-host example:
      `7_latched_static` (held value delivered, STALE between ticks), `8_drop`
      (10→5.00 Hz), `9_throttle` (20→4.33 Hz cap), `10_restamp` (1970 stamp → 22 ms
      measured latency), `11_trickle` (1 Hz source → 5.00 Hz delivered). Three
      enablers landed:
      - `expect.smoke_native_hz` (`cli._smoke_native_publish_rate`): drive the
        synthetic source faster/slower than the asserted (post-processing) rate.
      - A stale-stamped `geometry_msgs/msg/PointStamped` smoke source (1970 stamp)
        so restamp has an observable effect (the monitor guards absurd ages to a
        null latency, which a `latency_ms` contract fails without restamp).
      - **Trickle made observable**: the receiver-side trickle re-publish
        (`<final>/trickle`) is now a first-class monitored `native_in` stage
        (`generate_session_files._postprocessed_topic` + `cli._smoke_postprocessed_topic`),
        so its rate can be asserted — previously it was a status blind spot.
- [x] `min_count` + per-peer `completeness.min_ratio` (RFC concept "completeness").
      The status overview already records `messages_total` per stage, so `status_eval`
      asserts (a) the delivered final stage saw >= `min_count` messages and (b) within
      one peer's pipeline `final_stage_count / first_flowing_stage_count >= min_ratio`
      (catches a stage that is FLOWING but dropping, with no cross-peer clock-skew).
      Applied live to remote_assist `/tf` (`min_count: 50`, `min_ratio: 0.7`): verified
      203/203 delivered (ratio 1.000). The true cross-peer "did every bag message
      arrive" (exact source count) stays in the replay-only section below.
- [x] Link-overhead assertion (clean rewrite, single measurement authority). The
      status overview *already* measures per-stage size×rate, so it owns the ROS
      payload side; the only thing missing was the wire side. New `com_py/
      link_bytes.py` reads the kernel's interface byte counters (`/proc/net/dev`
      `rx_bytes`/`tx_bytes` deltas) -- **no tshark, no sudo, no extra node** --
      and `status_overview_core.compute_link_overview` divides directionally:
      `link_tx / Σ com_out payload` (out) and `link_rx / Σ com_in payload` (in),
      emitting a session-level `link` block in status.json. `status_eval` asserts
      it via a top-level `link: { max_ratio | max_ratio_out | max_ratio_in }`
      (≈1 good; ≫1 ⇒ retransmits / shadow connections / bad QoS). `topic_monitor`
      was improved at the same time: it now reuses the same `link_bytes` sampler
      and its `sudo tshark` path (`measure_peer_bytes` / `tshark_cmd` /
      `_kick_link_measurement`, which was broken anyway -- tshark is not in the
      image) is deleted, removing the duplicate ROS-bandwidth + link code.
- [x] remote_assist center OUTBOUND (a_to_b) bug fixed; both commands flipped to
      `presence: required`. (And the `/tf_static` + `/switchbox_state` late-publish
      edge is now fixed too — see above — so remote_assist has no `optional` topics
      left: OK=22 / STALLED=0 on both peers.)
- [x] framebridge / ffmpeg video testability decided. framebridge is exercised
      end-to-end by remote_assist (`/tf` local→global, `/move_base_free/goal`
      global→local, both delivered to `native_in`); ffmpeg video is n/a -- there is no
      video/ffmpeg feature anywhere in the codebase, so there is nothing to test.

### Replay-only (ground-truth) assertions — from "Live vs replay" above

- [x] Replay mode is enabled by an explicit `--bag <dir>` flag on `rosotacom test`
      (and `calibrate`): the replay-only assertions run only when a bag ground truth is
      supplied, otherwise they skip cleanly. (An explicit flag is the "explicit session
      flag" option; auto-detecting a bag source is unnecessary on top of it.)
- [x] Completeness / loss% vs the bag -- `expect.completeness.vs_bag_ratio: R` asserts
      `delivered_hz ≥ R · native_hz` (wired via `test --bag`, which threads the bag's
      native rate into `status_eval`). This is the *rate* form of `received ≥ ratio·sent`
      (rate = count / window), equivalent to and subsuming `min_count` while avoiding the
      count/loop-window alignment fragility of raw counts -- and the overview already
      reports per-stage counts (`messages_total`). Verified on `/tf`: delivered 19 Hz of
      107 Hz native (18%) → `0.1` passes, `0.5` fails with "excessive OTA loss".
- [x] Contract calibration (bag-as-ground-truth). `rosotacom calibrate --bag <dir>
      [<session>]` reads the bag's own `metadata.yaml` (pure YAML -- no rosbag2/mcap
      decode, `src/rosotacom/bag_ground_truth.py`) and reports per-topic ground truth
      (count, native hz, msg type, durability). Given a session it validates `expect`
      against the bag and flags contradictions: an `hz.min` above the native rate
      (the OTA link only thins, never amplifies -- e.g. the original `/tf` `min 40`
      that prompted the recalibration), and a static transient_local topic (≈ once,
      held) described as a stream. Verified on remote_assist (20 expectations
      consistent).
- [x] Completeness / loss% vs the bag's native rate. `expect.completeness.vs_bag_ratio: R`
      asserts the receiver's delivered hz >= R * the bag's native hz, i.e. at least R
      of the source crossed the link -- catching loss BEFORE the first observed stage
      (e.g. best_effort decimation at the send QoS), which the within-peer ratio can't
      see. Wired via `rosotacom test --bag <dir>` (loads the ground truth and threads it
      to `status_eval`). Verified on remote_assist `/tf`: delivered 19 Hz of 107 Hz native
      (18%) -- `vs_bag_ratio 0.1` passes, `0.5` fails with an "excessive OTA loss" message.
- [x] Suggested-`expect` emitter. `rosotacom test --suggest` reads the current run's
      status.json and prints a starter `expect` block per inbound topic from the observed
      hz/latency (a delivered-but-idle topic → `mode: latched`; an undelivered one →
      `presence: optional`). `status_eval.suggest_expectations`. Verified on remote_assist
      (/tf → hz 11–28, /can/twist → +latency 94 ms, /site → latched).
- [x] True OTA latency via an injected send-time. Two complementary paths, both
      reflecting the link rather than a payload-stamp's age:
      - *headered* topics: `restamp` stamps send-now; the receiver measures recv−send
        (verified 22 ms on 10_restamp and remote_assist `/can/twist`).
      - *headerless* topics: the OTA wrapper (`use_ota_wrapper`) already sets the
        OtaStamped header to the relay's SEND time -- an out-of-band send-time sidecar.
        RFC 0003 adds `source_stamp` and echo-derived clock-offset correction.
        New `expect.latency_ms.stage` redirects the latency assertion to the stage
        carrying that stamp (`com_in`), so offset-aware OTA transit latency is assertable even
        for a headerless payload. Example `13_link_latency` (a plain String): verified
        com_in latency 98.8 ms cross-host, asserted < 2000 ms, while the unwrapped
        native_in (headerless) is correctly latency-null.
- [x] Content integrity (first cut: pass-through byte-equality). A new smoke step
      `_ota_verify_content_integrity` echoes each delivered PASS-THROUGH String topic
      on the receiver (`rosotacom probe-content --topic --type --field --expect`) and
      asserts the field byte-equals what the sender published -- catching silent
      corruption / truncation / wrong serialization that presence/rate/latency miss.
      Restricted to untransformed topics (received name == published base), since a
      transform (restamp/framebridge/compress) is intentionally not byte-equal. Built
      on the isolation-probe pattern + the synthetic publishers' known payloads (the
      ground truth); pure compare in `cli.content_matches`. Example `12_content_integrity`
      verified cross-host (`/integrity_demo` == "rosotacom smoke"). **Still future:**
      transform-aware comparison (compare after the declared transform) and non-String
      types, which need a receiver-side recorder + per-type decode.
