"""Playout scheduling for a received message stream (pure logic, no rclpy).

A receiver that displays every message the moment it arrives converts network
delay *jitter* directly into display stutter. A playout pacer re-times the
stream: release each message at ``stamp + L``, where ``L`` is a delay budget
above the fastest observed path. Messages later than their deadline pass
through immediately and in order — pacing must never reorder or drop, because
downstream consumers (an H.264 decoder chain) need every message, in order.

Clock offset between sender and receiver is unknown here, so ``L`` is never
anchored to the raw ``arrival - stamp`` value (which contains the offset) but
to its rolling *minimum* ``d_floor``: the fastest path observed carries
network base delay + clock offset, and every budget is expressed relative to
it. That makes the schedule clock-offset-proof without any synchronization.

Adaptive mode tracks a high percentile of ``arrival - stamp`` with a bounded
window and adds a margin, clamped to ``[d_floor + min_ms, d_floor + max_ms]``;
fixed mode uses ``d_floor + target_ms``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class PacerConfig:
    target_ms: float = 350.0  # fixed mode: budget above the observed floor
    min_ms: float = 100.0  # adaptive clamp, relative to the floor
    max_ms: float = 800.0
    adaptive: bool = True
    percentile: float = 0.95  # of (arrival - stamp), window-local
    margin_ms: float = 40.0  # headroom above the tracked percentile
    window: int = 400  # samples (~40 s at 10 Hz)

    def __post_init__(self) -> None:
        if not 0.0 < self.percentile < 1.0:
            raise ValueError(f"percentile must be in (0, 1), got {self.percentile}")
        if self.min_ms > self.max_ms:
            raise ValueError(f"min_ms {self.min_ms} > max_ms {self.max_ms}")
        if self.window < 10:
            raise ValueError("window must hold at least 10 samples")


class PlayoutSchedule:
    """Assigns a release time to each (stamp, arrival) pair, in arrival order."""

    def __init__(self, config: PacerConfig | None = None) -> None:
        self.config = config or PacerConfig()
        self._d: deque[float] = deque(maxlen=self.config.window)
        self._d_floor: float | None = None
        self._last_release: float | None = None

    @property
    def budget_ms(self) -> float:
        """The current delay budget above the observed floor (for observability)."""
        cfg = self.config
        if not cfg.adaptive or not self._d:
            return cfg.target_ms
        ordered = sorted(self._d)
        idx = min(len(ordered) - 1, int(cfg.percentile * len(ordered)))
        floor = self._d_floor if self._d_floor is not None else ordered[0]
        budget = (ordered[idx] - floor) * 1000.0 + cfg.margin_ms
        return min(max(budget, cfg.min_ms), cfg.max_ms)

    def on_message(self, stamp_s: float, arrival_s: float) -> float:
        """Release time (same clock as ``arrival_s``) for the message.

        Monotonic per stream: a release never precedes the previous one, so
        arrival order is preserved even when a late message resets the pace.
        """
        d = arrival_s - stamp_s
        self._d.append(d)
        if self._d_floor is None or d < self._d_floor:
            self._d_floor = d
        release = stamp_s + self._d_floor + self.budget_ms / 1000.0
        if release < arrival_s:  # already late: pass through immediately
            release = arrival_s
        if self._last_release is not None and release < self._last_release:
            release = self._last_release
        self._last_release = release
        return release
