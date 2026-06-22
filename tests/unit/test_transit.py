from __future__ import annotations

import json
from pathlib import Path

from rosotacom.transit import (
    join_transit_records,
    load_transit_records,
    summarize_transit_records,
)


def _record(seq: int, status: str, ota_ms: float | None = None) -> dict:
    return {
        "kind": "transit",
        "topic": "/x",
        "seq": seq,
        "status": status,
        "sections": {"preprocess_ms": 1.0, "ota_hop_ms": ota_ms},
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
