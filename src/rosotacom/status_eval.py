"""Evaluate a session's self-reported status (status.json) against the per-topic
`expect` contract declared in its session-definition.

This is the test-side half of the expectation-driven model (see
docs/rfcs/0001-expectation-driven-test-suite.md): the live status overview already
classifies every topic (state/quality/hz/latency per stage); `rosotacom test`
reads that self-report and asserts each crossed topic was delivered (overall OK)
and that the inbound side meets its declared hz/latency expectations. Pure
functions (no ROS/Docker), so they are unit-testable against status.json fixtures.
"""

from __future__ import annotations

from typing import Any

STATUS_OK = "OK"


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


def _final_stage(topic: dict[str, Any]) -> dict[str, Any] | None:
    """The deepest *flowing* stage of a topic pipeline (its delivered end)."""
    stages: list[dict[str, Any]] = topic.get("stages") or []
    flowing = [s for s in stages if s.get("state") == "FLOWING"]
    if flowing:
        return flowing[-1]
    return stages[-1] if stages else None


def _check_expect(peer: str, base: str, stage: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    hz, hz_exp = stage.get("hz"), expect.get("hz") or {}
    if "min" in hz_exp and (hz is None or hz < hz_exp["min"]):
        failures.append(f"[{peer}] {base}: hz {hz} < expected min {hz_exp['min']}")
    if "max" in hz_exp and (hz is None or hz > hz_exp["max"]):
        failures.append(f"[{peer}] {base}: hz {hz} > expected max {hz_exp['max']}")
    lat, lat_exp = stage.get("latency_ms"), expect.get("latency_ms") or {}
    if "max" in lat_exp and (lat is None or lat > lat_exp["max"]):
        failures.append(f"[{peer}] {base}: latency {lat}ms > expected max {lat_exp['max']}ms")
    return failures


def evaluate_report(report: dict[str, Any], expect_by_topic: dict[str, dict[str, Any]]) -> list[str]:
    """Failures for one peer's status.json (empty == all good)."""
    peer = report.get("peer", "?")
    topics = report.get("topics", [])
    if not topics:
        return [f"[{peer}] status report has no topics"]
    failures: list[str] = []
    for topic in topics:
        base = topic.get("base", "?")
        overall = topic.get("overall")
        if overall != STATUS_OK:
            diag = topic.get("diagnosis")
            failures.append(f"[{peer}] {base}: status {overall}" + (f" ({diag})" if diag else ""))
            continue
        # Expectations are about delivered behavior, observed on the inbound side.
        expect = expect_by_topic.get(base)
        if expect and topic.get("direction") == "inbound":
            stage = _final_stage(topic)
            if stage is None:
                failures.append(f"[{peer}] {base}: no observable stage to check expectations")
            else:
                failures += _check_expect(peer, base, stage, expect)
    return failures


def evaluate_reports(reports: dict[str, dict[str, Any]], expect_by_topic: dict[str, dict[str, Any]]) -> list[str]:
    """Failures across every peer's status.json (empty == all good)."""
    failures: list[str] = []
    for report in reports.values():
        failures += evaluate_report(report, expect_by_topic)
    return failures
