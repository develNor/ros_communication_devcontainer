# RFC 0003 — Metric backbone (per-message transit records, offset-aware latency, real loss)

**Status:** Implemented · **Scope:** measurement
architecture (the layer *below* the verdict) · extends
[RFC 0001](0001-expectation-driven-test-suite.md) and
[RFC 0002](0002-expectation-concepts.md)

## Summary

RFC 0001/0002 built the **verdict** layer: per-topic `expect` contracts asserted
from a self-reported `status.json`, with `presence` / `mode` / `completeness` /
link-overhead. That layer leans on whatever the status overview can measure
today, which has three structural gaps:

1. **Loss is heartbeat-only.** RFC 0001 listed per-topic `loss_pct` under "not
   feasible as written" because "ROS 2 messages carry no sequence number (only
   `header.stamp`)". That is true for *native* topics but **obsolete for any
   wrapped topic**: `com_msgs/OtaStamped` already carries a per-source-topic
   `seq` (set in `universal_ota_wrapper`), and `universal_ota_unwrapper` already
   detects the gaps in `_check_sequence` — it only *logs* them instead of
   quantifying them.
2. **Latency is one-way and clock-skew-dependent.** Every latency is
   `now − header.stamp` (`topic_monitor.py`, `status_overview_core.py`), guarded
   by a "Clocks unsynced" fallback when the age exceeds 1000 s. It cannot be
   decomposed per pipeline section, and across hosts it silently carries the
   unknown clock offset between the two machines.
3. **Aggregates hide the dominant failure mode.** Field measurements (private
   harness lab notes) show the dominant degradation on a cellular uplink is
   *irregularity*, not raw loss: a heavy message head-of-line-blocks the next
   one, which is then effectively lost (overwritten by the one after). A mean
   latency hides "message N was slow, message N+1 never arrived."

This RFC defines the **measurement backbone** those gaps need:

- a single per-`(topic, seq)` **transit record** as the unifying artifact,
- **three mechanisms** to populate it (seq-loss, echo-heartbeat, latency
  decomposition), each anchored on infrastructure that already exists,
- an honest statement of what is and is **not** identifiable without a shared
  physical clock.

It **feeds** the existing verdict layer (unlocks `expect.loss_pct`; sharpens
`expect.latency_ms.stage`) rather than replacing it.

## The unifying artifact: the transit record

Every metric on the wishlist is a projection of one recorded object per message:

```
(topic, seq):
  t_wrap      (sender clock)     — OtaStamped.header.stamp
  t_com_in    (receiver clock)   — arrival immediately after bridge_in
  status      — delivered | lost | reordered                        [from seq gap]
  sections:
    ota_hop   = (t_com_in + θ) − t_wrap          (θ = sender/peer − receiver/local)
  inter_arrival / jitter         — receiver-local regularity
```

The envelope carries only what the cross-host **OTA hop** needs (`t_wrap` +
`seq`). Local / per-stage latency — including the message's pre-OTA pipeline cost —
is read uniformly from the `stage_latency` join (mechanism 3), **not** from an
in-band stamp.

| Wishlist item | Projection of the record |
|---|---|
| "latency split per processing section incl. OTA" | OTA hop from `sections`; per-step local split from `stage_latency` |
| "the time-sync error was X" | `θ` (echo-heartbeat) |
| "N messages lost, per topic" | `status` over the seq range |
| "say exactly, after a test drive, that …" | per-message, recorded, joined offline |

**Live** = the per-stage aggregates already in `status.json` (hz / latency /
**loss** per stage). **Forensic** = the per-`(topic, seq)` records dumped per run.
The status overview writes the forensic rows into `events.jsonl` next to
`status.json`. Rows carry `kind: transit` or `kind: state_transition`, so one
append-only timeline contains both message evidence and pipeline state changes.

## Metric tiers (priority order, from the operator)

