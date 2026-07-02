from __future__ import annotations

import json
import statistics
from pathlib import Path

from rosotacom.transit import (
    filter_transit_records_by_publish_window,
    join_transit_records,
    load_transit_records,
    summarize_transit_records,
)


def _record(seq: int, status: str, ota_ms: float | None = None, t_wrap: float | None = None) -> dict:
    return {
        "kind": "transit",
        "topic": "/x",
        "seq": seq,
        "status": status,
        "t_wrap": t_wrap,
        "sections": {"ota_hop_ms": ota_ms},
        "jitter_ms": 2.0 if ota_ms is not None else None,
    }


def test_load_join_and_summarize_transit_records(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    events = [
        {"kind": "state_transition", "topic": "/x"},
        _record(1, "lost"),
        _record(1, "delivered", 10.0),
        _record(2, "lost"),
        _record(3, "reordered", 30.0),
    ]
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    records = load_transit_records([path])
    joined = join_transit_records(records)
    assert [(record["seq"], record["status"]) for record in joined] == [
        (1, "delivered"),
        (2, "lost"),
        (3, "reordered"),
    ]
    summary = summarize_transit_records(records)["topics"]["/x"]
    assert summary["expected"] == 3
    assert summary["delivered"] == 2
    assert summary["lost"] == 1
    assert summary["loss_pct"] == 33.333
    assert summary["ota_hop_ms"]["p95"] == 30.0


def test_join_keeps_same_topic_sequence_from_different_sources() -> None:
    a = {**_record(1, "delivered", 10.0), "source": "a", "target": "b"}
    b = {**_record(1, "delivered", 20.0), "source": "b", "target": "a"}
    joined = join_transit_records([a, b])
    assert len(joined) == 2
    summary = summarize_transit_records([a, b])["topics"]
    assert set(summary) == {"a->b:/x", "b->a:/x"}


def test_filter_transit_records_by_publish_window_keeps_only_bounded_losses() -> None:
    records = [
        {**_record(1, "delivered", 10.0, 9.9), "source": "a", "target": "b"},
        {**_record(2, "lost"), "source": "a", "target": "b"},
        {**_record(3, "delivered", 10.0, 10.1), "source": "a", "target": "b"},
        {**_record(4, "lost"), "source": "a", "target": "b"},
        {**_record(5, "delivered", 10.0, 10.5), "source": "a", "target": "b"},
        {**_record(6, "lost"), "source": "a", "target": "b"},
        {**_record(7, "delivered", 10.0, 11.2), "source": "a", "target": "b"},
    ]

    filtered = filter_transit_records_by_publish_window(records, start_s=10.0, end_s=11.0)

    assert [(record["seq"], record["status"]) for record in filtered] == [
        (3, "delivered"),
        (4, "lost"),
        (5, "delivered"),
    ]


def test_filter_transit_records_by_publish_window_keeps_delayed_tail_delivery() -> None:
    records = [
        {**_record(1, "delivered", 10.0, 10.0), "source": "a", "target": "b"},
        {**_record(2, "delivered", 50.0, 10.99), "source": "a", "target": "b"},
        {**_record(3, "delivered", 10.0, 11.1), "source": "a", "target": "b"},
    ]

    filtered = filter_transit_records_by_publish_window(records, start_s=10.0, end_s=11.0)
    summary = summarize_transit_records(filtered)["topics"]["a->b:/x"]

    assert [(record["seq"], record["status"]) for record in filtered] == [
        (1, "delivered"),
        (2, "delivered"),
    ]
    assert summary["expected"] == 2
    assert summary["lost"] == 0
    assert summary["ota_hop_ms"]["p95"] == 50.0


def test_summarize_reports_full_latency_distribution() -> None:
    records = [_record(seq, "delivered", ota_ms=10.0 + 5.0 * seq, t_wrap=10.0 + 0.1 * seq) for seq in range(12)]

    latency = summarize_transit_records(records)["topics"]["/x"]["ota_hop_ms"]

    assert latency["min"] == 10.0
    assert latency["max"] == 65.0
    assert latency["mean"] == 37.5
    assert latency["p50"] == 35.0
    assert latency["p95"] == 65.0
    assert latency["p99"] == 65.0
    assert latency["std"] == round(statistics.pstdev(10.0 + 5.0 * seq for seq in range(12)), 3)


def test_summarize_reports_latency_trend_between_first_and_last_third() -> None:
    # Latency ramps 10 -> 65 ms over 12 messages published at 100 ms cadence:
    # the first-third median stays low, the last-third median is high.
    records = [_record(seq, "delivered", ota_ms=10.0 + 5.0 * seq, t_wrap=10.0 + 0.1 * seq) for seq in range(12)]

    trend = summarize_transit_records(records)["topics"]["/x"]["ota_hop_trend_ms"]

    assert trend == {"first_third_p50": 15.0, "last_third_p50": 55.0, "delta": 40.0}


def test_summarize_latency_trend_needs_enough_stamped_points() -> None:
    records = [_record(seq, "delivered", ota_ms=10.0, t_wrap=10.0 + 0.1 * seq) for seq in range(5)]

    trend = summarize_transit_records(records)["topics"]["/x"]["ota_hop_trend_ms"]

    assert trend == {"first_third_p50": None, "last_third_p50": None, "delta": None}


def test_summarize_reports_arrival_spacing_and_bunching() -> None:
    # 12 messages sent equidistantly at 100 ms. Arrivals are on-cadence except one
    # stalled gap (300 ms) immediately followed by a bunched gap (5 ms) — the
    # head-of-line-blocking signature under test.
    gaps = [None, 100.0, 100.0, 100.0, 100.0, 100.0, 300.0, 5.0, 100.0, 100.0, 100.0, 100.0]
    records = [
        {**_record(seq, "delivered", ota_ms=20.0, t_wrap=10.0 + 0.1 * seq), "inter_arrival_ms": gaps[seq]}
        for seq in range(12)
    ]

    spacing = summarize_transit_records(records)["topics"]["/x"]["inter_arrival_ms"]

    assert spacing["nominal_period_ms"] == 100.0
    assert spacing["min"] == 5.0
    assert spacing["max"] == 300.0
    assert spacing["p50"] == 100.0
    assert spacing["p05"] == 5.0
    assert spacing["bunched_pct"] == round(100.0 / 11.0, 3)
    assert spacing["stalled_pct"] == round(100.0 / 11.0, 3)
    assert spacing["stall_then_bunch_pct"] == 100.0


def test_summarize_arrival_spacing_without_timestamps_has_no_bunching_verdict() -> None:
    records = [{**_record(seq, "delivered", ota_ms=20.0), "inter_arrival_ms": 100.0} for seq in range(4)]

    spacing = summarize_transit_records(records)["topics"]["/x"]["inter_arrival_ms"]

    assert spacing["p50"] == 100.0
    assert spacing["nominal_period_ms"] is None
    assert spacing["bunched_pct"] is None
    assert spacing["stall_then_bunch_pct"] is None
