"""Host tests for the benched-set registry (RFC 0007 §4).

Structural validation and the verdict/summary documents; the cross-file
contracts (profiles exist, bands committed, workflows consume the registry)
live in ``tests/contract/test_benched_set_registry.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rosotacom.benched_set import (
    GateRow,
    RegistryError,
    find_row,
    load_registry,
    packaged_registry_path,
    rows_for_calibration,
    rows_for_lane,
    summarize_verdicts,
    verdict_document,
)

MINIMAL_PROBE_ROW = """
  - id: probe-x
    kind: performance
    lane: nightly
    reason: because
    rmw: cyclone
    genre: probe
    profile: gate-tight
    duration_s: 60
    load: {{ size: 12000, rate_hz: 20.0 }}
    metrics: [loss_pct]
{extra}
"""


def _write_registry(tmp_path: Path, rows_yaml: str) -> Path:
    path = tmp_path / "benched-set.yaml"
    path.write_text(f"version: 2\nrows:\n{rows_yaml}", encoding="utf-8")
    return path


def _probe_row_yaml(extra: str = "") -> str:
    return MINIMAL_PROBE_ROW.format(extra=extra)


def test_packaged_registry_loads_and_covers_both_lanes() -> None:
    rows = load_registry(packaged_registry_path())

    assert rows_for_lane(rows, "merge-gate"), "the merge gate needs at least one benched row"
    assert rows_for_lane(rows, "nightly"), "the nightly gate needs at least one benched row"
    rmws = {row.rmw for row in rows}
    assert {"cyclone", "fastdds", "zenoh"} <= rmws, "the matrix covers the supported RMW variants"
    profiles = {row.profile for row in rows if row.rmw == "cyclone"}
    assert len(profiles) >= 2, "the default RMW runs deep: nominal and tight profiles"
    assert any(row.kind == "boundary" for row in rows_for_lane(rows, "nightly")), "nightly carries boundary rows"
    assert all(row.kind == "performance" for row in rows_for_calibration(rows)), "only performance rows calibrate bands"


def test_find_row_names_known_rows_in_the_refusal() -> None:
    rows = load_registry(packaged_registry_path())
    with pytest.raises(RegistryError, match=rows[0].id):
        find_row(rows, "no-such-row")


def test_registry_refuses_duplicate_ids(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, _probe_row_yaml() + _probe_row_yaml())
    with pytest.raises(RegistryError, match="duplicate row id"):
        load_registry(path)


def test_registry_refuses_unknown_keys(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, _probe_row_yaml(extra="    surprise: 1\n"))
    with pytest.raises(RegistryError, match="whitelist"):
        load_registry(path)


def test_registry_requires_explicit_row_kind(tmp_path: Path) -> None:
    row = _probe_row_yaml().replace("    kind: performance\n", "")
    with pytest.raises(RegistryError, match="kind"):
        load_registry(_write_registry(tmp_path, row))


def test_registry_refuses_short_window_without_note(tmp_path: Path) -> None:
    row = _probe_row_yaml().replace("duration_s: 60", "duration_s: 10")
    with pytest.raises(RegistryError, match="window_note"):
        load_registry(_write_registry(tmp_path, row))


def test_registry_requires_committed_jitter_seed(tmp_path: Path) -> None:
    row = _probe_row_yaml().replace(
        "load: { size: 12000, rate_hz: 20.0 }",
        "load: { size: 12000, rate_hz: 20.0, interval_jitter_ms: 20.0 }",
    )
    with pytest.raises(RegistryError, match="seed"):
        load_registry(_write_registry(tmp_path, row))


def test_registry_refuses_metrics_the_genre_cannot_produce(tmp_path: Path) -> None:
    row = _probe_row_yaml().replace("metrics: [loss_pct]", "metrics: [capacity_size]")
    with pytest.raises(RegistryError, match="not produced by genre"):
        load_registry(_write_registry(tmp_path, row))


def test_registry_refuses_gated_and_monitor_overlap(tmp_path: Path) -> None:
    row = _probe_row_yaml(extra="    monitor: [loss_pct]\n")
    with pytest.raises(RegistryError, match="both gated and monitor-only"):
        load_registry(_write_registry(tmp_path, row))


def test_registry_refuses_floors_for_unbanded_metrics(tmp_path: Path) -> None:
    row = _probe_row_yaml(extra="    floors: { latency_p95_ms: 1.0 }\n")
    with pytest.raises(RegistryError, match="floor"):
        load_registry(_write_registry(tmp_path, row))


def test_capacity_rows_band_exactly_their_knob_breakpoint(tmp_path: Path) -> None:
    row = """
  - id: cap-x
    kind: performance
    lane: nightly
    reason: because
    rmw: cyclone
    genre: capacity
    profile: gate-tight
    duration_s: 10
    window_note: point checks inside the search
    search: { knob: size, low: 1000, high: 9000, rate_hz: 20.0 }
    oracle: { max_loss_pct: 5.0, max_latency_ms: 1000.0 }
    metrics: [loss_pct]
