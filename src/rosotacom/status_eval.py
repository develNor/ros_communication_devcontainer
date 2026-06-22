"""Evaluate a session's self-reported status (status.json) against the per-topic
`expect` contract declared in its session-definition.

This is the test-side half of the expectation-driven model (see
docs/rfcs/0001-expectation-driven-test-suite.md and
docs/rfcs/0002-expectation-concepts.md): the live status overview already
classifies every topic (state/quality/hz/latency per stage); `rosotacom test`
reads that self-report and asserts each crossed topic meets its declared
contract. Pure functions (no ROS/Docker), so they are unit-testable against
status.json fixtures.

Per-topic `expect` supports (all optional):

  presence: required | optional   (default required) -- an optional topic that
            does not deliver is not a failure (e.g. a bidirectional command
            topic with no source in a one-directional replay test).
  mode:     stream | latched | existence   (default stream)
            - stream:    must deliver end-to-end and currently be FLOWING; hz /
                         latency_ms / quality are asserted.
            - latched:   must have delivered its value at least once (and may be
                         held via transient_local). Rate is NOT required -- a
                         static/latched topic publishes on change and then idles.
            - existence: only requires the topic to be present in the graph (a
                         publisher exists). For irregular topics where neither
                         rate nor a held value is meaningful.
  hz:       { min, max }           (stream only)
  latency_ms: { max, stage }       (stream only) -- asserted on the final delivered
            stage by default; `stage: <name>` redirects it to a named pipeline stage.
            Use `stage: com_in` for a wrapped (use_ota_wrapper) topic so true OTA
            transit latency is read from the OtaStamped send-time the relay injects,
            even when the final unwrapped payload is headerless.
  min_count: N                     (stream only) -- the delivered (final flowing)
            stage must have observed at least N messages over the run. A floor on
            volume: distinguishes a real stream that crossed end-to-end from a
            single sample that trickled through. Robust to clock skew (one peer's
            own cumulative count).
  completeness: { min_ratio: R }   (stream only) -- within THIS peer's pipeline,
            final_stage_count / first_flowing_stage_count >= R. Catches a stage
            that is FLOWING but dropping (a lossy relay/framebridge/transport):
            the messages enter the pipeline but a fraction never reach the end.
            Uses a single monitor's counts, so there is no cross-peer timing
            fragility. For the true cross-peer "did every bag message arrive",
            see the replay-only section of docs/rfcs/0002.
"""

from __future__ import annotations

from typing import Any

STATUS_OK = "OK"

# Stage states reported by the status overview (status_overview_core.py).
_DELIVERED_STATES = {"FLOWING", "STALE"}  # a message was observed at least once

_VALID_MODES = {"stream", "latched", "existence"}
_VALID_PRESENCE = {"required", "optional"}