1. **OTA characteristics** — loss, size, bandwidth, latency *across the OTA hop*.
   The most important tier. Measured at `com_in` from `seq` + the echo RTT.
   **Always-on.**
2. **End-to-end latency** — composed, not a single in-band field: a headered final
   stage already reports `now − header.stamp`, and the full per-stage split comes
   from `stage_latency`; the transit record contributes the OTA hop.
3. **Pre-/post-processing overhead** — the cost of each local step (restamp,
   drop, compress, framebridge, transport). **Opt-in.**

This maps onto an always-on / opt-in split that also answers "must every metric
cross the OTA link?" — **no**:

| | Always-on, light | Opt-in, deep |
|---|---|---|
| What | transit-record digest: OTA loss/size/bw/latency | local stage-rosbag: every step separately |
| Key | `(topic, seq)`, cross-host | message index, intra-host |
| Where | small enough for `status.json` (optionally a live OTA digest) | stays local; pulled only for an analysis run |

## Three mechanisms

### 1. Per-topic loss from `OtaStamped.seq` — the RFC 0001 correction

`StageObservation` (`status_overview_core.py`) already deserializes each message
to record size and `header.stamp`. When the stage topic type is
`com_msgs/OtaStamped`, the observer additionally reads `msg.seq` and maintains a
per-stage sequence tracker using the same `expected_next_seq` / `missing` /
`reordered` / `max_burst_missing` model as heartbeat monitoring. `metrics()` then emits `loss_pct`
(windowed) and `reordered`; `classify_stage` surfaces them alongside the existing
`latency_ms` / `messages_total`; `status_eval` gains a generic
`expect.loss_pct: { max }` for **any** wrapped topic.

**Measurement point (a real decision, not a detail).** A `seq` lives only between
the wrap and the unwrap. Loss is therefore measured **only on the wrapped stage
`com_in`** — immediately after `bridge_in`, before unwrap/processing. Gaps
*downstream* of `drop` / `throttle` are **intended shaping**, not loss. With this
placement `loss_pct` is cleanly defined as *"the fraction of what was sent that
did not survive the OTA hop"* (network loss + intended QoS decimation such as
`best_effort`/`depth 1`), and is structurally distinct from bandwidth shaping —
which is precisely the quantity worth budgeting.

This is also the per-message form of RFC 0002's `completeness.vs_bag_ratio`
(which is the *rate* form: `delivered_hz ≥ R · native_hz`). The rate form stays
the cheap live gate; the seq form gives the exact lost-seq list for forensics.

### 2. Echo heartbeat — RTT, health, and a bounded clock offset

Replace the one-way heartbeat with a symmetric piggyback echo. Every fixed-rate
`EchoHeartbeat` is both a fresh probe and the reply to the latest peer probe:
`header.stamp` is the current send time, and `echo_t1` / `echo_t2` / `echo_t3`
carry the echoed probe's send, peer receive, and peer reply times. The local
receive time is `t4`. This keeps each direction at the configured heartbeat rate
instead of doubling traffic with separate request/response streams:

```
θ (offset) = ((t2 − t1) + (t3 − t4)) / 2        RTT = (t4 − t1) − (t3 − t2)
```

This is the NTP/PTP measurement run *inside ROS over the OTA link*. `θ` is
min-RTT-filtered over a 60-second window and is the only cross-host correction
the latency decomposition needs. The echo subsumes the one-way heartbeat:
Hz, sequence loss, RTT, offset, and `GOOD` / `BAD` / `LOST` health derive from
the same stream. Wrapped-topic offset correction requires
`shared.use_heartbeat: true`; echo-free load tests still get exact sequence loss
and an explicitly uncorrected delay, while corrected latency remains `null`.

**What the echo is actually for.** Not asymmetry correction (see Limits) and not
even primarily offset correction — field data puts the bench offset in the low
single-digit milliseconds, negligible against 30–500 ms OTA latencies. Its
operational value is **RTT + continuous health tracking**, which catches the
degradation states the lab notes describe (latency staying high even at trivial
load until the communication container is restarted).

