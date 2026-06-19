# RFC 0001 — Expectation-driven, OTA-first test suite

**Status:** Implemented · **Scope:** test/example architecture · supersedes the marker
model in `docs/testing.md`

## Summary

Replace the current two-axis capability markers and external-probe test harness
with a single, OTA-first model built around **per-topic `expect` contracts** that
are consumed by *both* the live status overview and an automated `rosotacom test`.
Separate **examples** (few, curated, teaching) from **test-configs** (exhaustive,
generated), and run the full suite as a **manual gate before promotion** rather
than on every commit.

The implementation now includes the `expect` schema, `rosotacom test` reading the
session self-report, marker migration, curated examples, generated RMW
test-configs, and the manual full-suite promotion gate.

## Motivation

Today (`docs/testing.md`, `tests/e2e/test_smoke.py`, and the FZI parent harness):

- **Too many markers.** `single_machine: ok|na` × `multi_machine: ok|required|na`
  is 2×3 of mandatory boilerplate on every session.
- **`multi_machine: ok` vs `required` is a false distinction.** "required" just
  meant "OTA-only"; "ok" meant "also fakeable locally". That is not two OTA
  categories — it is one OTA membership plus a *local* property.
- **"multi-machine" is the wrong name.** This is an OTA product; OTA delivery is
  *the* thing under test, not a special tier.
- **Test metadata in `session-definition.yaml` felt out of place** — until you
  notice it should not be test-only metadata at all (see `expect`, below).
- **No categorization.** Educational examples, the RMW combination matrix, and
  heavy real-data sessions are all mixed together; the full run is already slow.

## The reframe

OTA delivery within a config's declared expectations is the product, so it is the
test. Everything else is a lens:

- **OTA is the default, not a tier.** A supported config is in the suite unless it
  is explicitly experimental.
- **Local testing is a speed optimization** — a fast pre-check for configs that can
  be faked on one host with distinct domain IDs. Never the target.
- **No locally-testable-but-not-OTA-deployable configs.** The only legitimate
  asymmetry is OTA-only (e.g. `1_heartbeat_cyclone-ota-tuned`, which hides local
  topics on a shared domain).

## Decisions (settled)

1. **`expect` is per-topic.**
2. **`rosotacom test` reads the session's self-report (`status.json`)** rather than
   probing externally.
3. **Generate the RMW matrix** instead of hand-maintaining one dir per combo.
4. **Cadence: nightly full local suite plus a manual external OTA gate before promotion.**

## Design

### 1. Per-topic `expect` — a behavioral contract, not test metadata

```yaml
topics:
  vehicle_to_center:
    - topic: /costmap/costmap
      expect:
        hz:         { min: 1, max: 5 }    # formalizes today's "2–5hz (OK)" comments
        latency_ms: { max: 300 }
        loss_pct:   { max: 5 }            # heartbeat payloads only
```

It belongs in the user-facing `session-definition.yaml` because it has a **runtime
role**, consumed by three things:

- **The live status overview** → the `status` node warns when a topic violates its
  contract. Already implemented for heartbeats (`heartbeat_in_monitor` declares
  `expected_hz`, `delay_bad_ms`, `loss3_*`); generalize `topic_monitor` to read
  per-topic `expect`. *(follow-up)*
- **`rosotacom test`** → assert the contract holds (this PR).
- **User configs** → because it is runtime config, `rosotacom test my_session`
  works for any session, not just packaged examples.

Isolation (a local-only topic must not cross) is a *universal* OTA invariant, not a
per-topic expectation, so it stays a framework probe (`probe-publish`/`probe-check`),
not a `session-definition` field.

### 2. `rosotacom test` — assert the self-report

The status overview already classifies every topic per stage
(`status_overview_core.py`: `state`, `quality`, `hz`, `latency_ms`; settled state
rolls up to `overall: OK`). So testing delivery is *reading the self-report*, not
re-probing from outside:

