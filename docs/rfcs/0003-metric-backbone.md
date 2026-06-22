# RFC 0003 — Metric backbone (per-message transit records, offset-aware latency, real loss)

**Status:** Draft — design agreed, not yet implemented · **Scope:** measurement
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
  t_source    (sender clock)     — original header.stamp, or the wrap-input time
  t_wrap      (sender clock)     — OtaStamped.header.stamp           [exists today]
  t_com_in    (receiver clock)   — arrival immediately after bridge_in
  status      — delivered | lost | reordered                        [from seq gap]
  sections:
    preprocess = t_wrap   − t_source            (sender-local, exact, no sync)
    ota_hop    = (t_com_in − θ) − t_wrap         (θ = clock offset from echo)
```

| Wishlist item | Projection of the record |
|---|---|
| "latency split per processing section incl. OTA" | `sections` |
| "the time-sync error was X" | `θ` (echo-heartbeat) |
| "N messages lost, per topic" | `status` over the seq range |
| "say exactly, after a test drive, that …" | per-message, recorded, joined offline |

**Live** = the per-stage aggregates already in `status.json` (hz / latency /
**loss** per stage). **Forensic** = the per-`(topic, seq)` records dumped per run.
The status overview already writes an `events.jsonl` next to `status.json`
(`status_overview_core.py`), so the forensic dump has a home.

## Metric tiers (priority order, from the operator)

1. **OTA characteristics** — loss, size, bandwidth, latency *across the OTA hop*.
   The most important tier. Measured at `com_in` from `seq` + the echo RTT.
   **Always-on.**
2. **End-to-end latency** — `t_source` → final delivery (one cross-host hop, the
   rest local). **Always-on.**
3. **Pre-/post-processing overhead** — the cost of each local step (restamp,
   drop, compress, framebridge, transport). **Opt-in.**

This maps onto an always-on / opt-in split that also answers "must every metric
cross the OTA link?" — **no**:

| | Always-on, light | Opt-in, deep |
|---|---|---|
| What | transit-record digest: OTA loss/size/bw/latency + e2e | local stage-rosbag: every step separately |
| Key | `(topic, seq)`, cross-host | message index, intra-host |
| Where | small enough for `status.json` (optionally a live OTA digest) | stays local; pulled only for an analysis run |

## Three mechanisms

### 1. Per-topic loss from `OtaStamped.seq` — the RFC 0001 correction

`StageObservation` (`status_overview_core.py`) already deserializes each message
to record size and `header.stamp`. When the stage topic type is
`com_msgs/OtaStamped`, the observer additionally reads `msg.seq` and maintains a
per-stage sequence tracker — exactly the `expected_next_seq` / `missing` /
`reordered` / `max_burst_missing` math already written in
`heartbeat_in_monitor.py` (`WindowCounters`). `metrics()` then emits `loss_pct`
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

Replace the one-way heartbeat (`heartbeat_out_publisher` → `heartbeat_in_monitor`)
with a two-way echo. A new low-rate message carries four timestamps across an
A→B→A round trip (`t1` A-send, `t2` B-recv, `t3` B-send, `t4` A-recv, plus `seq`):

```
θ (offset) = ((t2 − t1) + (t3 − t4)) / 2        RTT = (t4 − t1) − (t3 − t2)
```

This is the NTP/PTP measurement run *inside ROS over the OTA link*. `θ` is
min-filtered over a window (the minimum-RTT sample carries the least queuing
noise) and is the only cross-host correction the latency decomposition needs. The
echo **subsumes** the one-way heartbeat: hz / loss / `status`
(GOOD/DEGRADED/BAD/LOST) all still derive from the same stream.

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

### 3. Latency decomposition — two sections free, the rest local

Add **one** field to `OtaStamped`: `source_stamp` (the original `header.stamp`
*before* wrapping, today recoverable only by deserializing the payload). At
`com_in` this yields two sections per message with no sidecar:

- `preprocess = t_wrap − source_stamp` — sender-local, exact, no sync.
- `ota_hop = (t_com_in − θ) − t_wrap` — the single offset-corrected cross-host
  section.

Deeper decomposition of the *local* pipeline (per-step cost of restamp / drop /
compress / framebridge / transport) does **not** need a new in-band field. Each
step already republishes on a suffixed stage topic; recording those stage topics
into a **local rosbag** and diffing consecutive stages offline gives every step's
cost. The join key is the **message index**, valid because the intra-host
pipeline is single-clock, lossless and in-order (no `seq` and no `θ` needed for
local steps). Per-stage `hz` and `mean_size_bytes` already exist in the status
overview, so only per-step *latency* is new, and it is an offline read.

> This is the systematic form of what is already done by hand: reading
> `ros2 topic delay` on successive suffixed stage topics and subtracting.

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
- **Wrap-point placement decides loss semantics.** Wrapping *after* shaping makes
  `loss_pct` mean pure unintended OTA loss. The exact current ordering of
  shaping vs. wrap in `generate_session_files` must be verified before relying on
  this (open question below).
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

## Build order (smallest lever first)

1. **`seq`-loss in `StageObservation` + `expect.loss_pct`.** Pure extension of
   existing code (the math exists in `heartbeat_in_monitor`; the `seq` exists in
   `OtaStamped`; the gap detection exists in the unwrapper). Immediately
   gate-able. Closes the RFC 0001 "not feasible" item. *(small, biggest
   immediate value)*
2. **`OtaStamped.source_stamp` + the two-section decomposition at `com_in`.**
   Cheap envelope change; gives per-message OTA-hop latency cleanly, headerless
   payloads included.
3. **Echo heartbeat + `θ`.** New message + monitor; the largest conceptual piece;
   unlocks RTT, health, and offset correction. Subsumes the one-way heartbeat.
4. **Forensic dump + offline joiner.** Per-`(topic, seq)` transit records into
   `events.jsonl`; an offline tool joins them for "exactly what happened after
   the run". Plus the local stage-rosbag per-step reader (mechanism 3). Pure
   analysis code, no ROS runtime change.

Steps 1–2 are >80 % wired already; step 3 is the real new development; step 4 is
analysis tooling.

## Open questions

- **Where does the loss computation live** — extend `StageObservation` (one
  measurement authority, flows straight into `status.json`) or a dedicated
  inbound monitor node? Leaning toward `StageObservation` for single-authority.
- **Verify the shaping-vs-wrap ordering** in `generate_session_files` so the
  `loss_pct` measurement point (mechanism 1) means what this RFC claims.
- **Exact `Echo` message fields** — three explicit `builtin_interfaces/Time`
  fields + `seq`, vs. reusing `header.stamp` for one of them.
- **Live OTA digest: yes or no?** The always-on transit-record digest is small
  enough to optionally cross the link for a live remote view — but per the
  transport/measurement separation it must be best-effort and never on the
  transport's critical path. Default: keep it local; send a digest only if a
  live remote view is explicitly wanted.
