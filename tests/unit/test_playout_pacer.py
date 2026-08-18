"""Playout pacer core — schedule properties on synthetic arrival traces."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WS_PY = REPO_ROOT / "src" / "rosotacom" / "resources" / "ws" / "ros2src" / "com_py"
sys.path.insert(0, str(WS_PY))

from com_py.playout_pacer_core import PacerConfig, PlayoutSchedule  # noqa: E402


def _run(schedule: PlayoutSchedule, stamps, delays):
    releases = []
    for stamp, d in zip(stamps, delays, strict=True):
        releases.append(schedule.on_message(stamp, stamp + d))
    return releases


def test_jittered_arrivals_release_on_a_regular_grid() -> None:
    # 10 Hz stream, delay jitter far above the frame period: released cadence
    # must be the sender cadence, not the arrival cadence.
    rng = random.Random(1)
    stamps = [i / 10 for i in range(300)]
    delays = [0.030 + rng.random() * 0.120 for _ in stamps]  # 30-150 ms
    schedule = PlayoutSchedule(PacerConfig(adaptive=True, min_ms=100.0, max_ms=800.0))
    releases = _run(schedule, stamps, delays)
    gaps = [b - a for a, b in zip(releases[50:], releases[51:], strict=False)]
    assert max(gaps) < 0.150  # no visible stutter (>150 ms) left
    assert sum(1 for g in gaps if abs(g - 0.1) < 0.01) / len(gaps) > 0.95


def test_late_message_passes_through_immediately_and_order_is_kept() -> None:
    schedule = PlayoutSchedule(PacerConfig(adaptive=False, target_ms=200.0))
    stamps = [i / 10 for i in range(50)]
    delays = [0.030] * 50
    delays[25] = 0.700  # one message far beyond any budget
    releases = _run(schedule, stamps, delays)
    # Late message is released at its arrival, not held further...
    assert releases[25] == pytest.approx(stamps[25] + 0.700)
    # ...and ordering is never violated.
    assert all(b >= a for a, b in zip(releases, releases[1:], strict=False))


def test_budget_is_clock_offset_proof() -> None:
    # Same jitter, sender clock shifted by -3 s: the budget must not explode.
    rng = random.Random(2)
    delays = [0.030 + rng.random() * 0.060 for _ in range(200)]
    plain = PlayoutSchedule()
    shifted = PlayoutSchedule()
    for i, d in enumerate(delays):
        plain.on_message(i / 10, i / 10 + d)
        shifted.on_message(i / 10 - 3.0, i / 10 + d)  # stamp 3 s in the "past"
    assert shifted.budget_ms == pytest.approx(plain.budget_ms, rel=0.05)
    assert shifted.budget_ms < 800.0


def test_adaptive_budget_tracks_jitter_and_respects_clamps() -> None:
    calm = PlayoutSchedule(PacerConfig(min_ms=100.0, max_ms=800.0, margin_ms=40.0))
    rough = PlayoutSchedule(PacerConfig(min_ms=100.0, max_ms=800.0, margin_ms=40.0))
    rng = random.Random(3)
    for i in range(300):
        calm.on_message(i / 10, i / 10 + 0.030 + rng.random() * 0.010)
        rough.on_message(i / 10, i / 10 + 0.030 + rng.random() * 0.400)
    assert calm.budget_ms == pytest.approx(100.0)  # clamped at min
    assert rough.budget_ms > 300.0
    assert rough.budget_ms <= 800.0


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        PacerConfig(percentile=1.5)
    with pytest.raises(ValueError):
        PacerConfig(min_ms=500.0, max_ms=100.0)
    with pytest.raises(ValueError):
        PacerConfig(window=3)