"""
    with pytest.raises(RegistryError, match="capacity_size"):
        load_registry(_write_registry(tmp_path, row))


def test_boundary_rows_validate_their_failure_signature(tmp_path: Path) -> None:
    row = """
  - id: boundary-x
    kind: boundary
    lane: nightly
    reason: because
    rmw: cyclone
    genre: probe
    profile: finding-3.2mbit
    duration_s: 60
    load: { size: 18000, rate_hz: 20.0 }
    metrics: [loss_pct]
    boundary:
      finding: docs/findings/example.md
      bad_profile: finding-3.1mbit
      good_oracle: { loss_pct: { max: 0.5 } }
      failure_signature: { loss_pct: { min: 1.0 } }
      next_steps: move the profile and ratchet
"""
    rows = load_registry(_write_registry(tmp_path, row))

    assert rows[0].kind == "boundary"
    assert rows[0].boundary["bad_profile"] == "finding-3.1mbit"

    broken = row.replace(
        "failure_signature: { loss_pct: { min: 1.0 } }",
        "failure_signature: { latency_p95_ms: { min: 1.0 } }",
    )
    with pytest.raises(RegistryError, match="unbanded metric"):
        load_registry(_write_registry(tmp_path, broken))


def _row(row_id: str = "probe-x", lane: str = "nightly") -> GateRow:
    return GateRow(
        id=row_id,
        lane=lane,
        reason="because",
        rmw="cyclone",
        genre="probe",
        profile="gate-tight",
        duration_s=60.0,
        metrics=("loss_pct",),
        monitor=("latency_p95_ms",),
        load={"size": 12000, "rate_hz": 20.0},
    )


def _verdict(row: GateRow, verdict: str, exit_code: int, *, gate: bool = True) -> dict:
    return verdict_document(
        row,
        verdict=verdict,
        exit_code=exit_code,
        gate=gate,
        sha="abc1234",
        fingerprint="github-hosted-linux-x86_64",
        created_at="2026-07-08T12:00:00",
        metrics={"loss_pct": 48.0},
        monitor_metrics={"latency_p95_ms": 300.0},
        bands={"loss_pct": {"lo": 47.0, "hi": 49.0, "better": "lower"}},
        result_path="run/result.json",
    )


def test_summary_red_semantics_match_rfc0007() -> None:
    """Missing row = red; REGRESSED = red; IMPROVED = red; monitor rows never red."""
    row_a, row_b = _row("row-a"), _row("row-b")
    run = {"sha": "abc1234"}

    missing = summarize_verdicts([row_a, row_b], {"row-a": _verdict(row_a, "WITHIN", 0)}, run=run)
    assert missing["overall"] == "red"
    assert missing["red_rows"] == ["row-b"]

    regressed = summarize_verdicts([row_a], {"row-a": _verdict(row_a, "REGRESSED", 1)}, run=run)
    assert regressed["overall"] == "red"

    improved = summarize_verdicts([row_a], {"row-a": _verdict(row_a, "IMPROVED", 2)}, run=run)
    assert improved["overall"] == "red", "IMPROVED is red too — the fix is the ratchet, not a revert"

    monitored = summarize_verdicts([row_a], {"row-a": _verdict(row_a, "REGRESSED", 1, gate=False)}, run=run)
    assert monitored["overall"] == "green", "a monitor row reports without blocking"

    green = summarize_verdicts(
        [row_a, row_b],
        {"row-a": _verdict(row_a, "WITHIN", 0), "row-b": _verdict(row_b, "WITHIN", 0)},
        run=run,
    )
    assert green["overall"] == "green"
    assert green["red_rows"] == []
