"""RFC 0005 — benchmark genres (sweep/capacity & perturbation/recovery).

Pure, host-testable driver and verdict logic. The *runs* themselves (live ROS
graphs, emulated profiles, the nightly runner topology) are non-deterministic and
FZI-private — they live in the harness, not here (RFC 0005, "CI distribution").
What lives here is everything that can be exercised by a deterministic host test:

* size-pattern expansion (the a/b irregular-size load, mirrors ``sized_publisher``);
* the capacity binary-search driver and its oracle (``loss < p`` and ``latency < L``);
* sweep bounds + the shared-link guard (an unshaped run never saturates the LAN);
* the budget store and the regression compare (per ``(SHA, profile, genre)``);
* recovery-metric extraction from a timeline of RFC 0003 transit records;
* the coarse linear-ramp curve (monitor-only trend).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Genre 1 — load driver: a/b size pattern
# --------------------------------------------------------------------------- #


def parse_size_pattern(pattern: str) -> list[str]:
    """Parse ``"a*4,b*1"`` / ``"ax4,bx1"`` into a cyclic list of ``a``/``b`` tokens.

    Mirrors ``sized_publisher._parse_pattern`` so the host test validates the same
    generation logic the ROS node uses. An empty pattern means a single ``a`` size.
    """
    if not pattern.strip():
        return ["a"]

    tokens: list[str] = []
    for raw_token in pattern.split(","):
        token = raw_token.strip().lower()
        match = re.fullmatch(r"([ab])(?:[*x](\d+))?", token)
        if not match:
            raise ValueError(f"Invalid pattern token '{raw_token}'. Use tokens like 'a', 'b', 'ax4', or 'b*2'.")
        count = int(match.group(2) or "1")
        if count < 1:
            raise ValueError(f"Pattern token '{raw_token}' must repeat at least once.")
        tokens.extend([match.group(1)] * count)
    return tokens


def expand_size_pattern(pattern: str, size_a: int, size_b: int | None = None) -> list[int]:
    """Expand a pattern into the concrete cyclic byte-size sequence it publishes."""
    if size_a < 0:
        raise ValueError("size_a must be >= 0.")
    tokens = parse_size_pattern(pattern)
    if any(token == "b" for token in tokens) and size_b is None:
        raise ValueError("Pattern references size 'b' but size_b was not provided.")
    sizes = {"a": size_a, "b": size_b}
    return [int(sizes[token]) for token in tokens]  # type: ignore[arg-type]


def pattern_mean_bytes(pattern: str, size_a: int, size_b: int | None = None) -> float:
    """Mean payload of one pattern cycle — the basis for the offered-bandwidth bound."""
    sizes = expand_size_pattern(pattern, size_a, size_b)
    return sum(sizes) / len(sizes)


# --------------------------------------------------------------------------- #
# Genre 1 — oracle: loss < p AND latency < L over a window
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OracleThresholds:
    """Pass/fail bound for a capacity probe, read off the RFC 0003 backbone."""

    max_loss_pct: float
    max_latency_ms: float
    latency_quantile: str = "p95"


def oracle_passes(loss_pct: float, latency_ms: float | None, thresholds: OracleThresholds) -> bool:
    """A point passes iff loss and the chosen latency quantile are both under bound.

    ``latency_ms is None`` means nothing was delivered in the window — a failure.
    """
    if latency_ms is None:
        return False
    return loss_pct <= thresholds.max_loss_pct and latency_ms <= thresholds.max_latency_ms


def oracle_passes_topic(topic_summary: dict[str, Any], thresholds: OracleThresholds) -> bool:
    """Apply the oracle to one topic of ``transit.summarize_transit_records`` output."""
    loss_pct = float(topic_summary.get("loss_pct", 100.0))
    latency = (topic_summary.get("ota_hop_ms") or {}).get(thresholds.latency_quantile)
    return oracle_passes(loss_pct, None if latency is None else float(latency), thresholds)


# --------------------------------------------------------------------------- #
# Genre 1 — sweep bounds + shared-link guard
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SweepBounds:
    """Operationally-relevant ceilings; a sweep stops here, not "until it breaks".

    ``max_bandwidth_bps`` is the offered-load ceiling that keeps an unshaped / LAN
    run from saturating the shared network (RFC 0005, "Bounded, not unbounded").
    """

    max_size: int | None = None
    max_rate: float | None = None
    max_bandwidth_bps: float | None = None


def offered_bandwidth_bps(size_bytes: float, rate_hz: float) -> float:
    return size_bytes * 8.0 * rate_hz


def within_shared_link_budget(size_bytes: float, rate_hz: float, bounds: SweepBounds) -> bool:
    if bounds.max_bandwidth_bps is None:
        return True
    return offered_bandwidth_bps(size_bytes, rate_hz) <= bounds.max_bandwidth_bps


def guard_shared_link(size_bytes: float, rate_hz: float, bounds: SweepBounds, *, shared_link: bool) -> None:
    """Raise if a *shared* (unshaped/LAN) run would exceed the offered-load budget."""
    if shared_link and not within_shared_link_budget(size_bytes, rate_hz, bounds):
        raise ValueError(
            f"offered load {offered_bandwidth_bps(size_bytes, rate_hz):.0f} bps at "
            f"size={size_bytes} B, rate={rate_hz} Hz would saturate the shared link "
            f"(budget {bounds.max_bandwidth_bps:.0f} bps)"
        )


def size_ceiling(bounds: SweepBounds, *, rate_hz: float) -> int | None:
    """Largest payload the sweep may probe at ``rate_hz`` under the configured bounds."""
    ceilings: list[int] = []
    if bounds.max_size is not None:
        ceilings.append(int(bounds.max_size))
    if bounds.max_bandwidth_bps is not None and rate_hz > 0:
        ceilings.append(int(bounds.max_bandwidth_bps // (8.0 * rate_hz)))
    return min(ceilings) if ceilings else None


# --------------------------------------------------------------------------- #
# Genre 1 — capacity binary search
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CapacitySlice:
    """A capacity is a slice of (size × rate × profile); always state the slice."""

    profile: str
    knob: str  # the swept parameter, e.g. "size" or "rate"
    fixed: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class CapacityResult:
    slice: CapacitySlice
    capacity: int | None  # highest passing value, or None if even ``low`` fails


def capacity_binary_search(low: int, high: int, probe: Callable[[int], bool]) -> int | None:
    """Highest integer value in ``[low, high]`` for which ``probe`` passes.

    Assumes monotonicity (passing at smaller values) — see the RFC's "honest
    limits": real cellular's non-monotonic loss can fool a single search, so the
    live driver confirms with repeats/medians. Returns ``None`` if ``low`` fails.
    """
    if low > high:
        raise ValueError("low must be <= high")
    if not probe(low):
        return None
    best = low
    lo, hi = low + 1, high
    while lo <= hi:
        mid = (lo + hi) // 2
        if probe(mid):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def find_capacity(
    slice_: CapacitySlice,
    low: int,
    high: int,
    probe: Callable[[int], bool],
    *,
    bounds: SweepBounds | None = None,
    rate_hz: float | None = None,
) -> CapacityResult:
    """Bounded capacity search: clamp ``high`` to the sweep ceiling, then search.

    When ``bounds`` and ``rate_hz`` are given the swept ceiling never exceeds the
    offered-load budget, so a sweep on a shared link cannot be asked to saturate it.
    """
    if bounds is not None and rate_hz is not None:
        ceiling = size_ceiling(bounds, rate_hz=rate_hz)
        if ceiling is not None:
            high = min(high, ceiling)
    return CapacityResult(slice=slice_, capacity=capacity_binary_search(low, high, probe))


# --------------------------------------------------------------------------- #
# Genre 1/2 — budgets & baselines (the benchmark analogue of ``expect``)
# --------------------------------------------------------------------------- #


class Direction(str, Enum):
    HIGHER_IS_BETTER = "higher_is_better"  # capacity numbers
    LOWER_IS_BETTER = "lower_is_better"  # latency / recovery times


@dataclass(frozen=True)
class MetricSpec:
    """How one budgeted metric regresses: direction + a ± tolerance band."""

    name: str
    direction: Direction
    rel_tolerance: float = 0.0
    abs_tolerance: float = 0.0

    def tolerance(self, baseline: float) -> float:
        return max(self.abs_tolerance, abs(baseline) * self.rel_tolerance)


@dataclass(frozen=True)
class BudgetKey:
    sha: str
    profile: str
    genre: str


@dataclass(frozen=True)
class MetricComparison:
    name: str
    baseline: float
    current: float
    delta: float
    regressed: bool


@dataclass(frozen=True)
class BudgetComparison:
    key: BudgetKey
    comparisons: list[MetricComparison]

    @property
    def regressed(self) -> bool:
        return any(comparison.regressed for comparison in self.comparisons)


def compare_metric(spec: MetricSpec, baseline: float, current: float) -> MetricComparison:
    tol = spec.tolerance(baseline)
    if spec.direction is Direction.HIGHER_IS_BETTER:
        regressed = current < baseline - tol
    else:
        regressed = current > baseline + tol
    return MetricComparison(spec.name, baseline, current, round(current - baseline, 6), regressed)


def compare_to_budget(
    key: BudgetKey,
    specs: Sequence[MetricSpec],
    baseline: dict[str, float],
    current: dict[str, float],
) -> BudgetComparison:
    """Today's envelope vs the recorded baseline ± tolerance, per metric."""
    comparisons = [
        compare_metric(spec, float(baseline[spec.name]), float(current[spec.name]))
        for spec in specs
        if spec.name in baseline and spec.name in current
    ]
    return BudgetComparison(key=key, comparisons=comparisons)