**Why an echo and not just chrony.** The deployment endpoints
(vehicles, control center) are not ours to administer — no host-level time config
can be assumed. The echo is the only offset/RTT mechanism that needs **zero host
configuration** and measures the *direct* peer-to-peer offset, rather than
inferring it from two independent internet-NTP syncs. Where an endpoint is
GPS-time-disciplined, its stamps are UTC-true and provide a free ground-truth
cross-check — a bonus, never a dependency.

### 3. Latency decomposition — the OTA hop in-band, all local timing via `stage_latency`

The transit record carries exactly **one** per-message section: the cross-host
`ota_hop = (t_com_in + θ) − t_wrap`. That is all the envelope needs — `t_wrap`
(`OtaStamped.header.stamp`) plus `seq`.

**All local / per-stage latency is read uniformly from the `stage_latency`
join**, not from an in-band stamp. Set `shared.metric_backbone.record_stages:
true` to record every generated local stage topic into an MCAP rosbag under the
run's `logs/<peer>/metrics/` directory; `ros2 run com_py stage_latency BAG
TOPIC...` joins bag receive timestamps **by message index** and reports
consecutive-stage costs. This works because the local path is single-clock and
in-order.

> This is the systematic form of what is already done by hand: reading
> `ros2 topic delay` on successive suffixed stage topics and subtracting.

> **Earlier design, reverted.** A first cut added an `OtaStamped.source_stamp`
> field to surface a `preprocess = t_wrap − source_stamp` section in-band. It was
> removed: the number was misleading (≈ the wrap step only for headerless topics;
> source-staleness incl. pre-rosotacom age for headered), redundant with the
> payload stamp where one exists, and already covered by `stage_latency`. Local
> timing now has a single, uniform home.
>
> **Index-join caveat.** The `stage_latency` index join is valid only across
> **non-decimating (1:1) stages**. A `drop` / `throttle` stage gives the next
> topic fewer messages, so positional index *i* stops referring to the same
> message, and headerless messages carry no key to repair it. Per-step *timing* is
> therefore meaningful across the latency-adding 1:1 steps (compress / framebridge
> / transport); decimating steps are characterized by **rate**, not per-message
> index.

## Honest limits (design notes)

- **Asymmetric one-way delay is not identifiable from the echo alone.** `θ` and
  the up/down path asymmetry are confounded; the split assumes path symmetry
  (as NTP/PTP do). Defeating it needs a shared physical clock (GPS/PTP), which
  the cross-cellular deployment does not have. Mitigation: report **end-to-end
  and RTT exactly** (RTT is offset-free), report per-direction one-way delay
  under the stated symmetry assumption. Field evidence supports the assumption:
  a 0-byte symmetric run measured near-equal up/down delay, i.e. the round-trip
  *sum* (offset-free) is exact and the implied offset is a few ms — noise against
  the latencies of interest.
- **Per-message tracing ends at the unwrap.** Native messages have no `seq`, so
  fine-grained per-message tracking covers the **wrapped portion**
  (source → wrap → OTA → `com_in`); downstream native processing is measured in
  aggregate (mean / percentile per stage). This is deliberate: it avoids
  threading a `seq` through nodes we do not control.
- **Loss vs. intended decimation** are both `seq` gaps — correctly so (both mean
  "did not cross"), which is why loss must never be read downstream of intended
  `drop` / `throttle` (see mechanism 1).
- **Wrap-point placement decides loss semantics.** The implemented pipeline runs
  restamp/latch/drop/throttle/pixel/frame transforms before compression and
  wrapping. Therefore `com_in` sequence gaps exclude intended pre-wrap shaping
  and measure failure after the wrap point.
- **`θ` drifts.** Re-estimate continuously; fine for short runs, relevant for
  hour-long sessions.
- **Head-of-line blocking is the real failure mode.** Field data: a heavy message
  delays the next, which is then overwritten/lost; load latency is
  rate-dependent, not size-only (the same payload at a higher rate shows
  markedly higher delay). Two consequences for this backbone and for benchmarks
  built on it:
  - the load surface is **2-D (size × rate) → latency/loss**, not size alone;
  - the transit record should carry **inter-arrival regularity / jitter** as a
    derived metric, because irregularity — not raw loss — is the operative
    failure. Per-message records (not averages) are what make it visible.

## Empirical baseline

The design above is shaped by real cellular-uplink measurements maintained in the
**private harness** (host-specific numbers and the per-link fragmentation limits
stay there, per the repo boundary — not reproduced in this public submodule).
The *publishable* qualitative findings that drive the design:

- **Latency rises monotonically with message size** on a cellular uplink, and
  there is a single-message size ceiling above which delivery becomes unreliable
  (fragmentation barrier).
- **Latency is bandwidth/queueing-dependent**, not size-only: the same payload at
  a higher publish rate incurs substantially more delay.
- **Irregular message sizes degrade delivery** (head-of-line blocking) — evening
  out the size/rate is more effective than reducing average load.
- A codec stage (ffmpeg) contributes a fixed, separately-measurable
  encode/decode cost — the canonical case for the per-step decomposition.

These belong in the RFC as *motivation*; the calibrated per-profile numbers are a
**budget** maintained per network profile (see RFC 0001/0002 expectations and the
profile work), not hardcoded here.

## Implementation checklist

- [x] Track `OtaStamped.seq` at inbound `com_in`; expose window and run-total
  loss, reorder count, and maximum missing burst in `status.json`.
- [x] Implement generic `expect.loss_pct: { max, stage? }` evaluation.
- [x] Read all local / per-stage latency from the `stage_latency` message-index
  join; the transit record's only in-band section is the OTA hop. (An earlier
  `OtaStamped.source_stamp` / `preprocess` section was added then **reverted** —
  see mechanism 3.)
- [x] Emit corrected/uncorrected OTA hop, offset, RTT, size, inter-arrival, and
  jitter data.
- [x] Replace the one-way heartbeat implementation with `EchoHeartbeat` and a
  symmetric piggyback echo node.
- [x] Continuously estimate peer offset from the minimum-RTT sample and state the
  symmetric-path assumption in artifacts.
- [x] Append per-`(topic, seq)` delivered/lost/reordered forensic records to
  `events.jsonl`.
- [x] Add `rosotacom metrics EVENTS...` to join duplicate rows and summarize
  exact loss plus p50/p95 section latency and jitter.
- [x] Add opt-in local stage rosbag recording and the message-index latency
  reader.
- [x] Verify shaping precedes wrapping and record the resulting loss semantics.
- [x] Cover sequence math, offset math, contracts, forensic joining, generated
  configuration, and stage joins with host tests.

## Implementation decisions and reality checks

- **Measurement authority:** sequence and transit accounting lives in
  `StageObservation`; the status overview remains the single writer of live and
  forensic artifacts.
- **Echo wire format:** `EchoHeartbeat.header.stamp` is the current probe's send
  time. The explicit `echo_t1/t2/t3` fields reply to the newest peer probe.
- **Offset sign:** artifacts report `peer_offset_ms = peer_clock − local_clock`.
  Corrected inbound delay is `t_com_in(local) + peer_offset − t_wrap(peer)`.
- **No silent fallback:** without a valid echo estimate, corrected OTA latency is
  `null`; the uncorrected value remains visible separately.
- **Echo is explicit for load isolation:** wrapped-payload benchmarks may keep
  `use_heartbeat: false` when even the small probe stream would contaminate the
  load surface. They retain exact loss; only offset-aware latency is unavailable.
- **Digest placement:** forensic rows stay local and never create another OTA
  payload stream. Combining peer artifacts remains an offline operation.
- **Sequence caveat:** a gap is materialized when a later sequence arrives. A
  missing tail at shutdown is not inferable without a sender-side end marker.