def expectations_from_cfg(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map a topic's base name -> its `expect` block, from a session config."""
    out: dict[str, dict[str, Any]] = {}
    topics = cfg.get("topics")
    if not isinstance(topics, dict):
        return out
    for entries in topics.values():
        for item in entries or []:
            if isinstance(item, dict) and isinstance(item.get("expect"), dict) and item.get("topic"):
                out[str(item["topic"])] = item["expect"]
    return out


def link_expect_from_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """The session-level link-overhead expectation, from a top-level `link:` block.

    e.g.  link: { max_ratio: 4.0 }              # both directions
          link: { max_ratio_out: 3, max_ratio_in: 6 }
    """
    link = cfg.get("link")
    return link if isinstance(link, dict) else {}


def _check_link(peer: str, link: dict[str, Any] | None, expect: dict[str, Any]) -> list[str]:
    """Assert the measured link/payload overhead ratio stays under its bound.

    Skips silently when there is no link sample (sampling disabled / no interface)
    or no payload in a direction (ratio is None): the ratio is only meaningful
    when both the wire and the ROS payload are flowing."""
    if not link or not expect:
        return []
    failures: list[str] = []
    default_max = expect.get("max_ratio")
    checks = [
        (
            "outbound",
            link.get("overhead_ratio_out"),
            expect.get("max_ratio_out", default_max),
            link.get("link_tx_kbps"),
            link.get("ros_payload_out_kbps"),
        ),
        (
            "inbound",
            link.get("overhead_ratio_in"),
            expect.get("max_ratio_in", default_max),
            link.get("link_rx_kbps"),
            link.get("ros_payload_in_kbps"),
        ),
    ]
    for direction, ratio, max_ratio, link_kbps, payload_kbps in checks:
        if max_ratio is None or ratio is None:
            continue
        if ratio > float(max_ratio):
            failures.append(
                f"[{peer}] link overhead ({direction}) ratio {ratio} > max {max_ratio} "
                f"(wire {link_kbps} kbps vs ROS payload {payload_kbps} kbps -- "
                f"retransmits / shadow connections / bad QoS?)"
            )
    return failures


def _topic_mode(expect: dict[str, Any]) -> str:
    mode = str(expect.get("mode", "stream")).strip().lower() or "stream"
    if mode not in _VALID_MODES:
        raise ValueError(f"expect.mode must be one of {sorted(_VALID_MODES)}, got {mode!r}")
    return mode


def _is_optional(expect: dict[str, Any]) -> bool:
    presence = str(expect.get("presence", "required")).strip().lower() or "required"
    if presence not in _VALID_PRESENCE:
        raise ValueError(f"expect.presence must be one of {sorted(_VALID_PRESENCE)}, got {presence!r}")
    return presence == "optional"


def _final_stage(topic: dict[str, Any]) -> dict[str, Any] | None:
    """The deepest *flowing* stage of a topic pipeline (its delivered end)."""
    stages: list[dict[str, Any]] = topic.get("stages") or []
    flowing = [s for s in stages if s.get("state") == "FLOWING"]
    if flowing:
        return flowing[-1]
    return stages[-1] if stages else None


def _was_delivered(topic: dict[str, Any]) -> bool:
    """True if this peer's end of the pipeline ever observed a message (the final
    stage is FLOWING or STALE). For a latched topic this means the held value
    reached its destination even though it no longer ticks."""
    stages: list[dict[str, Any]] = topic.get("stages") or []
    if not stages:
        return False
    return stages[-1].get("state") in _DELIVERED_STATES


def _was_produced(topic: dict[str, Any]) -> bool:
    """True if any stage was ever observed flowing. For an OUTBOUND latched topic
    the held value is a one-shot: the source/latch stages show it (FLOWING/STALE)
    but the inferred OTA-send stage may not keep ticking, so requiring the final
    stage is wrong on the sender. Actual OTA delivery is asserted by the receiver's
    inbound report."""
    stages: list[dict[str, Any]] = topic.get("stages") or []
    return any(s.get("state") in _DELIVERED_STATES for s in stages)


def _has_publisher(topic: dict[str, Any]) -> bool:
    """True if any stage advertises a publisher (the topic exists in the graph)."""
    stages: list[dict[str, Any]] = topic.get("stages") or []
    return any((s.get("publishers") or 0) > 0 for s in stages)


def _named_stage(topic: dict[str, Any], name: str) -> dict[str, Any] | None:
    stages: list[dict[str, Any]] = topic.get("stages") or []
    for s in stages:
        if s.get("stage") == name:
            return s
    return None


def _check_expect(
    peer: str, base: str, topic: dict[str, Any], stage: dict[str, Any], expect: dict[str, Any]
) -> list[str]:
    failures: list[str] = []
    hz, hz_exp = stage.get("hz"), expect.get("hz") or {}
    if "min" in hz_exp and (hz is None or hz < hz_exp["min"]):
        failures.append(f"[{peer}] {base}: hz {hz} < expected min {hz_exp['min']}")
    if "max" in hz_exp and (hz is None or hz > hz_exp["max"]):
        failures.append(f"[{peer}] {base}: hz {hz} > expected max {hz_exp['max']}")
    lat_exp = expect.get("latency_ms") or {}
    # latency_ms.stage redirects the latency check to a named stage -- e.g. `com_in`,
    # where a wrapped (OtaStamped) topic carries the relay's injected send-time, so
    # TRUE OTA latency is assertable even when the final unwrapped payload is headerless.
    lat_stage = stage
    stage_name = lat_exp.get("stage")
    if stage_name:
        named = _named_stage(topic, stage_name)
        if named is None:
            failures.append(f"[{peer}] {base}: latency_ms.stage '{stage_name}' not in pipeline")
            return failures
        lat_stage = named
    lat = lat_stage.get("latency_ms")
    if "max" in lat_exp and (lat is None or lat > lat_exp["max"]):
        where = f" at '{stage_name}'" if stage_name else ""
        failures.append(f"[{peer}] {base}: latency {lat}ms{where} > expected max {lat_exp['max']}ms")
    return failures


def _stage_count(stage: dict[str, Any]) -> int:
    try:
        return int(stage.get("messages_total") or 0)
    except (TypeError, ValueError):
        return 0


def _check_completeness(peer: str, base: str, topic: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    """Volume/loss assertions over a single peer's own per-stage message counts."""
    failures: list[str] = []
    min_count = expect.get("min_count")
    comp = expect.get("completeness") or {}
    min_ratio = comp.get("min_ratio")
    if min_count is None and min_ratio is None:
        return failures

    delivered = [s for s in (topic.get("stages") or []) if s.get("state") in _DELIVERED_STATES]
    if not delivered:
        return failures  # presence/mode already account for nothing being delivered
    final_count = _stage_count(delivered[-1])

    if min_count is not None and final_count < int(min_count):
        failures.append(f"[{peer}] {base}: delivered {final_count} msgs < expected min_count {int(min_count)}")
    if min_ratio is not None:
        first = delivered[0]
        first_count = _stage_count(first)
        if first_count > 0:
            ratio = final_count / first_count
            if ratio < float(min_ratio):
                failures.append(
                    f"[{peer}] {base}: completeness {final_count}/{first_count}={ratio:.2f} "
                    f"< expected min_ratio {min_ratio} (dropping between "
                    f"'{first.get('stage')}' and '{delivered[-1].get('stage')}')"
                )
    return failures


def _check_bag_completeness(
    peer: str,
    base: str,
    stage: dict[str, Any],
    expect: dict[str, Any],
    ground_truth: dict[str, Any] | None,
) -> list[str]:
    """Cross-reference the delivered rate against the bag's known native rate (replay).

    `expect.completeness.vs_bag_ratio: R` asserts delivered_hz >= R * native_hz, i.e.
    at least R of the source actually crossed the OTA link. Unlike the within-peer
    completeness ratio, this catches loss that happens BEFORE the first observed
    stage (e.g. best_effort decimation at the send QoS). Only meaningful in a replay
    test, so it is skipped unless a bag ground truth was supplied."""
    comp = expect.get("completeness") or {}
    vs_bag = comp.get("vs_bag_ratio")
    if vs_bag is None or not ground_truth:
        return []
    gt = ground_truth.get(base) or {}
    native_hz = gt.get("native_hz")
    if not native_hz or native_hz <= 0:
        return []
    delivered_hz = stage.get("hz") or 0.0
    ratio = delivered_hz / native_hz
    if ratio < float(vs_bag):
        return [
            f"[{peer}] {base}: delivered {delivered_hz:.1f} Hz is {ratio:.0%} of the bag's "
            f"native {native_hz:.1f} Hz (< vs_bag_ratio {vs_bag}) -- excessive OTA loss."
        ]
    return []


def evaluate_report(
    report: dict[str, Any],
    expect_by_topic: dict[str, dict[str, Any]],
    link_expect: dict[str, Any] | None = None,
    ground_truth: dict[str, Any] | None = None,
) -> list[str]:
    """Failures for one peer's status.json (empty == all good)."""
    peer = report.get("peer", "?")
    topics = report.get("topics", [])
    if not topics:
        return [f"[{peer}] status report has no topics"]
    failures: list[str] = _check_link(peer, report.get("link"), link_expect or {})
    for topic in topics:
        base = topic.get("base", "?")
        expect = expect_by_topic.get(base) or {}
        mode = _topic_mode(expect)
        optional = _is_optional(expect)
        overall = topic.get("overall")

        if overall == STATUS_OK:
            stage = _final_stage(topic)
            # Stream contracts assert delivered behaviour against the monitor's
            # own verdict and the declared thresholds. latched/existence topics
            # only need to have reached OK, which they have.
            if mode == "stream":
                if stage and stage.get("quality") == "BAD":
                    reason = stage.get("quality_reason")
                    detail = f": {reason}" if reason else ""
                    failures.append(f"[{peer}] {base}: contract violated (quality BAD{detail})")
                    continue
                if expect and topic.get("direction") == "inbound" and stage is not None:
                    failures += _check_expect(peer, base, topic, stage, expect)
                    failures += _check_completeness(peer, base, topic, expect)
                    failures += _check_bag_completeness(peer, base, stage, expect, ground_truth)
            continue

        # overall != OK -- reinterpret per the declared delivery mode.
        if mode == "latched":
            # Receiver (inbound): the held value must have reached the final stage.
            # Sender (outbound): a one-shot held value need only have been produced
            # and latched -- its OTA delivery is asserted by the receiver's report.
            if topic.get("direction") == "outbound":
                if _was_produced(topic):
                    continue
            elif _was_delivered(topic):
                continue
        if mode == "existence" and _has_publisher(topic):
            continue  # present in the graph as required
        if optional:
            continue  # optional: non-delivery is not a failure

        diag = topic.get("diagnosis")
        failures.append(f"[{peer}] {base}: status {overall}" + (f" ({diag})" if diag else ""))
    return failures


def evaluate_reports(
    reports: dict[str, dict[str, Any]],
    expect_by_topic: dict[str, dict[str, Any]],
    link_expect: dict[str, Any] | None = None,
    ground_truth: dict[str, Any] | None = None,
) -> list[str]:
    """Failures across every peer's status.json (empty == all good)."""
    failures: list[str] = []
    for report in reports.values():
        failures += evaluate_report(report, expect_by_topic, link_expect, ground_truth)
    return failures


def _suggest_one(topic: dict[str, Any]) -> dict[str, Any] | None:
    """A suggested `expect` block for one delivered inbound topic, from observation."""
    stage = _final_stage(topic)
    if stage is None or stage.get("state") not in _DELIVERED_STATES:
        # Never delivered in the reference run -> a one-directional/no-source topic.
        return {"presence": "optional"}
    hz = stage.get("hz")
    if not hz or hz <= 0:
        # Delivered but not ticking -> a static/latched held value.
        return {"mode": "latched"}
    expect: dict[str, Any] = {
        # A band around the observed rate: wide enough to tolerate run-to-run
        # jitter, tight enough to catch a regression. Author should narrow these.
        "hz": {"min": max(0.1, round(hz * 0.6, 1)), "max": round(hz * 1.5, 1)}
    }
    lat = stage.get("latency_ms")
    if lat is not None:
        expect["latency_ms"] = {"max": int(round(lat * 2 + 50))}
    return expect


def suggest_expectations(reports: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Suggest an `expect` block per topic from a reference run's status.json(s).

    Uses the inbound (receiver) view -- the side where the contract is asserted --
    and the deepest delivered stage's observed hz/latency. A starting point to
    author/`calibrate` a contract, not a substitute for human judgement. Pure and
    order-stable; topics seen only outbound (no inbound observation) are skipped."""
    out: dict[str, dict[str, Any]] = {}
    for report in reports.values():
        for topic in report.get("topics", []):
            if topic.get("direction") != "inbound":
                continue
            base = topic.get("base")
            if not base or base in out:
                continue
            suggestion = _suggest_one(topic)
            if suggestion is not None:
                out[str(base)] = suggestion
    return out