```
rosotacom test [session]
```
Reads each peer's `logs/<peer>/status/status.json` for the most recent instance and
asserts every crossed topic reached `overall: OK` and that inbound topics meet their
`expect`. Orchestration (bringing the session up) stays the caller's job — `smoke`
locally, or the multi-machine harness over two hosts — so the same `rosotacom test`
is the single oracle for both tiers. The pure evaluator is `status_eval.py`
(unit-tested against `status.json` fixtures).

This retires the external `ros2 topic hz` probing in the `verify` verb (PR #21):
delivery becomes self-report; `verify` is subsumed by `rosotacom test`.

### 3. Markers → at most one real axis

- **Drop `multi_machine: ok|required`.** Default = in the OTA suite.
- **Keep one honest flag,** the local fast-check eligibility, and prefer to *derive*
  it (distinct per-peer domains ⇒ fakeable; shared-domain hiding ⇒ not), allowing an
  explicit `local_check: false` override only on the exceptions.
- Net: from ~13 mandatory two-field blocks to a handful of one-line opt-outs.

### 4. Examples vs test-configs

| | Examples | Test-configs |
|---|---|---|
| Purpose | teach (one idea each) | exhaustive verification |
| Count | few, curated | many / generated |
| Location | `examples/sessions/` | `tests/sessions/` (or generated) |

- **Curate examples down.** The seven `1_heartbeat_<rmw>` dirs are a test matrix,
  not seven teaching examples — move them out.
- **Generate the RMW matrix** from one minimal-heartbeat template × a list of RMW
  combos. Adding a combo becomes one list entry. *(follow-up)*

### 5. Test types × cadence

**Types:** (A) RMW matrix — minimal heartbeat × every transport combo (generated);
(B) example validation — every example comes up + meets `expect`; (C) real-data /
complex (`remote_assist`-class over rosbag; private data lives in the FZI parent
harness first).

**Cadence:** per-PR runs L0 (unit/contract) + a small representative slice; the
**full suite is a manual gate the operator runs and confirms green before promoting
`main`** (decision 4). Cadence membership is a framework/dir decision, not another
per-session marker.

## Implementation status

**Implemented**
- `expect` accepted on topic entries (`generate_session_files.py` validator; ignored
  by generation).
- `status_eval.py`: pure evaluator of `status.json` against `expect`.
- `rosotacom test [session]`: reads the latest instance's per-peer `status.json` and
  asserts delivery + `expect`. Exit non-zero on any failure.
- Unit tests for the evaluator; validated end-to-end against a live local session.
- Per-topic `expect` flows into the `pipeline_spec` and the status overview now
  classifies each stage against it (`status_overview_core._classify_quality`); the
  contract is surfaced in `status.json` (`quality` / `expect`).
- `rosotacom test` leans on that verdict: a delivered topic whose final stage is
  `quality: BAD` fails (in addition to the raw-metric check).
- Heartbeat `expect` (`shared.heartbeat.expect`) drives both the status overview
  and the dedicated `heartbeat_in_monitor`: its `delay_bad_ms` / `loss3_bad_pct`
  come from `latency_ms.max` / `loss_pct.max`, and `expected_hz` tracks the
  publish rate. This is also where `loss_pct` is enforced.
- The two-axis `test_tiers` marker is retired. OTA membership is default; the
  local fast-check is derived from per-peer domains, with `local_check: false`
  available for OTA-only exceptions.
- The transport-specific heartbeat directories moved out of packaged examples
  into the generated `tests/sessions/rmw_matrix` test-config set.
- `verify` is retired. Delivery uses `rosotacom test`; local-only isolation stays
  a framework probe via `probe-publish` / `probe-check`.
- The full local suite runs nightly; the external OTA suite remains a manual
  promotion gate.

**Not feasible as written**
- `loss_pct` for *non-heartbeat* topics: ROS 2 messages carry no sequence number
  (only `header.stamp`), so the generic status node cannot detect gaps. Loss is
  only computable where the payload carries an explicit sequence (the heartbeat,
  handled above).

## Notes / open

- `status.json` `overall` is `OK` only once a session has settled; `rosotacom test`
  should poll/settle before asserting (the harness already waits).
- Heartbeat `expect` placement (no `topics:` entry) — likely `shared.heartbeat.expect`
  — is deferred with the monitor generalization.