@dataclass(frozen=True)
class BudgetEntry:
    key: BudgetKey
    metrics: dict[str, float]


def save_budget(path: Path, entries: Iterable[BudgetEntry]) -> None:
    """Persist budget entries (reuses the RFC 0003 forensic home — JSON lines)."""
    lines = [json.dumps({**asdict(entry.key), "metrics": entry.metrics}, sort_keys=True) for entry in entries]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def load_budget(path: Path) -> list[BudgetEntry]:
    entries: list[BudgetEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        metrics = row.pop("metrics", {})
        entries.append(BudgetEntry(key=BudgetKey(**row), metrics={str(k): float(v) for k, v in metrics.items()}))
    return entries


def find_baseline(entries: Iterable[BudgetEntry], *, profile: str, genre: str) -> BudgetEntry | None:
    """Most recent recorded entry for a ``(profile, genre)`` — the regression baseline."""
    matching = [entry for entry in entries if entry.key.profile == profile and entry.key.genre == genre]
    return matching[-1] if matching else None


# --------------------------------------------------------------------------- #
# Genre 2 — perturbation / recovery metrics
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OutageWindow:
    start: float  # seconds on the publish timeline
    end: float  # restore time


@dataclass(frozen=True)
class RecoveryMetrics:
    t_recover: float | None  # restore -> first message arriving after restore
    t_steady: float | None  # restore -> inter-arrival back within tolerance
    recovery_burst: int  # messages arriving in the burst window after restore
    lost_during_outage: dict[str, int]  # per topic
    latched_rearrival: dict[str, bool]  # per latched topic


def _send_time(record: dict[str, Any], *, seq0: int, t0: float, period_s: float, time_field: str) -> float:
    """Position on the publish timeline; reconstruct lost messages from the schedule.

    Delivered records carry ``t_wrap`` (publish time). Lost records carry no
    timestamp, so their position is the nominal send time ``t0 + (seq - seq0)·T``.
    """
    stamp = record.get(time_field)
    if stamp is not None:
        return float(stamp)
    return t0 + (int(record["seq"]) - seq0) * period_s


def _arrival_time(send_t: float, record: dict[str, Any]) -> float:
    sections = record.get("sections") or {}
    ota = sections.get("ota_hop_ms")
    if ota is None:
        ota = sections.get("ota_hop_uncorrected_ms")
    return send_t + (float(ota) / 1000.0 if ota is not None else 0.0)


def recovery_metrics(
    records: Iterable[dict[str, Any]],
    outage: OutageWindow,
    *,
    nominal_period_s: float,
    time_field: str = "t_wrap",
    steady_factor: float = 1.5,
    burst_window_s: float = 1.0,
    latched_topics: Iterable[str] = (),
) -> RecoveryMetrics:
    """Extract recovery dynamics from a timeline of RFC 0003 transit records.

    ``t_recover`` is the delay from restore to the first message arriving at/after
    restore; ``recovery_burst`` counts arrivals within ``burst_window_s`` of restore
    (the reconnect burst the baseline calls "the part that actually hurts");
    ``t_steady`` is the delay until inter-arrival returns within
    ``steady_factor × nominal_period``; ``lost_during_outage`` counts per-topic lost
    messages whose send time falls in the outage; ``latched_rearrival`` reports
    whether each latched topic delivered a value again after restore.
    """
    records = list(records)
    if not records:
        return RecoveryMetrics(None, None, 0, {}, {topic: False for topic in latched_topics})

    stamped = [record for record in records if record.get(time_field) is not None]
    anchor = min(stamped or records, key=lambda record: int(record["seq"]))
    seq0 = int(anchor["seq"])
    t0 = float(anchor.get(time_field) or 0.0)

    lost_during_outage: dict[str, int] = {}
    delivered: list[tuple[float, dict[str, Any]]] = []
    for record in records:
        send_t = _send_time(record, seq0=seq0, t0=t0, period_s=nominal_period_s, time_field=time_field)
        topic = str(record.get("topic") or "")
        if record.get("status") == "lost":
            if outage.start <= send_t <= outage.end:
                lost_during_outage[topic] = lost_during_outage.get(topic, 0) + 1
        else:
            delivered.append((_arrival_time(send_t, record), record))

    delivered.sort(key=lambda item: item[0])
    after = [arrival for arrival, _ in delivered if arrival >= outage.end]

    t_recover = after[0] - outage.end if after else None
    recovery_burst = sum(1 for arrival in after if arrival <= outage.end + burst_window_s)

    # Steady state is inter-arrival back *near* the nominal period — a symmetric band.
    # A reconnect burst arrives in a clump (gaps far below nominal), so it is not steady.
    t_steady: float | None = None
    low_gap = nominal_period_s / steady_factor
    high_gap = nominal_period_s * steady_factor
    for index in range(1, len(after)):
        if low_gap <= after[index] - after[index - 1] <= high_gap:
            t_steady = after[index] - outage.end
            break

    latched_rearrival = {
        topic: any(record.get("topic") == topic and arrival >= outage.end for arrival, record in delivered)
        for topic in latched_topics
    }
    return RecoveryMetrics(t_recover, t_steady, recovery_burst, lost_during_outage, latched_rearrival)


# --------------------------------------------------------------------------- #
# Genre 1 — coarse linear ramp (monitor-only trend)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RampPoint:
    value: float
    metric: float


def linear_ramp(values: Iterable[float], measure: Callable[[float], float]) -> list[RampPoint]:
    """The whole response curve (latency-vs-load). Monitor-only: trended, never gated."""
    return [RampPoint(float(value), float(measure(value))) for value in values]
