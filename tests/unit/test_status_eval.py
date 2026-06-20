from __future__ import annotations

import pytest

from rosotacom.status_eval import evaluate_report, evaluate_reports, expectations_from_cfg


def _stage(stage: str, hz: float, latency_ms: float | None, state: str = "FLOWING", publishers: int = 1) -> dict:
    return {"stage": stage, "hz": hz, "latency_ms": latency_ms, "state": state, "publishers": publishers}


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


def test_bad_quality_on_final_stage_fails_even_when_delivered() -> None:
    # The status overview now classifies stages against `expect`; a delivered
    # (overall OK) topic whose final stage is quality BAD violates its contract.
    bad_stage = {
        "stage": "app_in",
        "hz": 3.0,
        "latency_ms": 5.0,
        "state": "FLOWING",
        "quality": "BAD",
        "quality_reason": "hz",
    }
    report = {"peer": "b", "topics": [_topic("/heartbeat_a", "inbound", "OK", [bad_stage])]}
    failures = evaluate_report(report, {})
    assert len(failures) == 1 and "contract violated" in failures[0] and "hz" in failures[0]


# --- presence: required | optional ------------------------------------------


def _stalled(base: str, *, state: str, publishers: int) -> dict:
    t = _topic(base, "inbound", "STALLED", [_stage("app_in", 0.0, None, state, publishers)])
    t["diagnosis"] = "no publisher" if publishers == 0 else "stopped 9s ago"
    return t


def test_optional_topic_not_delivered_passes() -> None:
    # An optional topic (e.g. an a_to_b command with no source in a one-way
    # replay test) that never delivers is not a failure.
    report = {"peer": "a", "topics": [_stalled("/cmd", state="ABSENT", publishers=0)]}
    assert evaluate_report(report, {"/cmd": {"presence": "optional"}}) == []


def test_required_topic_not_delivered_still_fails() -> None:
    report = {"peer": "a", "topics": [_stalled("/cmd", state="ABSENT", publishers=0)]}
    failures = evaluate_report(report, {"/cmd": {"presence": "required"}})
    assert len(failures) == 1 and "STALLED" in failures[0]


# --- mode: latched ----------------------------------------------------------


def test_latched_delivered_then_idle_passes() -> None:
    # A latched/static topic delivers its value once and then stops ticking; the
    # final stage is STALE, overall is STALLED, but the held value arrived.
    report = {"peer": "a", "topics": [_stalled("/site", state="STALE", publishers=1)]}
    assert evaluate_report(report, {"/site": {"mode": "latched"}}) == []


def test_latched_never_delivered_fails() -> None:
    # Publisher present but no message ever observed -> the latch never arrived.
    report = {"peer": "a", "topics": [_stalled("/site", state="IDLE", publishers=1)]}
    failures = evaluate_report(report, {"/site": {"mode": "latched"}})
    assert len(failures) == 1 and "STALLED" in failures[0]


def test_latched_outbound_produced_passes_even_if_send_stage_idle() -> None:
    # On the sender a one-shot held value shows at native/processed but the
    # inferred OTA-send stage may not keep ticking; producing+latching is enough.
    topic = {
        "base": "/site",
        "direction": "outbound",
        "overall": "STALLED",
        "diagnosis": "stopped",
        "stages": [
            _stage("native", 1.0, None, "FLOWING"),
            _stage("processed", 0.0, None, "STALE"),
            _stage("ota_sent", 0.0, None, "IDLE"),
        ],
    }
    assert evaluate_report({"peer": "b", "topics": [topic]}, {"/site": {"mode": "latched"}}) == []


def test_latched_outbound_never_produced_fails() -> None:
    topic = {
        "base": "/site",
        "direction": "outbound",
        "overall": "STALLED",
        "diagnosis": "no publisher",
        "stages": [_stage("native", 0.0, None, "IDLE"), _stage("ota_sent", 0.0, None, "IDLE")],
    }
    assert len(evaluate_report({"peer": "b", "topics": [topic]}, {"/site": {"mode": "latched"}})) == 1


def test_latched_does_not_assert_rate_when_ok() -> None:
    # A latched topic that happens to be FLOWING must not fail on a stream-style
    # hz contract (it has none); only stream mode asserts hz/quality.
    bad_stage = {
        "stage": "app_in",
        "hz": 1.0,
        "latency_ms": 5.0,
        "state": "FLOWING",
        "quality": "BAD",
        "quality_reason": "hz",
        "publishers": 1,
    }
    report = {"peer": "a", "topics": [_topic("/site", "inbound", "OK", [bad_stage])]}
    assert evaluate_report(report, {"/site": {"mode": "latched"}}) == []


# --- mode: existence --------------------------------------------------------


def test_existence_with_publisher_passes() -> None:
    report = {"peer": "a", "topics": [_stalled("/diag", state="IDLE", publishers=1)]}
    assert evaluate_report(report, {"/diag": {"mode": "existence"}}) == []


def test_existence_without_publisher_fails() -> None:
    report = {"peer": "a", "topics": [_stalled("/diag", state="ABSENT", publishers=0)]}
    failures = evaluate_report(report, {"/diag": {"mode": "existence"}})
    assert len(failures) == 1


def test_invalid_mode_raises() -> None:
    report = {"peer": "a", "topics": [_stalled("/x", state="STALE", publishers=1)]}
    with pytest.raises(ValueError):
        evaluate_report(report, {"/x": {"mode": "bogus"}})
