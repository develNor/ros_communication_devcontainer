"""Offline analysis for RFC 0003 transit records."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[rank], 3)


def load_transit_records(paths: Iterable[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if event.get("kind") == "transit":
                records.append(event)
    return records


def join_transit_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join duplicate observations by ``(topic, seq)`` and keep the richest row."""
    joined: dict[tuple[str, str, int], dict[str, Any]] = {}
    status_rank = {"lost": 0, "reordered": 1, "delivered": 2}
    for record in records:
        topic = str(record.get("topic") or "")
        source = str(record.get("source") or "")
        seq = int(record["seq"])
        key = (source, topic, seq)
        current = joined.get(key)
        if current is None:
            joined[key] = dict(record)
            continue
        if status_rank.get(str(record.get("status")), -1) > status_rank.get(str(current.get("status")), -1):
            current["status"] = record.get("status")
        for field, value in record.items():
            if current.get(field) is None and value is not None:
                current[field] = value
            elif field == "sections" and isinstance(value, dict):
                sections = current.setdefault("sections", {})
                for section, section_value in value.items():
                    if sections.get(section) is None and section_value is not None:
                        sections[section] = section_value
    return [joined[key] for key in sorted(joined)]


def summarize_transit_records(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for record in join_transit_records(records):
        topic = str(record.get("topic") or "")
        source = str(record.get("source") or "")
        target = str(record.get("target") or "")
        label = f"{source}->{target}:{topic}" if source or target else topic
        by_topic.setdefault(label, []).append(record)

    topics: dict[str, Any] = {}
    for topic, topic_records in sorted(by_topic.items()):
        lost = sum(record.get("status") == "lost" for record in topic_records)
        reordered = sum(record.get("status") == "reordered" for record in topic_records)
        delivered = len(topic_records) - lost
        ota = []
        for record in topic_records:
            if isinstance(record.get("sections"), dict):
                val = record["sections"].get("ota_hop_ms")
                if val is None:
                    val = record["sections"].get("ota_hop_uncorrected_ms")
                if val is not None:
                    ota.append(float(val))
        jitter = [float(record["jitter_ms"]) for record in topic_records if record.get("jitter_ms") is not None]
        topics[topic] = {
            "expected": len(topic_records),
            "delivered": delivered,
            "lost": lost,
            "loss_pct": round(100.0 * lost / len(topic_records), 3) if topic_records else 0.0,
            "reordered": reordered,
            "ota_hop_ms": {"p50": _percentile(ota, 0.50), "p95": _percentile(ota, 0.95)},
            "jitter_ms": {"p50": _percentile(jitter, 0.50), "p95": _percentile(jitter, 0.95)},
        }
    return {"schema_version": 1, "topics": topics}
