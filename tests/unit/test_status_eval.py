from __future__ import annotations

from rosotacom.status_eval import evaluate_report, evaluate_reports, expectations_from_cfg


def _stage(stage: str, hz: float, latency_ms: float | None, state: str = "FLOWING") -> dict:
    return {"stage": stage, "hz": hz, "latency_ms": latency_ms, "state": state}


def _topic(base: str, direction: str, overall: str, stages: list[dict]) -> dict:
    return {"base": base, "direction": direction, "overall": overall, "stages": stages}


# Mirrors a settled 1_heartbeat status.json: inbound delivered through app_in,
# outbound observed locally at the native publish stage.
OK_REPORT = {
    "peer": "b",
    "topics": [
        _topic("/heartbeat_a", "inbound", "OK", [_stage("com_in", 10.0, 5.3), _stage("app_in", 10.0, 6.9)]),
        _topic("/heartbeat_b", "outbound", "OK", [_stage("native", 10.0, None)]),
    ],
}


def test_all_ok_without_expectations_passes() -> None:
    assert evaluate_report(OK_REPORT, {}) == []


def test_non_ok_overall_fails_with_diagnosis() -> None:
    report = {"peer": "a", "topics": [_topic("/x", "inbound", "STALLED", [_stage("com_in", 0.0, None, "STALE")])]}
    report["topics"][0]["diagnosis"] = "stopped 4s ago"
    failures = evaluate_report(report, {})
    assert len(failures) == 1 and "STALLED" in failures[0] and "stopped 4s ago" in failures[0]


def test_empty_report_fails() -> None:
    assert evaluate_report({"peer": "a", "topics": []}, {})


def test_expect_within_bounds_passes() -> None:
    expect = {"/heartbeat_a": {"hz": {"min": 8, "max": 12}, "latency_ms": {"max": 200}}}
    assert evaluate_report(OK_REPORT, expect) == []


def test_expect_hz_too_low_fails() -> None:
    failures = evaluate_report(OK_REPORT, {"/heartbeat_a": {"hz": {"min": 50}}})
    assert len(failures) == 1 and "hz" in failures[0]


def test_expect_latency_exceeded_fails() -> None:
    failures = evaluate_report(OK_REPORT, {"/heartbeat_a": {"latency_ms": {"max": 1}}})
    assert len(failures) == 1 and "latency" in failures[0]


def test_expect_only_checked_on_inbound_side() -> None:
    # Delivery (and thus the contract) is observed inbound; an outbound topic's
    # expect is not asserted against its local publish stage.
    assert evaluate_report(OK_REPORT, {"/heartbeat_b": {"hz": {"min": 50}}}) == []


def test_expectations_from_cfg_collects_only_expect_blocks() -> None:
    cfg = {"topics": {"b_to_a": [{"topic": "/c", "expect": {"hz": {"min": 1}}}, {"topic": "/d"}, "/e"]}}
    assert expectations_from_cfg(cfg) == {"/c": {"hz": {"min": 1}}}


def test_evaluate_reports_aggregates_across_peers() -> None:
    bad = {"peer": "a", "topics": [_topic("/x", "inbound", "ABSENT", [])]}
    failures = evaluate_reports({"a": bad, "b": OK_REPORT}, {})
    assert len(failures) == 1 and "ABSENT" in failures[0]
