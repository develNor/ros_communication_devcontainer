"""RFC 0005 — benchmark plot rendering (the ``[plots]`` optional extra).

All plot functions accept plain dicts/sequences (the output of the benchmark
drivers), not domain objects, so they remain testable without a live run.
``matplotlib`` is import-guarded: the core package never depends on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any


def _require_matplotlib() -> tuple[Any, Any]:
    """Return ``(matplotlib, matplotlib.pyplot)`` or raise a helpful error."""
    try:
        import matplotlib

        matplotlib.use("Agg")  # non-interactive backend
        import matplotlib.pyplot as plt

        return matplotlib, plt
    except ImportError:
        raise ImportError(
            "matplotlib is required for benchmark plots. Install with: pip install rosotacom[plots]"
        ) from None


# --------------------------------------------------------------------------- #
# 1.1 / 2.1 — capacity frontier
# --------------------------------------------------------------------------- #


def plot_capacity_frontier(
    results: Sequence[dict[str, Any]],
    *,
    x_key: str = "bandwidth_bps",
    out: str | Path,
    title: str = "Capacity frontier",
) -> Path:
    """Latency & loss vs environment params.

    X-axis is the swept knob (``x_key``); two Y-axes for ``loss_pct``
    (left) and ``latency_p95_ms`` (right).
    """
    _, plt = _require_matplotlib()
    out = Path(out)

    # Determine the actual key to use on the x-axis
    actual_x_key = x_key
    if results and x_key not in results[0] and "profile" in results[0]:
        actual_x_key = "profile"

    xs = [r[actual_x_key] for r in results]
    loss = [r["loss_pct"] for r in results]
    lat = [r["latency_p95_ms"] for r in results]

    fig, ax_loss = plt.subplots()
    ax_loss.set_xlabel(x_key)
    ax_loss.set_ylabel("Loss (%)", color="tab:red")
    ax_loss.plot(xs, loss, "o-", color="tab:red", label="loss_pct")
    ax_loss.tick_params(axis="y", labelcolor="tab:red")

    ax_lat = ax_loss.twinx()
    ax_lat.set_ylabel("Latency p95 (ms)", color="tab:blue")
    ax_lat.plot(xs, lat, "s--", color="tab:blue", label="latency_p95_ms")
    ax_lat.tick_params(axis="y", labelcolor="tab:blue")

    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


# --------------------------------------------------------------------------- #
# 1.2 — offered bandwidth
# --------------------------------------------------------------------------- #


def plot_offered_bw(
    results: Sequence[dict[str, Any]],
    *,
    out: str | Path,
    title: str = "Latency vs offered bandwidth",
) -> Path:
    """Latency vs offered bandwidth.

    Points colored by ``(size, rate_hz, streams)`` tuple.  Each result dict
    has: ``offered_bw_bps``, ``latency_p95_ms``, ``size``, ``rate_hz``,
    ``streams``.
    """
    _, plt = _require_matplotlib()
    out = Path(out)

    xs = [r["offered_bw_bps"] for r in results]
    ys = [r["latency_p95_ms"] for r in results]
    labels = [f"{r['size']}B/{r['rate_hz']}Hz/×{r['streams']}" for r in results]

    # Assign a unique colour per label.
    unique = sorted(set(labels))
    cmap = plt.colormaps["tab10"].resampled(max(len(unique), 1))
    colour_map = {lbl: cmap(i) for i, lbl in enumerate(unique)}

    fig, ax = plt.subplots()
    for x, y, lbl in zip(xs, ys, labels, strict=True):
        ax.scatter(x, y, color=colour_map[lbl], label=lbl, zorder=3)

    # De-duplicate legend entries.
    handles, leg_labels = ax.get_legend_handles_labels()
    seen: set[str] = set()
    deduped: list[tuple[Any, str]] = []
    for handle, label in zip(handles, leg_labels, strict=True):
        if label not in seen:
            seen.add(label)
            deduped.append((handle, label))
    if deduped:
        ax.legend(*zip(*deduped, strict=True), fontsize="small")

    ax.set_xlabel("Offered bandwidth (bps)")
    ax.set_ylabel("Latency p95 (ms)")
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


# --------------------------------------------------------------------------- #
# 1.3 — degradation ramp
# --------------------------------------------------------------------------- #


def plot_ramp(
    curve: Sequence[dict[str, float]],
    *,
    out: str | Path,
    title: str = "Degradation ramp",
    x_label: str = "Load",
    y_label: str = "Latency p95 (ms)",
) -> Path:
    """Latency-vs-load ramp curve with knee annotation.

    Each dict has ``value`` (the load knob) and ``metric`` (the observed
    latency).
    """
    _, plt = _require_matplotlib()
    out = Path(out)

    xs = [p["value"] for p in curve]
    ys = [p["metric"] for p in curve]

    fig, ax = plt.subplots()
    ax.plot(xs, ys, "o-", color="tab:green")

    # Simple knee detection: point with largest second-order difference.
    if len(ys) >= 3:
        diffs = [ys[i + 1] - ys[i] for i in range(len(ys) - 1)]
        second = [diffs[i + 1] - diffs[i] for i in range(len(diffs) - 1)]
        knee_idx = second.index(max(second)) + 1
        ax.annotate(
            "knee",
            xy=(xs[knee_idx], ys[knee_idx]),
            fontsize=9,
            ha="center",
            arrowprops={"arrowstyle": "->"},
            xytext=(xs[knee_idx], ys[knee_idx] * 1.15 + 1),
        )

    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


# --------------------------------------------------------------------------- #
# 1.4 / 2.3 — recovery timeline
# --------------------------------------------------------------------------- #


def plot_recovery_timeline(
    records: Sequence[dict[str, Any]],
    outage_start: float,
    outage_end: float,
    *,
    out: str | Path,
    title: str = "Recovery timeline",
) -> Path:
    """Message arrivals around the outage window.

    Plots arrival times, marks the outage window, and annotates
    ``t_recover`` and burst count.
    """
    _, plt = _require_matplotlib()
    out = Path(out)

    arrivals = sorted(float(r["arrival_s"]) for r in records if r.get("arrival_s") is not None)

    fig, ax = plt.subplots()
    ax.eventplot([arrivals], orientation="horizontal", lineoffsets=0.5, colors="tab:blue")
    ax.axvspan(outage_start, outage_end, alpha=0.25, color="red", label="outage")

    # First arrival after outage end → recovery time.
    post = [a for a in arrivals if a > outage_end]
    if post:
        t_recover = post[0] - outage_end
        ax.axvline(post[0], linestyle="--", color="tab:green", label=f"recover Δ{t_recover:.1f}s")

    # Burst: arrivals within 1 s of outage_end.
    burst = sum(1 for a in arrivals if outage_end <= a <= outage_end + 1.0)
    if burst:
        ax.set_xlabel(f"Time (s) — burst={burst}")
    else:
        ax.set_xlabel("Time (s)")

    ax.set_title(title)
    ax.legend(fontsize="small")
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


# --------------------------------------------------------------------------- #
# 2.2 — per-topic heatmap
# --------------------------------------------------------------------------- #


def plot_topic_heatmap(
    per_topic: dict[str, dict[str, float]],
    *,
    metric: str = "loss_pct",
    out: str | Path,
    title: str = "Per-topic loss heatmap",
) -> Path:
    """Per-topic × condition heatmap.

    ``per_topic`` maps ``topic_name → {condition_name → metric_value}``.
    """
    _, plt = _require_matplotlib()
    out = Path(out)

    topics = sorted(per_topic)
    if not topics:
        # Empty data — write a blank figure.
        fig, ax = plt.subplots()
        ax.set_title(title)
        fig.savefig(out, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return out

    conditions = sorted({c for vals in per_topic.values() for c in vals})
    matrix = [[per_topic[t].get(c, 0.0) for c in conditions] for t in topics]

    fig, ax = plt.subplots(figsize=(max(4, len(conditions) * 0.8), max(3, len(topics) * 0.5)))
    im = ax.imshow(matrix, aspect="auto", cmap="YlOrRd")
    fig.colorbar(im, ax=ax, label=metric)

    ax.set_xticks(range(len(conditions)))
    ax.set_xticklabels(conditions, rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(len(topics)))
    ax.set_yticklabels(topics, fontsize=8)
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out
