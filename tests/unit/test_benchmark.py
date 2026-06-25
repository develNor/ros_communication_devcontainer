"""RFC 0005 validation checklist — host tests for the benchmark genre logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from rosotacom.benchmark import (
    BudgetEntry,
    BudgetKey,
    CapacitySlice,
    Direction,
    MetricSpec,
    OracleThresholds,
    OutageWindow,
    SweepBounds,
    capacity_binary_search,
    compare_to_budget,
    expand_size_pattern,
    find_baseline,
    find_capacity,
    guard_shared_link,
    linear_ramp,
    load_budget,
    offered_bandwidth_bps,
    oracle_passes,
    oracle_passes_topic,
    parse_size_pattern,
    pattern_mean_bytes,
    recovery_metrics,
    save_budget,
    size_ceiling,
    within_shared_link_budget,
)

# --- a/b size-pattern publisher -------------------------------------------- #


def test_size_pattern_generation_matches_a_b_sequence() -> None:
    assert parse_size_pattern("a*4,b*1") == ["a", "a", "a", "a", "b"]
    assert parse_size_pattern("ax4,bx1") == ["a", "a", "a", "a", "b"]
    assert parse_size_pattern("") == ["a"]
    # The canonical baseline failure-mode load: 4×0 B + 1×70 KB.
    assert expand_size_pattern("a*4,b*1", size_a=0, size_b=70_000) == [0, 0, 0, 0, 70_000]
    assert pattern_mean_bytes("a*4,b*1", size_a=0, size_b=70_000) == 14_000


def test_size_pattern_rejects_unset_b_and_bad_tokens() -> None:
    with pytest.raises(ValueError):
        expand_size_pattern("a,b", size_a=10)  # size_b missing
    with pytest.raises(ValueError):
        parse_size_pattern("c*2")
    with pytest.raises(ValueError):
        parse_size_pattern("a*0")


# --- capacity binary-search driver + oracle -------------------------------- #


def test_oracle_passes_requires_loss_and_latency_under_bound() -> None:
    thresholds = OracleThresholds(max_loss_pct=1.0, max_latency_ms=200.0)
    assert oracle_passes(0.5, 150.0, thresholds)
    assert not oracle_passes(2.0, 150.0, thresholds)  # loss too high
    assert not oracle_passes(0.5, 250.0, thresholds)  # latency too high
    assert not oracle_passes(0.0, None, thresholds)  # nothing delivered


def test_oracle_reads_a_transit_summary_topic() -> None:
    thresholds = OracleThresholds(max_loss_pct=1.0, max_latency_ms=200.0)
    passing = {"loss_pct": 0.0, "ota_hop_ms": {"p50": 80.0, "p95": 150.0}}
    failing = {"loss_pct": 0.0, "ota_hop_ms": {"p50": 80.0, "p95": 300.0}}
    assert oracle_passes_topic(passing, thresholds)
    assert not oracle_passes_topic(failing, thresholds)


def test_capacity_binary_search_finds_the_breakpoint() -> None:
    # Stubbed metric source: anything up to 1500 B passes (monotone), bigger fails.
    def probe(size: int) -> bool:
        return size <= 1500

    assert capacity_binary_search(0, 70_000, probe) == 1500
    # Even the smallest probe fails -> no capacity.
    assert capacity_binary_search(100, 70_000, lambda size: False) is None


def test_capacity_binary_search_probes_fixed_range_once() -> None:
    probed: list[int] = []

    def probe(size: int) -> bool:
        probed.append(size)
        return True

    assert capacity_binary_search(1, 1, probe) == 1
    assert probed == [1]


def test_find_capacity_states_its_slice() -> None:
    slice_ = CapacitySlice(profile="cellular-emulated", knob="size", fixed={"rate": 10.0})
    result = find_capacity(slice_, 0, 70_000, lambda size: size <= 8000)
    assert result.capacity == 8000
    assert result.slice.profile == "cellular-emulated"
    assert result.slice.fixed == {"rate": 10.0}


# --- sweep bounds + shared-link guard -------------------------------------- #


def test_size_ceiling_respects_bandwidth_budget_at_rate() -> None:
    bounds = SweepBounds(max_size=100_000, max_bandwidth_bps=8_000_000)  # 1 MB/s
    # At 10 Hz the bandwidth budget caps payload at 100 KB; max_size also 100 KB.
    assert size_ceiling(bounds, rate_hz=10.0) == 100_000
    # At 100 Hz the bandwidth budget bites first: 1 MB/s / 100 Hz = 10 KB.
    assert size_ceiling(bounds, rate_hz=100.0) == 10_000


def test_shared_link_guard_blocks_lan_saturation_but_allows_shaped() -> None:
    bounds = SweepBounds(max_bandwidth_bps=8_000_000)
    assert offered_bandwidth_bps(200_000, 10.0) == 16_000_000
    assert not within_shared_link_budget(200_000, 10.0, bounds)
    # On a shared/unshaped link the guard refuses to saturate the LAN.
    with pytest.raises(ValueError):
        guard_shared_link(200_000, 10.0, bounds, shared_link=True)
    # On a shaped profile the profile's own rate cap bounds the load -> allowed.
    guard_shared_link(200_000, 10.0, bounds, shared_link=False)


def test_find_capacity_never_searches_past_the_shared_link_budget() -> None:
    bounds = SweepBounds(max_bandwidth_bps=8_000_000)
    slice_ = CapacitySlice(profile="unshaped-lan", knob="size", fixed={"rate": 10.0})
    probed: list[int] = []

    def probe(size: int) -> bool:
        probed.append(size)
        return True  # link would pass at any size; only the bound should stop us

    result = find_capacity(slice_, 0, 1_000_000, probe, bounds=bounds, rate_hz=10.0)
    # Capacity clamped to the 1 MB/s budget at 10 Hz (100 KB), never the 1 MB request.
    assert result.capacity == 100_000
    assert max(probed) <= 100_000


# --- budget store + regression compare ------------------------------------- #


def test_budget_compare_flags_regression_per_metric_direction() -> None:
    key = BudgetKey(sha="abc123", profile="cellular-emulated", genre="capacity")
    specs = [
        MetricSpec("capacity_bytes", Direction.HIGHER_IS_BETTER, rel_tolerance=0.05),
        MetricSpec("p95_latency_ms", Direction.LOWER_IS_BETTER, abs_tolerance=10.0),
    ]
    baseline = {"capacity_bytes": 10_000.0, "p95_latency_ms": 150.0}
    # Capacity dropped 20% (regression); latency only +5 ms, within the 10 ms band (ok).
    current = {"capacity_bytes": 8_000.0, "p95_latency_ms": 155.0}
    comparison = compare_to_budget(key, specs, baseline, current)
    flagged = {c.name: c.regressed for c in comparison.comparisons}
    assert flagged == {"capacity_bytes": True, "p95_latency_ms": False}
    assert comparison.regressed


def test_budget_compare_tolerates_within_band_and_improvements() -> None:
    key = BudgetKey(sha="abc123", profile="lan", genre="capacity")
    specs = [
        MetricSpec("capacity_bytes", Direction.HIGHER_IS_BETTER, rel_tolerance=0.05),
        MetricSpec("p95_latency_ms", Direction.LOWER_IS_BETTER, abs_tolerance=10.0),
    ]
    baseline = {"capacity_bytes": 10_000.0, "p95_latency_ms": 150.0}
    current = {"capacity_bytes": 12_000.0, "p95_latency_ms": 90.0}  # both better
    comparison = compare_to_budget(key, specs, baseline, current)
    assert not comparison.regressed


def test_budget_store_roundtrip_and_baseline_lookup(tmp_path: Path) -> None:
    path = tmp_path / "budgets.jsonl"
    entries = [
        BudgetEntry(BudgetKey("sha1", "lan", "capacity"), {"capacity_bytes": 9_000.0}),
        BudgetEntry(BudgetKey("sha2", "lan", "capacity"), {"capacity_bytes": 10_000.0}),
        BudgetEntry(BudgetKey("sha2", "cellular", "recovery"), {"t_recover_s": 1.2}),
    ]
    save_budget(path, entries)
    loaded = load_budget(path)
    assert loaded == entries
    baseline = find_baseline(loaded, profile="lan", genre="capacity")
    assert baseline is not None and baseline.metrics["capacity_bytes"] == 10_000.0  # most recent
    assert find_baseline(loaded, profile="wifi", genre="capacity") is None


# --- recovery driver + metric set ------------------------------------------ #


def _transit(seq: int, status: str, t_wrap: float | None, ota_ms: float | None = 50.0, topic: str = "/x") -> dict:
    return {
        "kind": "transit",
        "topic": topic,
        "seq": seq,
        "status": status,
        "t_wrap": t_wrap,
        "sections": {"ota_hop_ms": ota_ms if status != "lost" else None},
    }


def test_recovery_metrics_from_synthetic_timeline() -> None:
    # 1 Hz publisher, 100 ms OTA hop. Good until t=2, outage 2.5..5.0, restore at 5.0.
    period = 1.0
    records = [
        _transit(0, "delivered", 0.0, ota_ms=100.0),
        _transit(1, "delivered", 1.0, ota_ms=100.0),
        _transit(2, "delivered", 2.0, ota_ms=100.0),
        _transit(3, "lost", None),  # send time reconstructed to t=3.0 (in outage)
        _transit(4, "lost", None),  # send time reconstructed to t=4.0 (in outage)
        # Reconnect burst: the backlog dumps in a clump just after restore.
        _transit(5, "delivered", 5.0, ota_ms=100.0),  # arrives 5.10
        _transit(6, "delivered", 5.05, ota_ms=100.0),  # arrives 5.15
        _transit(7, "delivered", 5.1, ota_ms=100.0),  # arrives 5.20
        # Cadence returns to the nominal 1 Hz.
        _transit(8, "delivered", 6.0, ota_ms=100.0),  # arrives 6.10
        _transit(9, "delivered", 7.0, ota_ms=100.0),  # arrives 7.10
    ]
    metrics = recovery_metrics(
        records,
        OutageWindow(start=2.5, end=5.0),
        nominal_period_s=period,
        burst_window_s=1.0,
        latched_topics=["/x"],
    )
    # First arrival after restore is seq 5 at 5.0 + 100 ms = 5.10 -> 0.10 s after restore.
    assert metrics.t_recover == pytest.approx(0.10, abs=1e-6)
    # seq 5,6,7 all arrive within 1 s of restore -> burst of 3 (seq 8 at 6.10 excluded).
    assert metrics.recovery_burst == 3
    # Two messages (seq 3,4) lost during the outage on /x.
    assert metrics.lost_during_outage == {"/x": 2}
    # The latched topic delivered again after restore.
    assert metrics.latched_rearrival == {"/x": True}
    # Burst gaps (0.05 s) are below the nominal band; the 0.9 s gap into seq 8 (6.10)
    # is the first near-nominal inter-arrival -> steady 1.1 s after restore.
    assert metrics.t_steady == pytest.approx(1.1, abs=1e-6)


def test_recovery_metrics_handles_total_loss_during_outage() -> None:
    records = [
        _transit(0, "delivered", 0.0),
        _transit(1, "lost", None),
        _transit(2, "lost", None),
    ]
    metrics = recovery_metrics(
        records,
        OutageWindow(start=1.0, end=3.0),
        nominal_period_s=1.0,
        latched_topics=["/x"],
    )
    assert metrics.t_recover is None  # nothing arrived after restore
    assert metrics.t_steady is None
    assert metrics.recovery_burst == 0
    assert metrics.lost_during_outage == {"/x": 2}
    assert metrics.latched_rearrival == {"/x": False}


# --- coarse linear ramp (monitor-only) ------------------------------------- #


def test_linear_ramp_builds_the_response_curve() -> None:
    curve = linear_ramp([1_000, 2_000, 4_000], measure=lambda size: size / 1000.0 * 20.0)
    assert [(point.value, point.metric) for point in curve] == [
        (1_000.0, 20.0),
        (2_000.0, 40.0),
        (4_000.0, 80.0),
    ]
