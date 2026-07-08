"""RFC 0007 §4 — the benched set: the curated registry of gated benchmark rows.

The registry is a whitelist, not a crawler: every row is one deliberate
``(rmw × profile × load × metric set)`` combination with an operator-visible
reason to exist. Workflows and the ``benchmark`` CLI *read* this registry —
adding an RMW variant or profile extends the matrix without editing CI, and a
matrix nobody can afford to keep green is a monitor with extra steps.

This module is pure and host-testable: loading, structural validation, and the
machine-readable per-run verdict document. Cross-file checks (profiles exist,
bands committed, workflows consume the registry) live in the contract tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

REGISTRY_SCHEMA = 1
REGISTRY_RESOURCE = "resources/benched-set.yaml"

LANES = ("merge-gate", "nightly")
GENRES = ("probe", "capacity")

# The RFC 0003 metrics each genre can put under a band today. The metric policy
# (RFC 0007 §3) prefers bottleneck-dominated metrics for tight public bands;
# latency percentiles on shared runners are monitor-only unless calibration
# proves otherwise.
GENRE_METRICS: dict[str, tuple[str, ...]] = {
    "probe": ("completeness_pct", "loss_pct", "latency_p50_ms", "latency_p95_ms", "payload_bandwidth_bps"),
    "capacity": ("capacity_size", "capacity_rate", "capacity_bandwidth"),
}

PROBE_LOAD_KEYS = frozenset(
    {"size", "size_pattern", "rate_hz", "streams", "interval_jitter_ms", "interval_jitter_seed"}
)
CAPACITY_KNOBS = ("size", "rate", "bandwidth")

# A gated window must span >=2 CycloneDDS SPDP discovery periods (2 x 30 s,
# RFC 0005 "discovery traffic is real load") or explicitly annotate why not.
MIN_GATED_WINDOW_S = 60.0

VERDICT_SCHEMA = 1


class RegistryError(ValueError):
    """The registry refused to load; the message names the offending row/field."""


@dataclass(frozen=True)
class GateRow:
    """One benched row: what runs, under what, and which metrics gate."""

    id: str
    lane: str
    reason: str
    rmw: str
    genre: str
    profile: str
    duration_s: float
    metrics: tuple[str, ...]  # banded — these gate
    monitor: tuple[str, ...] = ()  # recorded in the verdict, never gated
    load: dict[str, Any] = field(default_factory=dict)  # probe load parameters
    search: dict[str, Any] = field(default_factory=dict)  # capacity binary-search bounds
    oracle: dict[str, Any] = field(default_factory=dict)  # capacity pass/fail thresholds
    replay: dict[str, Any] = field(default_factory=dict)  # replay provenance/expect fragment for replay-derived rows
    floors: dict[str, float] = field(default_factory=dict)  # committed minimum half-widths per gated metric
    repeats: int = 1  # in-run repeats (median vote / attempts)
    window_note: str = ""  # why a sub-60 s window is still a valid gate


def packaged_registry_path() -> Path:
    """The committed registry (packaged with the wheel, same file as in-repo)."""
    return Path(str(resources.files("rosotacom").joinpath(REGISTRY_RESOURCE)))


def _require_str(row_id: str, raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"row {row_id!r}: {key!r} must be a non-empty string")
    return value.strip()


def _string_tuple(row_id: str, raw: dict[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise RegistryError(f"row {row_id!r}: {key!r} must be a list of metric names")
    return tuple(item.strip() for item in value)


_ROW_KEYS = frozenset(
    {
        "id",
        "lane",
        "reason",
        "rmw",
        "genre",
        "profile",
        "duration_s",
        "metrics",
        "monitor",
        "load",
        "search",
        "oracle",
        "replay",
        "floors",
        "repeats",
        "window_note",
    }
)


def _parse_row(raw: Any) -> GateRow:
    if not isinstance(raw, dict):
        raise RegistryError(f"registry rows must be mappings, got {type(raw).__name__}")
    row_id = str(raw.get("id") or "").strip()
    if not row_id:
        raise RegistryError("every registry row needs an 'id'")
    unknown = sorted(set(raw) - _ROW_KEYS)
    if unknown:
        raise RegistryError(f"row {row_id!r}: unknown keys {unknown} — the registry is a whitelist, extend it in code")

    lane = _require_str(row_id, raw, "lane")
    if lane not in LANES:
        raise RegistryError(f"row {row_id!r}: lane {lane!r} is not one of {list(LANES)}")
    genre = _require_str(row_id, raw, "genre")
    if genre not in GENRES:
        raise RegistryError(f"row {row_id!r}: genre {genre!r} is not one of {list(GENRES)}")
    reason = _require_str(row_id, raw, "reason")

    duration_raw = raw.get("duration_s")
    if not isinstance(duration_raw, int | float) or duration_raw <= 0:
        raise RegistryError(f"row {row_id!r}: 'duration_s' must be a positive number")
    duration_s = float(duration_raw)

    metrics = _string_tuple(row_id, raw, "metrics")
    if not metrics:
        raise RegistryError(f"row {row_id!r}: a gated row needs at least one banded metric")
    monitor = _string_tuple(row_id, raw, "monitor")
    producible = set(GENRE_METRICS[genre])
    for name, values in (("metrics", metrics), ("monitor", monitor)):
        unknown_metrics = sorted(set(values) - producible)
        if unknown_metrics:
            raise RegistryError(
                f"row {row_id!r}: {name} {unknown_metrics} are not produced by genre {genre!r} "
                f"(known: {sorted(producible)})"
            )
    overlap = sorted(set(metrics) & set(monitor))
    if overlap:
        raise RegistryError(f"row {row_id!r}: {overlap} cannot be both gated and monitor-only")

    load = raw.get("load") or {}
    search = raw.get("search") or {}
    oracle = raw.get("oracle") or {}
    replay = raw.get("replay") or {}
    floors = raw.get("floors") or {}
    for name, value in (
        ("load", load),
        ("search", search),
        ("oracle", oracle),
        ("replay", replay),
        ("floors", floors),
    ):
        if not isinstance(value, dict):
            raise RegistryError(f"row {row_id!r}: {name!r} must be a mapping")
    for metric, floor in floors.items():
        if metric not in metrics:
            raise RegistryError(f"row {row_id!r}: floor for {metric!r} but only gated metrics take committed floors")
        if not isinstance(floor, int | float) or floor <= 0:
            raise RegistryError(f"row {row_id!r}: floor for {metric!r} must be a positive number")

    repeats_raw = raw.get("repeats", 1)
    if not isinstance(repeats_raw, int) or repeats_raw < 1:
        raise RegistryError(f"row {row_id!r}: 'repeats' must be an integer >= 1")

    window_note = str(raw.get("window_note") or "").strip()
    if duration_s < MIN_GATED_WINDOW_S and not window_note:
        raise RegistryError(
            f"row {row_id!r}: window {duration_s:g}s is shorter than {MIN_GATED_WINDOW_S:g}s "
            "(2 discovery periods, RFC 0007 §3) — lengthen it or add a 'window_note' saying why it still gates"
        )

    row = GateRow(
        id=row_id,
        lane=lane,
        reason=reason,
        rmw=_require_str(row_id, raw, "rmw"),
        genre=genre,
        profile=_require_str(row_id, raw, "profile"),
        duration_s=duration_s,
        metrics=metrics,
        monitor=monitor,
        load=dict(load),
        search=dict(search),
        oracle=dict(oracle),
        replay=dict(replay),
        floors={str(metric): float(floor) for metric, floor in floors.items()},
        repeats=repeats_raw,
        window_note=window_note,
    )
    _validate_genre_parameters(row)
    return row


def _validate_genre_parameters(row: GateRow) -> None:
    if row.genre == "probe":
        if row.search or row.oracle:
            raise RegistryError(f"row {row.id!r}: 'search'/'oracle' belong to capacity rows")
        unknown = sorted(set(row.load) - PROBE_LOAD_KEYS)
        if unknown:
            raise RegistryError(f"row {row.id!r}: unknown probe load keys {unknown} (known: {sorted(PROBE_LOAD_KEYS)})")
        if ("size" in row.load) == ("size_pattern" in row.load):
            raise RegistryError(f"row {row.id!r}: a probe load needs exactly one of 'size' or 'size_pattern'")
        if float(row.load.get("rate_hz") or 0.0) <= 0.0:
            raise RegistryError(f"row {row.id!r}: probe load needs a positive 'rate_hz'")
        # Seed policy is explicit (vision): a seeded-random load must commit its seed.
        if float(row.load.get("interval_jitter_ms") or 0.0) > 0.0 and "interval_jitter_seed" not in row.load:
            raise RegistryError(
                f"row {row.id!r}: interval jitter without 'interval_jitter_seed' — gated rows commit their seeds"
            )
        _validate_replay_metadata(row)
        return

    # capacity
    if row.replay:
        raise RegistryError(f"row {row.id!r}: replay metadata belongs to probe rows")
    if row.load:
        raise RegistryError(f"row {row.id!r}: capacity rows configure 'search'/'oracle', not 'load'")
    knob = str(row.search.get("knob") or "")
    if knob not in CAPACITY_KNOBS:
        raise RegistryError(f"row {row.id!r}: search knob {knob!r} is not one of {list(CAPACITY_KNOBS)}")
    low, high = row.search.get("low"), row.search.get("high")
    if not isinstance(low, int) or not isinstance(high, int) or not 0 < low <= high:
        raise RegistryError(f"row {row.id!r}: search needs integers 0 < low <= high")
    if float(row.search.get("rate_hz") or 0.0) <= 0.0:
        raise RegistryError(f"row {row.id!r}: search needs a positive 'rate_hz'")
    for key in ("max_loss_pct", "max_latency_ms"):
        if not isinstance(row.oracle.get(key), int | float):
            raise RegistryError(f"row {row.id!r}: oracle needs numeric {key!r}")
    expected_metric = f"capacity_{knob}"
    if set(row.metrics) != {expected_metric}:
        raise RegistryError(
            f"row {row.id!r}: a capacity row bands exactly [{expected_metric!r}] (the breakpoint of its knob)"
        )


def _validate_replay_metadata(row: GateRow) -> None:
    if not row.replay:
        return
    required = {
        "source",
        "public_example",
        "bag_topic",
        "native_count",
        "native_duration_s",
        "native_hz",
        "size_basis",
        "expect",
    }
    missing = sorted(required - set(row.replay))
    if missing:
        raise RegistryError(f"row {row.id!r}: replay metadata missing {missing}")
    expect = row.replay.get("expect")
    if not isinstance(expect, dict):
        raise RegistryError(f"row {row.id!r}: replay.expect must be a mapping")
    for key in ("min_count", "completeness_min_ratio", "vs_bag_ratio"):
        if key not in expect:
            raise RegistryError(f"row {row.id!r}: replay.expect missing {key!r}")
    if int(expect["min_count"]) < 1:
        raise RegistryError(f"row {row.id!r}: replay.expect.min_count must be positive")
    for key in ("completeness_min_ratio", "vs_bag_ratio"):
        value = float(expect[key])
        if not 0.0 < value <= 1.0:
            raise RegistryError(f"row {row.id!r}: replay.expect.{key} must be in (0, 1]")


def load_registry(path: Path | None = None) -> list[GateRow]:
    """Load and structurally validate the benched set. Refusals name the row."""
    registry_path = path if path is not None else packaged_registry_path()
    doc = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise RegistryError(f"{registry_path}: registry must be a mapping")
    if doc.get("version") != REGISTRY_SCHEMA:
        raise RegistryError(f"{registry_path}: registry version {doc.get('version')!r} != {REGISTRY_SCHEMA}")
    raw_rows = doc.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise RegistryError(f"{registry_path}: registry needs a non-empty 'rows' list")
    rows = [_parse_row(raw) for raw in raw_rows]
    seen: set[str] = set()
    for row in rows:
        if row.id in seen:
            raise RegistryError(f"duplicate row id {row.id!r}; row ids key committed bands")
        seen.add(row.id)
    return rows


def rows_for_lane(rows: list[GateRow], lane: str | None) -> list[GateRow]:
    if lane is None:
        return list(rows)
    if lane not in LANES:
        raise RegistryError(f"lane {lane!r} is not one of {list(LANES)}")
    return [row for row in rows if row.lane == lane]


def find_row(rows: list[GateRow], row_id: str) -> GateRow:
    for row in rows:
        if row.id == row_id:
            return row
    known = ", ".join(row.id for row in rows)
    raise RegistryError(f"no benched row {row_id!r} in the registry (rows: {known})")


def verdict_document(
    row: GateRow,
    *,
    verdict: str,
    exit_code: int,
    gate: bool,
    sha: str,
    fingerprint: str,
    created_at: str,
    metrics: dict[str, float],
    monitor_metrics: dict[str, float],
    bands: dict[str, dict[str, Any]],
    result_path: str,
    refusal: str | None = None,
    ratchet_command: str | None = None,
) -> dict[str, Any]:
    """The machine-readable per-run verdict a downstream gate can consume."""
    return {
        "schema": VERDICT_SCHEMA,
        "kind": "benchmark-gate-verdict",
        "row": row.id,
        "lane": row.lane,
        "rmw": row.rmw,
        "genre": row.genre,
        "profile": row.profile,
        "verdict": verdict,
        "exit_code": exit_code,
        "gate": gate,
        "sha": sha,
        "fingerprint": fingerprint,
        "created_at": created_at,
        "metrics": metrics,
        "monitor_metrics": monitor_metrics,
        "bands": bands,
        "result": result_path,
        "refusal": refusal,
        "ratchet_command": ratchet_command,
    }


def write_verdict(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def summarize_verdicts(
    expected_rows: list[GateRow],
    verdicts: dict[str, dict[str, Any]],
    *,
    run: dict[str, Any],
) -> dict[str, Any]:
    """Aggregate per-row verdicts into the one document a promotion gate reads.

    Failure semantics (RFC 0007 §4): a missing verdict is a setup failure and
    red; ``REGRESSED`` is red; ``IMPROVED`` is red too — the fix is the ratchet
    command recorded in the row's verdict, not a revert.
    """
    rows_out: list[dict[str, Any]] = []
    red: list[str] = []
    for row in expected_rows:
        doc = verdicts.get(row.id)
        if doc is None:
            rows_out.append({"row": row.id, "verdict": "MISSING", "exit_code": None, "gate": True})
            red.append(row.id)
            continue
        rows_out.append(doc)
        gate = bool(doc.get("gate", True))
        if gate and (doc.get("verdict") != "WITHIN" or doc.get("exit_code") != 0):
            red.append(row.id)
    return {
        "schema": VERDICT_SCHEMA,
        "kind": "benchmark-gate-summary",
        "run": run,
        "overall": "red" if red else "green",
        "red_rows": red,
        "rows": rows_out,
    }
