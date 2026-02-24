#!/usr/bin/env python3

# -- BEGIN LICENSE BLOCK ----------------------------------------------
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#    * Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
#
#    * Redistributions in binary form must reproduce the above copyright
#      notice, this list of conditions and the following disclaimer in the
#      documentation and/or other materials provided with the distribution.
#
#    * Neither the name of the {copyright_holder} nor the names of its
#      contributors may be used to endorse or promote products derived from
#      this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
# -- END LICENSE BLOCK ------------------------------------------------
#
# ---------------------------------------------------------------------
# !\file
#
# \author  Martin Gontscharow <gontscharow@fzi.de>
# \date    2024-12-17
#
# ---------------------------------------------------------------------

# NOTE: loss3 (3s window) is used for status determination instead of loss10,
# because loss10 is too long-term (degrades status for 10s on 1-2 missed msgs)
# and loss1 would be too redundant with Hz. loss10 is still published for plots/summary.

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Deque, List, Tuple

import rclpy
from rclpy.node import Node
from rclpy.time import Time
from rclpy.qos import QoSProfile

from std_msgs.msg import Float64, String

from com_msgs.msg import Heartbeat
from com_py.qos import load_qos_config, get_topic_qos


@dataclass
class WindowCounters:
    expected: int = 0
    missing: int = 0
    reordered: int = 0
    max_burst_missing: int = 0


