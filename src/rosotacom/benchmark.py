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
* fixed-probe time-bin characterization for latency/loss/Hz/bandwidth plots;
* the coarse linear-ramp curve (monitor-only trend).
"""

from __future__ import annotations

import json
import math
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


def parse_payload_size_bytes(raw: str) -> int:
    """Parse a payload byte size with optional decimal/binary units."""
    text = raw.strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([A-Za-z]*)", text)
    if not match:
        raise ValueError(f"Invalid payload size {raw!r}. Use values like 20000, 20KB, or 20KiB.")

    value = float(match.group(1))
    unit = match.group(2).lower()
    units = {
        "": 1,
        "b": 1,
        "k": 1_000,
        "kb": 1_000,
        "m": 1_000_000,
        "mb": 1_000_000,
        "g": 1_000_000_000,
        "gb": 1_000_000_000,
        "ki": 1024,
        "kib": 1024,
        "mi": 1024 * 1024,
        "mib": 1024 * 1024,
        "gi": 1024 * 1024 * 1024,
        "gib": 1024 * 1024 * 1024,
    }
    if unit not in units:
        raise ValueError(f"Invalid payload size unit {match.group(2)!r}. Use B, KB, MB, KiB, or MiB.")
    bytes_value = value * units[unit]
    if bytes_value < 0 or not bytes_value.is_integer():
        raise ValueError(f"Payload size {raw!r} does not resolve to a whole non-negative byte count.")
    return int(bytes_value)


def _compress_size_pattern(tokens: Sequence[str]) -> str:
    compressed: list[str] = []
    current = tokens[0]
    count = 1
    for token in tokens[1:]:
        if token == current:
            count += 1
            continue
        compressed.append(f"{current}*{count}")
        current = token
        count = 1
    compressed.append(f"{current}*{count}")
    return ",".join(compressed)


def parse_size_pattern_load(pattern: str) -> dict[str, Any]:
    """Parse ``1x20KB+1x0KB`` into ``sized_publisher`` a/b load parameters.

    Pattern terms are ``COUNTxSIZE`` or just ``SIZE``, separated by ``+`` or
    ``,``. Decimal units (KB/MB/GB) use powers of 1000; binary units
    (KiB/MiB/GiB) use powers of 1024. The current ROS publisher supports up to
    two distinct payload sizes, mapped to ``size_a`` and ``size_b``.
    """
    if not pattern.strip():
        raise ValueError("size pattern must not be empty.")

    label_by_size: dict[int, str] = {}
    size_by_label: dict[str, int] = {}
    tokens: list[str] = []

    for raw_term in re.split(r"[+,]", pattern):
        term = raw_term.strip()
        if not term:
            raise ValueError(f"Invalid size pattern {pattern!r}: empty term.")
        match = re.fullmatch(r"(?:(\d+)\s*[x*]\s*)?(.+)", term)
        if not match:
            raise ValueError(f"Invalid size pattern term {raw_term!r}. Use terms like 1x20KB or 0KB.")
        count = int(match.group(1) or "1")
        if count < 1:
            raise ValueError(f"Size pattern term {raw_term!r} must repeat at least once.")
        size = parse_payload_size_bytes(match.group(2))
        label = label_by_size.get(size)
        if label is None:
            if len(label_by_size) >= 2:
                raise ValueError("size pattern supports at most two distinct payload sizes.")
            label = "a" if not label_by_size else "b"
            label_by_size[size] = label
            size_by_label[label] = size
        tokens.extend([label] * count)

    load: dict[str, Any] = {
        "size_a": size_by_label["a"],
        "pattern": _compress_size_pattern(tokens),
        "size_pattern": pattern,
    }
    if "b" in size_by_label:
        load["size_b"] = size_by_label["b"]
    return load


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
# Fixed probe — time-binned connection characterization
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProbeBin:
    topic: str
    bin_start_s: float
    bin_end_s: float
    expected: int
    delivered: int
    lost: int
    loss_pct: float
    delivered_hz: float
    expected_hz: float
    payload_bandwidth_bps: float
    mean_size_bytes: float | None
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    jitter_p50_ms: float | None
    jitter_p95_ms: float | None
    inter_arrival_p50_ms: float | None
    inter_arrival_p95_ms: float | None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[rank], 3)


def _transit_topic_label(record: dict[str, Any]) -> str:
    topic = str(record.get("topic") or "")
    source = str(record.get("source") or "")
    target = str(record.get("target") or "")
    return f"{source}->{target}:{topic}" if source or target else topic


def _section_ms(record: dict[str, Any], *names: str) -> float | None:
    sections = record.get("sections")
    if not isinstance(sections, dict):
        return None
    for name in names:
        value = sections.get(name)
        if value is not None:
            return float(value)
    return None


def _infer_nominal_period_s(records: Sequence[dict[str, Any]], *, time_field: str) -> float | None:
    candidates: list[float] = []
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        key = (
            str(record.get("source") or ""),
            str(record.get("target") or ""),
            str(record.get("topic") or ""),
        )
        grouped.setdefault(key, []).append(record)

    for stream in grouped.values():
        stamped = sorted(
            (record for record in stream if record.get(time_field) is not None),
            key=lambda record: int(record["seq"]),
        )
        for previous, current in zip(stamped, stamped[1:], strict=False):
            seq_delta = int(current["seq"]) - int(previous["seq"])
            time_delta = float(current[time_field]) - float(previous[time_field])
            if seq_delta > 0 and time_delta >= 0.0:
                candidates.append(time_delta / seq_delta)
    if not candidates:
        return None
    ordered = sorted(candidates)
    return ordered[len(ordered) // 2]


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else 0.5 * (ordered[mid - 1] + ordered[mid])


def _mad(values: Sequence[float]) -> float:
    """Median absolute deviation — a spread estimate that ignores outliers."""
    center = _median(values)
    return _median([abs(v - center) for v in values])


# A fixed-probe sample is either INCLUDED (the impaired regime under test) or
# EXCLUDED (the start-up: warm-up floor, the partial packet, and any startup drop).
INCLUDED = "included"
EXCLUDED = "excluded"


def find_probe_onset(
    samples: Sequence[float | None],
    *,
    min_points: int = 12,
    step_window: int = 6,
    min_step_ratio: float = 3.0,
    min_step_ms: float = 15.0,
    settle_fraction: float = 0.6,
    settle_window: int = 6,
) -> int | None:
    """Index in ``samples`` where the clean impaired regime begins, or ``None``.

    ``samples`` is one stream ordered by send time; each entry is a delivered
    packet's latency in ms, or ``None`` for a lost packet.

    A fixed probe records packets before the tc/netem shaping is applied (the low
    transport floor), then a brief transition where the in-flight packet sees a
    partial delay and/or is dropped as the qdisc changes, then the impaired regime
    under test (a plateau *or* a rising bufferbloat ramp). This returns the first
    index from which the stream is *cleanly* in the impaired regime — a sustained
    window whose latencies are at/above a threshold set between the floor and the
    impaired level, with no loss dragging the window down. Everything before it
    (warm-up, the immature partial packet, and any startup drop) is start-up and
    should be excluded, so plot and summary agree on a clean measurement window.

    Detection is conservative: without a clear low-floor→high step (``impaired`` at
    least ``min_step_ratio``× **or** ``min_step_ms`` above the floor) it returns
    ``None`` and nothing is excluded — e.g. impairment live from the first packet,
    a no-op profile, or a smooth ramp with no warm-up.
    """
    n = len(samples)
    delivered = [s for s in samples if s is not None]
    if len(delivered) < min_points:
        return None

    window = max(1, min(step_window, len(delivered) // 2))
    # Sharpest upward step on the delivered latencies: the largest ratio between
    # the windowed median just after and just before an index. The un-impaired
    # floor is the lowest level, so the warm-up→impaired jump dominates, and
    # windowed medians stop a single ramp spike from masquerading as the step.
    step_index: int | None = None
    best_ratio = 1.0
    for i in range(window, len(delivered) - window):
        pre = _median(delivered[i - window : i])
        post = _median(delivered[i : i + window])
        if pre <= 0.0:
            continue
        ratio = post / pre
        if ratio > best_ratio:
            best_ratio = ratio
            step_index = i
    if step_index is None:
        return None

    floor = _median(delivered[max(0, step_index - window) : step_index])
    impaired = _median(delivered[step_index : step_index + window])
    step_is_real = impaired >= floor * min_step_ratio or impaired - floor >= min_step_ms
    if best_ratio < min_step_ratio or not step_is_real:
        return None

    # Threshold between floor and impaired level; ``settle_fraction`` sits it above
    # the partial packet so the immature transition sample is excluded.
    impaired_lo = floor + settle_fraction * (impaired - floor)
    hold = max(1, min(settle_window, len(delivered) // 2))

    # Onset = first delivered sample at/above the regime that *starts* a sustained
    # window mostly at/above it. A startup drop (``None``) or the partial packet
    # keeps its window from qualifying, so onset lands on the first mature packet
    # after them — the point from which the measurement is clean.
    for i in range(n):
        value = samples[i]
        if value is None or value < impaired_lo:
            continue
        ahead = samples[i : i + hold]
        if len(ahead) < hold:
            break
        good = sum(1 for s in ahead if s is not None and s >= impaired_lo)
        if good >= 0.7 * hold:
            return i
    return None


def _stream_samples(
    stream: Sequence[dict[str, Any]],
    *,
    seq0: int,
    t0: float,
    period_s: float,
    time_field: str,
) -> list[tuple[float, float | None]]:
    """One seq-ordered stream as ``(send_time, latency_or_None)`` — ``None`` = lost."""
    samples: list[tuple[float, float | None]] = []
    for record in stream:
        send_t = _send_time(record, seq0=seq0, t0=t0, period_s=period_s, time_field=time_field)
        latency = (
            None if record.get("status") == "lost" else _section_ms(record, "ota_hop_ms", "ota_hop_uncorrected_ms")
        )
        samples.append((send_t, latency))
    return samples


def exclude_probe_warmup(
    joined_records: Sequence[dict[str, Any]],
    *,
    nominal_period_s: float | None = None,
    time_field: str = "t_wrap",
) -> tuple[list[dict[str, Any]], float | None]:
    """Drop the start-up prefix from already-joined transit records.

    Returns ``(included_records, onset_send_time)`` where ``onset_send_time`` is
    the earliest per-stream impairment onset on the publish timeline — the origin
    that re-anchors "time since first publish" so ``t=0`` is the clean measurement
    start. Every record before its stream's onset (warm-up, the partial packet,
    and any startup drop) is dropped, so the summary and the raw plot agree on the
    same window. Streams with no clear warm-up step are kept whole; when no stream
    shows a step, nothing is dropped and ``onset_send_time`` is ``None``.
    """
    joined = list(joined_records)
    if not joined:
        return [], None
    period_s = (
        nominal_period_s if nominal_period_s is not None else _infer_nominal_period_s(joined, time_field=time_field)
    ) or 0.0

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in joined:
        key = (
            str(record.get("source") or ""),
            str(record.get("target") or ""),
            str(record.get("topic") or ""),
        )
        grouped.setdefault(key, []).append(record)

    kept: list[dict[str, Any]] = []
    onsets: list[float] = []
    for _key, stream in grouped.items():
        stream = sorted(stream, key=lambda record: int(record["seq"]))
        stamped = [record for record in stream if record.get(time_field) is not None]
        anchor = min(stamped or stream, key=lambda record: int(record["seq"]))
        seq0 = int(anchor["seq"])
        t0 = float(anchor.get(time_field) or 0.0)
        samples = _stream_samples(stream, seq0=seq0, t0=t0, period_s=period_s, time_field=time_field)
        onset_index = find_probe_onset([latency for _, latency in samples])
        if onset_index is None:
            kept.extend(stream)
            continue
        onset_send = samples[onset_index][0]
        onsets.append(onset_send)
        kept.extend(record for record, (send_t, _lat) in zip(stream, samples, strict=True) if send_t >= onset_send)
    return kept, (min(onsets) if onsets else None)


def characterize_probe_records(
    records: Iterable[dict[str, Any]],
    *,
    bin_s: float = 1.0,
    nominal_period_s: float | None = None,
    topic: str = "",
    time_field: str = "t_wrap",
    exclude_warmup: bool = True,
) -> list[dict[str, Any]]:
    """Summarize one fixed probe as per-topic time bins.

    Bins use publish time, not arrival time, so latency/loss spikes line up with
    the traffic that caused them. Lost records often lack timestamps; when a
    nominal period is supplied (usually ``1 / rate_hz``), their send time is
    reconstructed from sequence number just like the recovery metrics do.

    With ``exclude_warmup`` (the default) the un-impaired warm-up plateau and the
    partial-impairment transition packet are dropped via
    :func:`exclude_probe_warmup`, and the bins are aligned to the impairment
    onset so ``bin_start_s == 0`` is where shaping is fully applied. Set it False
    to bin the raw timeline from the first publish.
    """
    if bin_s <= 0.0:
        raise ValueError("bin_s must be > 0.")

    from .transit import join_transit_records

    joined_records = join_transit_records(records)
    if not joined_records:
        return []
    period_s = (
        nominal_period_s
        if nominal_period_s is not None
        else _infer_nominal_period_s(joined_records, time_field=time_field)
    )
    period_s = float(period_s or 0.0)

    if exclude_warmup:
        joined_records, _onset = exclude_probe_warmup(joined_records, nominal_period_s=period_s, time_field=time_field)
        if not joined_records:
            return []

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in joined_records:
        key = (
            str(record.get("source") or ""),
            str(record.get("target") or ""),
            str(record.get("topic") or ""),
        )
        grouped.setdefault(key, []).append(record)

    expanded: list[tuple[str, dict[str, Any], float]] = []
    for _key, stream in sorted(grouped.items()):
        stream = sorted(stream, key=lambda record: int(record["seq"]))
        stamped = [record for record in stream if record.get(time_field) is not None]
        anchor = min(stamped or stream, key=lambda record: int(record["seq"]))
        seq0 = int(anchor["seq"])
        t0 = float(anchor.get(time_field) or 0.0)
        for record in stream:
            label = _transit_topic_label(record)
            if topic and topic not in {label, str(record.get("topic") or "")}:
                continue
            expanded.append(
                (
                    label,
                    record,
                    _send_time(record, seq0=seq0, t0=t0, period_s=period_s, time_field=time_field),
                )
            )
    if not expanded:
        return []

    first_send_s = min(send_t for _label, _record, send_t in expanded)
    buckets: dict[tuple[str, float], dict[str, Any]] = {}
    for label, record, send_t in expanded:
        relative_s = max(0.0, send_t - first_send_s)
        bucket_start = math.floor(relative_s / bin_s) * bin_s
        bucket = buckets.setdefault(
            (label, bucket_start),
            {
                "topic": label,
                "bin_start_s": bucket_start,
                "bin_end_s": bucket_start + bin_s,
                "expected": 0,
                "delivered": 0,
                "lost": 0,
                "payload_bytes": 0.0,
                "latencies": [],
                "jitters": [],
                "inter_arrivals": [],
            },
        )
        bucket["expected"] += 1
        if record.get("status") == "lost":
            bucket["lost"] += 1
            continue
        bucket["delivered"] += 1
        if record.get("size_bytes") is not None:
            bucket["payload_bytes"] += float(record["size_bytes"])
        latency_ms = _section_ms(record, "ota_hop_ms", "ota_hop_uncorrected_ms")
        if latency_ms is not None:
            bucket["latencies"].append(latency_ms)
        if record.get("jitter_ms") is not None:
            bucket["jitters"].append(float(record["jitter_ms"]))
        if record.get("inter_arrival_ms") is not None:
            bucket["inter_arrivals"].append(float(record["inter_arrival_ms"]))

    rows: list[dict[str, Any]] = []
    for (_label, _bucket_start), bucket in sorted(buckets.items()):
        expected = int(bucket["expected"])
        delivered = int(bucket["delivered"])
        lost = int(bucket["lost"])
        payload_bytes = float(bucket["payload_bytes"])
        row = ProbeBin(
            topic=str(bucket["topic"]),
            bin_start_s=round(float(bucket["bin_start_s"]), 6),
            bin_end_s=round(float(bucket["bin_end_s"]), 6),
            expected=expected,
            delivered=delivered,
            lost=lost,
            loss_pct=round(100.0 * lost / expected, 3) if expected else 0.0,
            delivered_hz=round(delivered / bin_s, 3),
            expected_hz=round(expected / bin_s, 3),
            payload_bandwidth_bps=round(payload_bytes * 8.0 / bin_s, 3),
            mean_size_bytes=round(payload_bytes / delivered, 3) if delivered else None,
            latency_p50_ms=_percentile(bucket["latencies"], 0.50),
            latency_p95_ms=_percentile(bucket["latencies"], 0.95),
            jitter_p50_ms=_percentile(bucket["jitters"], 0.50),
            jitter_p95_ms=_percentile(bucket["jitters"], 0.95),
            inter_arrival_p50_ms=_percentile(bucket["inter_arrivals"], 0.50),
            inter_arrival_p95_ms=_percentile(bucket["inter_arrivals"], 0.95),
        )
        rows.append(asdict(row))
    return rows


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
