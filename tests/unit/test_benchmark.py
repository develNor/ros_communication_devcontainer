"""RFC 0005 + RFC 0007 validation — host tests for the benchmark genre and band logic."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from rosotacom.benchmark import (
    Band,
    BandError,
    BandProvenance,
    Better,
    CapacitySlice,
    FingerprintMismatch,
    OracleThresholds,
    OutageWindow,
    SweepBounds,
    Verdict,
    WideningRefused,
    band_verdict,
    capacity_binary_search,
    characterize_probe_records,
    compare_to_band,
    default_better,
    exclude_probe_warmup,
    expand_size_pattern,
    find_band,
    find_capacity,
    find_probe_onset,
    guard_shared_link,
    linear_ramp,
    load_bands,
    metrics_from_result,
    offered_bandwidth_bps,
    oracle_passes,
    oracle_passes_topic,
    parse_payload_size_bytes,
    parse_size_pattern,
    parse_size_pattern_load,
    pattern_mean_bytes,
    ratchet_band,
    recovery_metrics,
    result_row_id,
    runner_fingerprint,
    save_bands,
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


def test_human_size_pattern_load_maps_to_a_b_publisher_params() -> None:
    assert parse_payload_size_bytes("20KB") == 20_000
    assert parse_payload_size_bytes("20KiB") == 20 * 1024

    load = parse_size_pattern_load("1x20KB+1x0KB")
    assert load == {
        "size_a": 20_000,
        "size_b": 0,
        "pattern": "a*1,b*1",
        "size_pattern": "1x20KB+1x0KB",
        "sizes": [20_000, 0],
    }
    assert pattern_mean_bytes(load["pattern"], load["size_a"], load["size_b"]) == 10_000


def test_size_pattern_rejects_unset_b_and_bad_tokens() -> None:
    with pytest.raises(ValueError):
        expand_size_pattern("a,b", size_a=10)  # size_b missing
    with pytest.raises(ValueError):
        parse_size_pattern("a*0")


def test_multi_element_pattern_expansion() -> None:
    load = parse_size_pattern_load("1x43KB+1x3KB+3x4KB")
    assert load["sizes"] == [43_000, 3_000, 4_000, 4_000, 4_000]
    assert load["size_pattern"] == "1x43KB+1x3KB+3x4KB"
    assert load["size_a"] == 43_000
    assert load["size_b"] == 3_000
    assert load["size_c"] == 4_000
    assert load["pattern"] == "a*1,b*1,c*3"

    assert expand_size_pattern("a*1,b*1,c*3", size_a=43_000, size_b=3_000, c=4_000) == [
        43_000,
        3_000,
        4_000,
        4_000,
        4_000,
    ]
    assert pattern_mean_bytes("a*1,b*1,c*3", size_a=43_000, size_b=3_000, c=4_000) == 11_600.0


def test_seeded_jitter_determinism() -> None:
    import random

    def generate_intervals(rate_hz: float, jitter_ms: float, seed: int, count: int) -> list[float]:
        nominal_period = 1.0 / rate_hz
        jitter_s = jitter_ms / 1000.0
        rng = random.Random(seed)
        intervals = []
        for _ in range(count):
            noise = rng.gauss(0.0, jitter_s)
            intervals.append(max(0.001, nominal_period + noise))
        return intervals

    # Same seed -> same schedule
    seq1 = generate_intervals(10.0, 20.0, 42, 100)
    seq2 = generate_intervals(10.0, 20.0, 42, 100)
    assert seq1 == seq2

    # Different seed -> different schedule
    seq3 = generate_intervals(10.0, 20.0, 43, 100)
    assert seq1 != seq3


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


# --- fixed probe characterization ------------------------------------------ #


def test_characterize_probe_records_bins_loss_latency_hz_and_bandwidth() -> None:
    records = [
        {
            "kind": "transit",
            "source": "a",
            "target": "b",
            "topic": "/x",
            "seq": 0,
            "status": "delivered",
            "t_wrap": 10.0,
            "sections": {"ota_hop_ms": 10.0},
            "size_bytes": 100,
            "jitter_ms": 1.0,
            "inter_arrival_ms": None,
        },
        {
            "kind": "transit",
            "source": "a",
            "target": "b",
            "topic": "/x",
            "seq": 1,
            "status": "lost",
            "t_wrap": None,
            "sections": {"ota_hop_ms": None},
            "size_bytes": None,
            "jitter_ms": None,
            "inter_arrival_ms": None,
        },
        {
            "kind": "transit",
            "source": "a",
            "target": "b",
            "topic": "/x",
            "seq": 2,
            "status": "delivered",
            "t_wrap": 11.0,
            "sections": {"ota_hop_ms": 20.0},
            "size_bytes": 100,
            "jitter_ms": 2.0,
            "inter_arrival_ms": 500.0,
        },
        {
            "kind": "transit",
            "source": "a",
            "target": "b",
            "topic": "/x",
            "seq": 3,
            "status": "delivered",
            "t_wrap": 11.5,
            "sections": {"ota_hop_ms": 30.0},
            "size_bytes": 200,
            "jitter_ms": 3.0,
            "inter_arrival_ms": 500.0,
        },
    ]

    bins = characterize_probe_records(records, bin_s=1.0, nominal_period_s=0.5)

    assert bins == [
        {
            "topic": "a->b:/x",
            "bin_start_s": 0.0,
            "bin_end_s": 1.0,
            "expected": 2,
            "delivered": 1,
            "lost": 1,
            "loss_pct": 50.0,
            "delivered_hz": 1.0,
            "expected_hz": 2.0,
            "payload_bandwidth_bps": 800.0,
            "mean_size_bytes": 100.0,
            "latency_p50_ms": 10.0,
            "latency_p95_ms": 10.0,
            "jitter_p50_ms": 1.0,
            "jitter_p95_ms": 1.0,
            "inter_arrival_p50_ms": None,
            "inter_arrival_p95_ms": None,
        },
        {
            "topic": "a->b:/x",
            "bin_start_s": 1.0,
            "bin_end_s": 2.0,
            "expected": 2,
            "delivered": 2,
            "lost": 0,
            "loss_pct": 0.0,
            "delivered_hz": 2.0,
            "expected_hz": 2.0,
            "payload_bandwidth_bps": 2400.0,
            "mean_size_bytes": 150.0,
            "latency_p50_ms": 20.0,
            "latency_p95_ms": 30.0,
            "jitter_p50_ms": 2.0,
            "jitter_p95_ms": 3.0,
            "inter_arrival_p50_ms": 500.0,
            "inter_arrival_p95_ms": 500.0,
        },
    ]


# --- fixed-probe settling / warm-up exclusion ------------------------------ #


def test_find_probe_onset_excludes_warmup_and_partial() -> None:
    # 20 un-impaired warm-up samples, one partial packet as shaping engages, then
    # the impaired plateau. Onset is the first mature packet; the partial drops.
    warmup = [5.0, 4.5, 5.5, 4.8, 5.2, 6.0, 4.9, 5.1, 5.3, 4.7] * 2
    settled = [80.0, 78.0, 84.0, 77.0, 88.0, 81.0, 79.0, 86.0, 74.0, 83.0] * 3
    samples = [*warmup, 21.0, *settled]

    assert find_probe_onset(samples) == len(warmup) + 1


def test_find_probe_onset_skips_a_startup_drop() -> None:
    # The packet in flight as the qdisc changes is partial (46) and the next is
    # dropped (None). Onset lands on the first mature packet after both, so the
    # startup drop is excluded and never counts as loss.
    warmup = [5.0, 4.5, 5.5, 4.8, 5.2, 6.0, 4.9, 5.1, 5.3, 4.7] * 2
    settled = [88.0, 86.0, 85.0, 91.0, 85.0, 90.0, 87.0, 84.0, 89.0, 86.0] * 3
    samples = [*warmup, 46.0, None, *settled]

    assert find_probe_onset(samples) == len(warmup) + 2  # past the partial and the drop


def test_find_probe_onset_keeps_ramping_regime_whole() -> None:
    # Bufferbloat: warm-up floor, one partial packet, then a rising ramp with no
    # plateau. Onset is the first ramp packet; the whole ramp is kept.
    warmup = [5.0, 4.5, 5.5, 4.8, 5.2, 6.0, 4.9, 5.1, 5.3, 4.7] * 2
    ramp = [96.0 + 6.0 * i for i in range(60)]
    samples = [*warmup, 48.0, *ramp]

    assert find_probe_onset(samples) == len(warmup) + 1


def test_find_probe_onset_none_without_a_clear_step() -> None:
    # Impairment live from the first packet, and a smooth ramp with no warm-up:
    # no sharp low→high step, so nothing is excluded.
    assert find_probe_onset([80.0, 78.0, 84.0, 77.0, 88.0, 81.0, 79.0, 86.0, 74.0, 83.0] * 3) is None
    assert find_probe_onset([96.0 + 4.0 * i for i in range(80)]) is None


def test_find_probe_onset_ignores_short_series() -> None:
    assert find_probe_onset([5.0, 5.0, 80.0, 80.0]) is None


def _probe_stream(latencies: list[float | None], *, period_s: float = 0.05, t0: float = 100.0) -> list[dict]:
    """Build joined transit records at a fixed rate; ``None`` latency means a lost packet."""
    records: list[dict] = []
    for seq, latency in enumerate(latencies):
        record = {
            "kind": "transit",
            "source": "a",
            "target": "b",
            "topic": "/x",
            "seq": seq,
            "t_wrap": t0 + seq * period_s,
        }
        if latency is None:
            record["status"] = "lost"
        else:
            record.update(status="delivered", sections={"ota_hop_ms": latency}, size_bytes=100)
        records.append(record)
    return records


def test_exclude_probe_warmup_drops_startup_incl_drop_and_reports_onset() -> None:
    warmup = [5.0] * 20
    settled = [80.0] * 40
    # A partial packet (21) and a startup drop (None) sit between warm-up and the
    # impaired plateau; both must be excluded so the kept window carries no loss.
    records = _probe_stream([*warmup, 21.0, None, *settled])

    kept, onset = exclude_probe_warmup(records, nominal_period_s=0.05)

    assert len(kept) == len(settled)
    assert {record["sections"]["ota_hop_ms"] for record in kept} == {80.0}
    assert not any(record["status"] == "lost" for record in kept)  # startup drop excluded
    # Onset is the send time of the first mature packet (seq 22).
    assert onset == pytest.approx(100.0 + 22 * 0.05)


def test_exclude_probe_warmup_keeps_all_without_a_step() -> None:
    records = _probe_stream([80.0] * 40)

    kept, onset = exclude_probe_warmup(records, nominal_period_s=0.05)

    assert len(kept) == len(records)
    assert onset is None


def test_characterize_probe_records_excludes_warmup_and_reanchors_to_onset() -> None:
    warmup = [5.0] * 20
    settled = [80.0] * 60  # 3 s of settled traffic at 20 Hz
    records = _probe_stream([*warmup, 21.0, *settled])

    excluded = characterize_probe_records(records, bin_s=1.0, nominal_period_s=0.05)
    included = characterize_probe_records(records, bin_s=1.0, nominal_period_s=0.05, exclude_warmup=False)

    # Re-anchored: the impaired regime starts at bin 0, and no bin carries the
    # warm-up latency floor.
    assert excluded[0]["bin_start_s"] == 0.0
    assert all(row["latency_p50_ms"] > 50.0 for row in excluded)
    # Without exclusion the warm-up is binned from the first publish and shows up
    # as an extra low-latency bin at the front.
    assert len(included) > len(excluded)
    assert included[0]["latency_p50_ms"] < 10.0


# --- committed bands: two-sided verdicts + the ratchet (RFC 0007) ----------- #

CI_RUNNER = "github-hosted-linux-x86_64"


def _provenance(**overrides: object) -> BandProvenance:
    base: dict[str, object] = {
        "fingerprint": CI_RUNNER,
        "window_s": 60.0,
        "repeats": 5,
        "sigma": 100.0,
        "floor": 50.0,
        "k": 3.0,
        "source_sha": "abc123",
        "ratcheted_at": "2026-07-08T12:00:00",
        "note": "initial calibration",
    }
    base.update(overrides)
    return BandProvenance(**base)  # type: ignore[arg-type]


def _band(
    lo: float = 4_700.0,
    hi: float = 5_300.0,
    better: Better = Better.HIGHER,
    metric: str = "capacity_size",
    **prov_overrides: object,
) -> Band:
    return Band(
        row="capacity-size",
        profile="rate-limited-capacity-ci",
        metric=metric,
        lo=lo,
        hi=hi,
        better=better,
        provenance=_provenance(**prov_overrides),
    )


def test_band_verdicts_are_two_sided_for_both_better_directions() -> None:
    higher = _band()  # capacity: higher is better, band [4700, 5300]
    assert band_verdict(higher, 5_000.0) is Verdict.WITHIN
    assert band_verdict(higher, 4_700.0) is Verdict.WITHIN  # closed interval
    assert band_verdict(higher, 5_300.0) is Verdict.WITHIN
    assert band_verdict(higher, 4_600.0) is Verdict.REGRESSED
    assert band_verdict(higher, 5_400.0) is Verdict.IMPROVED

    lower = _band(lo=1.0, hi=2.0, better=Better.LOWER, metric="t_recover_s")
    assert band_verdict(lower, 1.5) is Verdict.WITHIN
    assert band_verdict(lower, 2.5) is Verdict.REGRESSED  # slower recovery = worse
    assert band_verdict(lower, 0.5) is Verdict.IMPROVED


def test_compare_refuses_cross_runner_fingerprints_with_recalibration_instruction() -> None:
    band = _band()
    with pytest.raises(FingerprintMismatch, match="--recalibrate"):
        compare_to_band(band, 5_000.0, fingerprint="host-laptop-linux-x86_64")
    comparison = compare_to_band(band, 5_000.0, fingerprint=CI_RUNNER)
    assert comparison.verdict is Verdict.WITHIN


def test_band_store_roundtrip_preserves_provenance_and_rejects_v1(tmp_path: Path) -> None:
    path = tmp_path / "budgets.jsonl"
    bands = [
        _band(),
        _band(lo=1.0, hi=2.0, better=Better.LOWER, metric="t_recover_s", note="recovery calibration"),
    ]
    save_bands(path, bands)
    loaded = load_bands(path)
    assert loaded == sorted(bands, key=lambda band: band.key)
    assert find_band(loaded, row="capacity-size", profile="rate-limited-capacity-ci", metric="t_recover_s")

    # A leftover v1 budget entry is refused outright — no migration shim.
    path.write_text('{"genre": "capacity", "metrics": {"capacity_size": 1.0}, "profile": "p", "sha": "x"}\n')
    with pytest.raises(BandError, match="--recalibrate"):
        load_bands(path)


def test_band_store_refuses_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "budgets.jsonl"
    with pytest.raises(BandError, match="duplicate"):
        save_bands(path, [_band(), _band(lo=1.0, hi=2.0)])


def test_ratchet_recenters_within_calibrated_width_and_preserves_provenance() -> None:
    existing = _band()  # center 5000, half-width 300
    moved = ratchet_band(
        existing,
        [5_600.0, 5_700.0, 5_500.0],
        row=existing.row,
        profile=existing.profile,
        metric=existing.metric,
        better=existing.better,
        fingerprint=CI_RUNNER,
        window_s=60.0,
        source_sha="def456",
        ratcheted_at="2026-07-09T09:00:00",
        note="gop default changed",
    )
    assert moved.center == 5_600.0  # median of the runs
    assert moved.half_width == existing.half_width  # width comes from calibration, not this run
    # Calibration provenance is preserved; move provenance is updated.
    assert moved.provenance.sigma == existing.provenance.sigma
    assert moved.provenance.floor == existing.provenance.floor
    assert moved.provenance.k == existing.provenance.k
    assert moved.provenance.repeats == existing.provenance.repeats
    assert moved.provenance.window_s == existing.provenance.window_s
    assert moved.provenance.fingerprint == CI_RUNNER
    assert moved.provenance.source_sha == "def456"
    assert moved.provenance.note == "gop default changed"


def test_ratchet_refuses_moving_toward_worse_without_recalibrate() -> None:
    higher = _band()  # [4700, 5300], higher is better
    with pytest.raises(WideningRefused, match="--recalibrate"):
        ratchet_band(
            higher,
            [4_800.0],
            row=higher.row,
            profile=higher.profile,
            metric=higher.metric,
            better=higher.better,
            fingerprint=CI_RUNNER,
            window_s=60.0,
            source_sha="bad",
            ratcheted_at="now",
        )
    lower = _band(lo=1.0, hi=2.0, better=Better.LOWER, metric="t_recover_s")
    with pytest.raises(WideningRefused, match="--recalibrate"):
        ratchet_band(
            lower,
            [2.4],
            row=lower.row,
            profile=lower.profile,
            metric=lower.metric,
            better=lower.better,
            fingerprint=CI_RUNNER,
            window_s=60.0,
            source_sha="bad",
            ratcheted_at="now",
        )


def test_ratchet_requires_an_existing_band_unless_recalibrating() -> None:
    with pytest.raises(BandError, match="--recalibrate"):
        ratchet_band(
            None,
            [5_000.0],
            row="capacity-size",
            profile="p",
            metric="capacity_size",
            better=Better.HIGHER,
            fingerprint=CI_RUNNER,
            window_s=60.0,
            source_sha="abc",
            ratcheted_at="now",
        )


def test_ratchet_refuses_runs_from_another_runner_class() -> None:
    existing = _band()
    with pytest.raises(FingerprintMismatch, match="--recalibrate"):
        ratchet_band(
            existing,
            [5_600.0],
            row=existing.row,
            profile=existing.profile,
            metric=existing.metric,
            better=existing.better,
            fingerprint="host-bench-pair-linux-x86_64",
            window_s=60.0,
            source_sha="abc",
            ratcheted_at="now",
        )


def test_recalibrate_mints_width_from_repeats_with_floor_guard() -> None:
    fresh = ratchet_band(
        None,
        [5_000.0, 5_060.0, 4_940.0],  # median 5000, sample stdev 60
        row="capacity-size",
        profile="p",
        metric="capacity_size",
        better=Better.HIGHER,
        fingerprint=CI_RUNNER,
        window_s=60.0,
        source_sha="abc",
        ratcheted_at="now",
        note="calibration",
        recalibrate=True,
        k=3.0,
        floor=0.0,
        floor_frac=0.0,
    )
    assert fresh.center == 5_000.0
    assert fresh.half_width == pytest.approx(180.0)  # 3σ
    assert fresh.provenance.sigma == pytest.approx(60.0)
    assert fresh.provenance.repeats == 3

    # One lucky run (σ = 0): the floor keeps the band from becoming a hair-trigger.
    floored = ratchet_band(
        None,
        [5_000.0],
        row="capacity-size",
        profile="p",
        metric="capacity_size",
        better=Better.HIGHER,
        fingerprint=CI_RUNNER,
        window_s=60.0,
        source_sha="abc",
        ratcheted_at="now",
        recalibrate=True,
        floor_frac=0.02,
    )
    assert floored.half_width == pytest.approx(100.0)
    assert floored.provenance.floor == pytest.approx(100.0)

    # A recalibration is the deliberate path: it may move a band toward worse.
    existing = _band()
    worse = ratchet_band(
        existing,
        [4_000.0, 4_020.0],
        row=existing.row,
        profile=existing.profile,
        metric=existing.metric,
        better=existing.better,
        fingerprint=CI_RUNNER,
        window_s=60.0,
        source_sha="abc",
        ratcheted_at="now",
        note="accepted trade-off",
        recalibrate=True,
    )
    assert worse.center == 4_010.0

    with pytest.raises(BandError, match="--floor"):
        ratchet_band(
            None,
            [0.0],
            row="r",
            profile="p",
            metric="capacity_size",
            better=Better.HIGHER,
            fingerprint=CI_RUNNER,
            window_s=60.0,
            source_sha="abc",
            ratcheted_at="now",
            recalibrate=True,
            floor=0.0,
            floor_frac=0.02,
        )


def test_metrics_and_row_derive_from_capacity_and_recovery_results() -> None:
    capacity_doc = {
        "genre": "capacity",
        "configuration": {"knob": "size", "profile": "p", "duration_s": 30.0},
        "result": {"capacity": 5_000},
    }
    assert metrics_from_result(capacity_doc) == {"capacity_size": 5_000.0}
    assert result_row_id(capacity_doc) == "capacity-size"

    recovery_doc = {
        "genre": "recovery",
        "result": {
            "t_recover": 1.5,
            "t_steady": 3.0,
            "recovery_burst": 12,
            "lost_during_outage": {"/a": 3, "/b": 2},
        },
    }
    assert metrics_from_result(recovery_doc) == {
        "t_recover_s": 1.5,
        "t_steady_s": 3.0,
        "recovery_burst": 12.0,
        "lost_during_outage_total": 5.0,
    }
    assert result_row_id(recovery_doc) == "recovery"

    with pytest.raises(BandError, match="no value to band"):
        metrics_from_result({"genre": "capacity", "configuration": {"knob": "size"}, "result": {"capacity": None}})
    with pytest.raises(BandError, match="registry"):
        metrics_from_result({"genre": "ramp"})


def test_runner_fingerprint_distinguishes_runner_classes() -> None:
    assert runner_fingerprint({"ROSOTACOM_RUNNER_CLASS": "bench-pair"}) == "bench-pair"
    hosted = runner_fingerprint({"RUNNER_ENVIRONMENT": "github-hosted"})
    assert hosted.startswith("github-hosted-")
    local = runner_fingerprint({})
    assert local.startswith("host-")
    assert hosted != local


def test_default_better_directions_cover_banded_metrics() -> None:
    assert default_better("capacity_size") is Better.HIGHER
    assert default_better("capacity_rate") is Better.HIGHER
    assert default_better("t_recover_s") is Better.LOWER
    assert default_better("lost_during_outage_total") is Better.LOWER
    with pytest.raises(BandError, match="--better"):
        default_better("mystery_metric")


def test_band_json_lines_are_stable_and_reviewable(tmp_path: Path) -> None:
    """A ratchet shows up as a minimal diff: stable ordering, stable key order."""
    path = tmp_path / "budgets.jsonl"
    bands = [
        _band(lo=1.0, hi=2.0, better=Better.LOWER, metric="t_recover_s"),
        _band(),
    ]
    save_bands(path, bands)
    first = path.read_text(encoding="utf-8")
    save_bands(path, list(reversed(load_bands(path))))
    assert path.read_text(encoding="utf-8") == first
    moved = dataclasses.replace(bands[1], lo=bands[1].lo + 100.0, hi=bands[1].hi + 100.0)
    save_bands(path, [bands[0], moved])
    second = path.read_text(encoding="utf-8")
    changed = [(a, b) for a, b in zip(first.splitlines(), second.splitlines(), strict=True) if a != b]
    assert len(changed) == 1  # exactly the ratcheted band's line moved


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