class HeartbeatInMonitor(Node):
    """
    Subscribes to a Heartbeat topic and publishes monitoring data
    for plotting (Foxglove / Lichtblick) and diagnostics:

      Numerical (Float64) topics – for time-series plots:
        /age_ms, /delay_ms, /hz, /loss
        /delay_min_ms, /delay_avg_ms, /delay_med_ms, /delay_max_ms
        /hz_min, /hz_avg, /hz_med, /hz_max
        /loss3_pct, /loss10_pct, /loss60_pct, /loss_total_pct

      String topics – for state-transition / enum display:
        /status   (GOOD | DEGRADED | BAD | LOST)
        /reason   (age | delay | loss3 | hz | -)

      Summary string topic – remaining textual stats:
        /summary  (loss counts, burst, reorder, streak, totals)
    """

    def __init__(self) -> None:
        super().__init__("heartbeat_in_monitor")

        # Parameters
        self.declare_parameter("heartbeat_topic", "/heartbeat")
        self.declare_parameter("qos_config_file", "")
        self.declare_parameter("hz_window_s", 1.0)

        # Summary / GUI parameters
        self.declare_parameter("summary_topic_suffix", "/summary")
        self.declare_parameter("summary_rate_hz", 5.0)
        self.declare_parameter("expected_hz", 10.0)
        self.declare_parameter("stats_window_s", 60.0)

        # Thresholds for status classification
        self.declare_parameter("delay_good_ms", 50)
        self.declare_parameter("delay_bad_ms", 100)
        self.declare_parameter("loss3_degraded_pct", 5.0)
        self.declare_parameter("loss3_bad_pct", 10.0)
        # Hz tolerance in messages (due to window timing, ±1 is acceptable)
        self.declare_parameter("hz_ok_tol_msgs", 1)       # OK if within ±1 msg
        self.declare_parameter("hz_degraded_tol_msgs", 2)  # Degraded at ±2 msgs
        self.declare_parameter("hz_bad_tol_msgs", 3)       # Bad at ±3 msgs
        # Age multiplier for LOST status (multiplied by msg_interval)
        self.declare_parameter("age_lost_intervals", 5)    # LOST if no msg for 5+ intervals

        self.heartbeat_topic = self.get_parameter("heartbeat_topic").value
        self.qos_config_file = self.get_parameter("qos_config_file").value
        self.hz_window_s = float(self.get_parameter("hz_window_s").value)
        self.sub_role = "heartbeat_sub"
        self.pub_role = "heartbeat_pub"

        self.summary_topic = (
            self.heartbeat_topic + self.get_parameter("summary_topic_suffix").value
        )
        self.summary_rate_hz = float(self.get_parameter("summary_rate_hz").value)
        self.expected_hz = float(self.get_parameter("expected_hz").value)
        self.stats_window_s = float(self.get_parameter("stats_window_s").value)

        self.delay_good_ms = int(self.get_parameter("delay_good_ms").value)
        self.delay_bad_ms = int(self.get_parameter("delay_bad_ms").value)
        self.loss3_degraded_pct = float(self.get_parameter("loss3_degraded_pct").value)
        self.loss3_bad_pct = float(self.get_parameter("loss3_bad_pct").value)
        self.hz_ok_tol_msgs = int(self.get_parameter("hz_ok_tol_msgs").value)
        self.hz_degraded_tol_msgs = int(self.get_parameter("hz_degraded_tol_msgs").value)
        self.hz_bad_tol_msgs = int(self.get_parameter("hz_bad_tol_msgs").value)
        self.age_lost_intervals = int(self.get_parameter("age_lost_intervals").value)

        # Derive age thresholds from expected_hz and delay thresholds
        msg_interval_ms = int(1000.0 / self.expected_hz) if self.expected_hz > 0 else 100
        self.age_good_ms = msg_interval_ms + self.delay_good_ms
        self.age_bad_ms = 2 * msg_interval_ms + self.delay_bad_ms
        self.age_lost_ms = self.age_lost_intervals * msg_interval_ms

        # Load QoS configuration
        self.qos_config = load_qos_config(self.get_logger(), self.qos_config_file)
        sub_qos = get_topic_qos(
            self.get_logger(), self.qos_config, self.heartbeat_topic, self.sub_role
        )

        # ---- Publishers: Numerical (Float64) for plotting ----
        pub_qos = QoSProfile(depth=10)

        self.age_ms_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/age_ms", pub_qos)
        self.delay_ms_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/delay_ms", pub_qos)
        self.delay_min_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/delay_min_ms", pub_qos)
        self.delay_avg_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/delay_avg_ms", pub_qos)
        self.delay_med_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/delay_med_ms", pub_qos)
        self.delay_max_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/delay_max_ms", pub_qos)

        self.hz_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/hz", pub_qos)
        self.hz_min_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/hz_min", pub_qos)
        self.hz_avg_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/hz_avg", pub_qos)
        self.hz_med_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/hz_med", pub_qos)
        self.hz_max_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/hz_max", pub_qos)

        self.loss_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/loss", pub_qos)
        self.loss3_pct_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/loss3_pct", pub_qos)
        self.loss10_pct_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/loss10_pct", pub_qos)
        self.loss60_pct_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/loss60_pct", pub_qos)
        self.loss_total_pct_pub = self.create_publisher(
            Float64, self.heartbeat_topic + "/loss_total_pct", pub_qos)

        # ---- Publishers: String topics (state-transition / enum) ----
        self.status_pub = self.create_publisher(
            String, self.heartbeat_topic + "/status", pub_qos)
        self.reason_pub = self.create_publisher(
            String, self.heartbeat_topic + "/reason", pub_qos)
        self.summary_pub = self.create_publisher(
            String, self.summary_topic, pub_qos)

        # Subscriber
        self.subscription = self.create_subscription(
            Heartbeat,
            self.heartbeat_topic,
            self.heartbeat_callback,
            qos_profile=sub_qos,
        )

        # State
        self.last_heartbeat_time: Time | None = None
        self.last_seq: int | None = None
        self.expected_next_seq: int | None = None
        self.last_delay_ms: int = 0

        # For Hz computation over a sliding time window
        self.last_hz_observed: float = 0.0

        # Gap / reorder state for summary
        self.last_gap_wall_hms: str = "--:--:--"
        self.no_loss_streak_start: Time | None = None

        # Rolling event queues for windowed stats
        # Each event: (t_monotonic_ns, expected_delta, missing_delta, reordered_delta, burst_missing)
        self.events: Deque[Tuple[int, int, int, int, int]] = deque()

        # Delay history for min/avg/med/max stats  (timestamp_ns, delay_ms)
        self.delay_history: Deque[Tuple[int, int]] = deque()

        # Hz history for min/avg/med/max stats  (timestamp_ns, hz)
        self.hz_history: Deque[Tuple[int, float]] = deque()

        # Total counters (since node start)
        self.total_received: int = 0
        self.total_expected: int = 0
        self.total_missing: int = 0
        self.total_reordered: int = 0

        # Timer to periodically compute Hz
        self.timer = self.create_timer(self.hz_window_s, self.hz_timer_callback)

        # Summary timer (also publishes all periodic numerical topics)
        summary_period = 1.0 / max(0.1, self.summary_rate_hz)
        self.summary_timer = self.create_timer(summary_period, self.summary_timer_callback)

        self.get_logger().info(
            f"HeartbeatInMonitor initialized. Listening on '{self.heartbeat_topic}', "
            f"Hz window={self.hz_window_s:.2f}s, stats window={self.stats_window_s:.0f}s, "
            f"summary='{self.summary_topic}' @ {self.summary_rate_hz:.1f} Hz"
        )

    # ------------------------------------------------------------------
    # Formatting helpers (for summary string)
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_int(n: int, width: int) -> str:
        n = max(0, min(n, 10**width - 1))
        return f"{n:>{width}d}"

    @staticmethod
    def _fmt_pct(p: float) -> str:
        p = max(0.0, min(p, 99.99))
        return f"{p:5.2f}%"

    @staticmethod
    def _fmt_hhmmss(total_s: int) -> str:
        total_s = max(0, min(total_s, 99 * 3600 + 59 * 60 + 59))
        h = total_s // 3600
        m = (total_s % 3600) // 60
        s = total_s % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    # ------------------------------------------------------------------
    # Windowed stats
    # ------------------------------------------------------------------

    def _prune_events(self, now_ns: int, max_window_s: float = 60.0) -> None:
        cutoff = now_ns - int(max_window_s * 1e9)
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    def _prune_deque(self, dq: deque, now_ns: int, window_s: float) -> None:
        cutoff = now_ns - int(window_s * 1e9)
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _accumulate_window(self, now_ns: int, window_s: float) -> WindowCounters:
        cutoff = now_ns - int(window_s * 1e9)
        wc = WindowCounters()
        for t_ns, exp_d, miss_d, reord_d, burst_m in self.events:
            if t_ns >= cutoff:
                wc.expected += exp_d
                wc.missing += miss_d
                wc.reordered += reord_d
                wc.max_burst_missing = max(wc.max_burst_missing, burst_m)
        return wc

    @staticmethod
    def _compute_stats(values: List[float]) -> Tuple[float, float, float, float]:
        """Return (min, avg, median, max) for *values*; (0,0,0,0) if empty."""
        if not values:
            return (0.0, 0.0, 0.0, 0.0)
        return (
            min(values),
            statistics.mean(values),
            statistics.median(values),
            max(values),
        )

    # ------------------------------------------------------------------
    # Publish helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pub_float(pub, value: float) -> None:
        msg = Float64()
        msg.data = float(value)
        pub.publish(msg)

    @staticmethod
    def _pub_string(pub, value: str) -> None:
        msg = String()
        msg.data = value
        pub.publish(msg)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def heartbeat_callback(self, msg: Heartbeat) -> None:
        now = self.get_clock().now()
        now_ns = now.nanoseconds
        wall_hms = datetime.now().strftime("%H:%M:%S")

        src_time = Time.from_msg(msg.header.stamp)
        delay_ms = int((now - src_time).nanoseconds / 1e6)

        seq = int(msg.seq)
        missing = 0
        reordered = 0
        burst_missing = 0

        if self.expected_next_seq is None:
            self.expected_next_seq = seq + 1
            # On first msg, start "no loss streak" now
            self.no_loss_streak_start = now
        else:
            seq_delta = seq - self.expected_next_seq

            if seq_delta > 0:
                # Missing IDs
                missing = seq_delta
                burst_missing = missing

                missing_start = self.expected_next_seq
                missing_end = seq - 1

                if missing_start == missing_end:
                    self.get_logger().warning(
                        f"Heartbeat gap detected: received id {seq}, but id {missing_start} is missing"
                    )
                else:
                    self.get_logger().warning(
                        f"Heartbeat gap detected: received id {seq}, but {missing} ids ({missing_start}-{missing_end}) are missing"
                    )

                self.last_gap_wall_hms = wall_hms
                self.no_loss_streak_start = now  # reset streak start on loss event

            elif seq_delta < 0 and self.last_seq is not None:
                reordered = 1
                self.get_logger().warning(
                    f"Out-of-order heartbeat: id {seq} arrived after already receiving id {self.last_seq}"
                )

            self.expected_next_seq = seq + 1

        # Publish delay and per-message loss immediately (for real-time plotting)
        self._pub_float(self.delay_ms_pub, delay_ms)
        self._pub_float(self.loss_pub, missing)

        # Update state
        self.last_heartbeat_time = now
        self.last_seq = seq
        self.last_delay_ms = delay_ms

        # Record event for windowed summary stats
        self.events.append((now_ns, 1, missing, reordered, burst_missing))
        self._prune_events(now_ns, 60.0)

        # Record delay for statistics
        self.delay_history.append((now_ns, delay_ms))
        self._prune_deque(self.delay_history, now_ns, self.stats_window_s)

        # Update total counters
        self.total_received += 1
        self.total_expected += 1 + missing  # received + missing
        self.total_missing += missing
        self.total_reordered += reordered

    def hz_timer_callback(self) -> None:
        """
        Periodic timer to compute and publish the observed heartbeat Hz
        over the last ``hz_window_s`` seconds, using the events deque.
        """
        now = self.get_clock().now()
        now_ns = now.nanoseconds

        # Prune and count messages in the Hz window from the events deque
        self._prune_events(now_ns, 60.0)
        cutoff_ns = now_ns - int(self.hz_window_s * 1e9)
        msg_count_in_window = sum(1 for t_ns, *_ in self.events if t_ns >= cutoff_ns)

        hz = msg_count_in_window / self.hz_window_s if self.hz_window_s > 0 else 0.0
        self.last_hz_observed = hz

        # Publish Hz immediately (for real-time plotting)
        self._pub_float(self.hz_pub, hz)

        # Record Hz for statistics
        self.hz_history.append((now_ns, hz))
        self._prune_deque(self.hz_history, now_ns, self.stats_window_s)

    def summary_timer_callback(self) -> None:
        now = self.get_clock().now()
        now_ns = now.nanoseconds

        # Age since last message
        if self.last_heartbeat_time is None:
            age_ms = 9999
        else:
            age_ms = int((now - self.last_heartbeat_time).nanoseconds / 1e6)
            age_ms = max(0, min(age_ms, 9999))

        # Window stats
        self._prune_events(now_ns, 60.0)
        w3 = self._accumulate_window(now_ns, 3.0)
        w10 = self._accumulate_window(now_ns, 10.0)
        w60 = self._accumulate_window(now_ns, 60.0)

        loss3_pct = (100.0 * w3.missing / w3.expected) if w3.expected > 0 else 0.0
        loss10_pct = (100.0 * w10.missing / w10.expected) if w10.expected > 0 else 0.0
        loss60_pct = (100.0 * w60.missing / w60.expected) if w60.expected > 0 else 0.0
        loss_total_pct = (
            (100.0 * self.total_missing / self.total_expected)
            if self.total_expected > 0
            else 0.0
        )

        # No-loss streak
        if self.no_loss_streak_start is None:
            no_loss_s = 0
        else:
            no_loss_s = int((now - self.no_loss_streak_start).nanoseconds / 1e9)
            no_loss_s = max(0, no_loss_s)

        hz_obs = self.last_hz_observed
        exp_hz = self.expected_hz
        delay_ms = self.last_delay_ms

        # ---- Publish numerical topics for plotting ----
        self._pub_float(self.age_ms_pub, age_ms)

        # Loss percentages (for the loss plot)
        self._pub_float(self.loss3_pct_pub, loss3_pct)
        self._pub_float(self.loss10_pct_pub, loss10_pct)
        self._pub_float(self.loss60_pct_pub, loss60_pct)
        self._pub_float(self.loss_total_pct_pub, loss_total_pct)

        # Delay statistics (over stats_window_s)
        self._prune_deque(self.delay_history, now_ns, self.stats_window_s)
        delay_values = [float(d) for _, d in self.delay_history]
        d_min, d_avg, d_med, d_max = self._compute_stats(delay_values)
        self._pub_float(self.delay_min_pub, d_min)
        self._pub_float(self.delay_avg_pub, d_avg)
        self._pub_float(self.delay_med_pub, d_med)
        self._pub_float(self.delay_max_pub, d_max)

        # Hz statistics (over stats_window_s)
        self._prune_deque(self.hz_history, now_ns, self.stats_window_s)
        hz_values = [h for _, h in self.hz_history]
        h_min, h_avg, h_med, h_max = self._compute_stats(hz_values)
        self._pub_float(self.hz_min_pub, h_min)
        self._pub_float(self.hz_avg_pub, h_avg)
        self._pub_float(self.hz_med_pub, h_med)
        self._pub_float(self.hz_max_pub, h_max)

        # ---- Publish status and reason (state-transition topics) ----
        status, reason = self._classify_status(age_ms, delay_ms, loss3_pct, hz_obs, exp_hz)
        self._pub_string(self.status_pub, status)
        self._pub_string(self.reason_pub, reason)

        # ---- Build summary with remaining textual stats ----
        wall_time = datetime.now().strftime("%H:%M:%S")
        topic = self.heartbeat_topic

        lines = [
            f"HB IN  {topic}  {wall_time}",
            (
                f"LOSS3  : {self._fmt_pct(loss3_pct)}"
                f" ({self._fmt_int(w3.missing, 2)}/{self._fmt_int(w3.expected, 2)})"
                f"   LOSS10 : {self._fmt_pct(loss10_pct)}"
                f" ({self._fmt_int(w10.missing, 3)}/{self._fmt_int(w10.expected, 3)})"
            ),
            (
                f"LOSS60 : {self._fmt_pct(loss60_pct)}"
                f" ({self._fmt_int(w60.missing, 4)}/{self._fmt_int(w60.expected, 4)})"
            ),
            (
                f"TOTAL  : {self._fmt_pct(loss_total_pct)}"
                f" ({self._fmt_int(self.total_missing, 6)}/{self._fmt_int(self.total_expected, 6)})"
                f"   RECV : {self._fmt_int(self.total_received, 6)}"
            ),
            (
                f"BURST60 : {w60.max_burst_missing:>4d}"
                f"   REORD60 : {w60.reordered:>4d}"
                f"   REORD_T : {self.total_reordered:>6d}"
            ),
            (
                f"LAST_GAP : {self.last_gap_wall_hms}"
                f"   NO_LOSS : {self._fmt_hhmmss(no_loss_s)}"
            ),
        ]

        self._pub_string(self.summary_pub, "\n".join(lines))

    def _classify_status(
        self,
        age_ms: int,
        delay_ms: int,
        loss3_pct: float,
        hz_obs: float,
        exp_hz: float,
    ) -> Tuple[str, str]:
        # LOST: Connection seems completely gone
        if age_ms >= self.age_lost_ms:
            return "LOST", "age"

        # BAD/DEGRADED: Age (derived from expected_hz + delay thresholds)
        if age_ms >= self.age_bad_ms:
            return "BAD", "age"
        if age_ms >= self.age_good_ms:
            return "DEGRADED", "age"

        # Delay (latency)
        if delay_ms >= self.delay_bad_ms:
            return "BAD", "delay"
        if delay_ms > self.delay_good_ms:
            return "DEGRADED", "delay"

        # Loss (3s window – responsive without being too jittery)
        if loss3_pct >= self.loss3_bad_pct:
            return "BAD", "loss3"
        if loss3_pct >= self.loss3_degraded_pct:
            return "DEGRADED", "loss3"

        # Hz deviation (message count based, accounting for window timing jitter)
        hz_dev_msgs = abs(hz_obs - exp_hz)
        if hz_dev_msgs >= self.hz_bad_tol_msgs:
            return "BAD", "hz"
        if hz_dev_msgs >= self.hz_degraded_tol_msgs:
            return "DEGRADED", "hz"

        return "GOOD", "-"


def main(args=None) -> None:
    rclpy.init(args=args)
    node = HeartbeatInMonitor()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("Shutting down HeartbeatInMonitor …")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
