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

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Deque, Optional, Tuple

import rclpy
from rclpy.node import Node
from rclpy.time import Time
from rclpy.qos import QoSProfile

from std_msgs.msg import String

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
    Subscribes to a Heartbeat topic and:
      - prints delay to terminal (similar to `ros2 topic delay`)
      - publishes delay in ms
      - publishes observed Hz (windowed)
      - publishes a fixed-width, aligned multiline summary string for GUI usage
      - publishes reordering / loss indication via the heartbeat sequence number
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

        # Thresholds for status classification
        self.declare_parameter("delay_good_ms", 50)
        self.declare_parameter("delay_bad_ms", 100)
        self.declare_parameter("loss10_degraded_pct", 2.5)
        self.declare_parameter("loss10_bad_pct", 5.5)
        # Hz tolerance in messages (due to window timing, ±1 is acceptable)
        self.declare_parameter("hz_ok_tol_msgs", 1)       # OK if within ±1 msg
        self.declare_parameter("hz_degraded_tol_msgs", 2) # Degraded at ±2 msgs
        self.declare_parameter("hz_bad_tol_msgs", 3)      # Bad at ±3 msgs
        # Age multiplier for LOST status (multiplied by msg_interval)
        self.declare_parameter("age_lost_intervals", 5)   # LOST if no msg for 5+ intervals

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

        self.delay_good_ms = int(self.get_parameter("delay_good_ms").value)
        self.delay_bad_ms = int(self.get_parameter("delay_bad_ms").value)
        self.loss10_degraded_pct = float(self.get_parameter("loss10_degraded_pct").value)
        self.loss10_bad_pct = float(self.get_parameter("loss10_bad_pct").value)
        self.hz_ok_tol_msgs = int(self.get_parameter("hz_ok_tol_msgs").value)
        self.hz_degraded_tol_msgs = int(self.get_parameter("hz_degraded_tol_msgs").value)
        self.hz_bad_tol_msgs = int(self.get_parameter("hz_bad_tol_msgs").value)
        self.age_lost_intervals = int(self.get_parameter("age_lost_intervals").value)

        # Derive age thresholds from expected_hz and delay thresholds
        # Age = time since last message received. At expected_hz, messages arrive every (1000/hz) ms.
        # Good age: one message interval + good delay tolerance
        # Bad age: two message intervals + bad delay tolerance
        # Lost age: N message intervals (connection seems completely gone)
        msg_interval_ms = int(1000.0 / self.expected_hz) if self.expected_hz > 0 else 100
        self.age_good_ms = msg_interval_ms + self.delay_good_ms
        self.age_bad_ms = 2 * msg_interval_ms + self.delay_bad_ms
        self.age_lost_ms = self.age_lost_intervals * msg_interval_ms

        # Load QoS configuration
        self.qos_config = load_qos_config(self.get_logger(), self.qos_config_file)

        # Build QoS profiles from config
        sub_qos = get_topic_qos(
            self.get_logger(), self.qos_config, self.heartbeat_topic, self.sub_role
        )

        # Topics for published diagnostics
        self.delay_topic = self.heartbeat_topic + "/delay_readable"
        self.hz_topic = self.heartbeat_topic + "/hz_readable"

        self.delay_pub = self.create_publisher(
            String, self.delay_topic, qos_profile=QoSProfile(depth=10)
        )
        self.hz_pub = self.create_publisher(
            String, self.hz_topic, qos_profile=QoSProfile(depth=10)
        )
        self.summary_pub = self.create_publisher(
            String, self.summary_topic, qos_profile=QoSProfile(depth=10)
        )

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
        self.last_delay_ms: int = 0  # Store last delay for summary

        # For Hz computation over a sliding time window
        # We track message timestamps in self.events and compute Hz from there
        self.hz_last_time: Time = self.get_clock().now()
        self.last_hz_observed: float = 0.0

        # Gap / reorder state for summary
        self.last_gap_wall_hms: str = "--:--:--"
        self.no_loss_streak_start: Time | None = None

        # Rolling event queues for windowed stats
        # Each event: (t_monotonic_ns, expected_delta, missing_delta, reordered_delta, burst_missing)
        self.events: Deque[Tuple[int, int, int, int, int]] = deque()

        # Track burst within 60s in a stable way
        self.max_burst_60s: int = 0

        # Timer to periodically log Hz
        self.timer = self.create_timer(self.hz_window_s, self.hz_timer_callback)

        # Summary timer
        summary_period = 1.0 / max(0.1, self.summary_rate_hz)
        self.summary_timer = self.create_timer(summary_period, self.summary_timer_callback)

        self.get_logger().info(
            f"HeartbeatInMonitor initialized. Listening on '{self.heartbeat_topic}', "
            f"Hz window={self.hz_window_s:.2f}s, summary='{self.summary_topic}' @ {self.summary_rate_hz:.1f} Hz"
        )

    # ------------------------------------------------------------------
    # Formatting helpers (fixed width, right-aligned with spaces)
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_int(n: int, width: int) -> str:
        n = max(0, min(n, 10**width - 1))
        return f"{n:>{width}d}"

    @classmethod
    def _fmt_ms(cls, ms: int) -> str:
        return cls._fmt_int(ms, 4)  # right-aligned, max 9999

    @staticmethod
    def _fmt_hz(obs: float, exp: float) -> str:
        return f"{obs:4.1f}/{exp:4.1f}"

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
        seq_delta = 0
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

        # Publish delay in ms (unchanged behaviour)
        delay_msg = String()
        delay_msg.data = f"Last Incoming Heartbeat latency {delay_ms} ms"
        self.delay_pub.publish(delay_msg)

        # Update state
        self.last_heartbeat_time = now
        self.last_seq = seq
        self.last_delay_ms = delay_ms  # Store for summary

        # Record event for windowed summary stats
        # Expected delta: 1 new message received in this step.
        self.events.append((now_ns, 1, missing, reordered, burst_missing))
        self._prune_events(now_ns, 60.0)

    def hz_timer_callback(self) -> None:
        """
        Periodic timer to compute and publish the observed heartbeat Hz
        over the last `hz_window_s` seconds, using the events deque.
        """
        now = self.get_clock().now()
        now_ns = now.nanoseconds

        # Prune and count messages in the Hz window from the events deque
        self._prune_events(now_ns, 60.0)
        cutoff_ns = now_ns - int(self.hz_window_s * 1e9)
        msg_count_in_window = sum(1 for t_ns, *_ in self.events if t_ns >= cutoff_ns)

        hz = msg_count_in_window / self.hz_window_s if self.hz_window_s > 0 else 0.0
        self.last_hz_observed = hz

        hz_msg = String()
        hz_msg.data = f"Incoming Heartbeat frequency: {int(hz)} Hz"
        self.hz_pub.publish(hz_msg)

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
        w10 = self._accumulate_window(now_ns, 10.0)
        w60 = self._accumulate_window(now_ns, 60.0)

        loss10_pct = (100.0 * w10.missing / w10.expected) if w10.expected > 0 else 0.0
        loss60_pct = (100.0 * w60.missing / w60.expected) if w60.expected > 0 else 0.0

        # No-loss streak
        if self.no_loss_streak_start is None:
            no_loss_s = 0
        else:
            no_loss_s = int((now - self.no_loss_streak_start).nanoseconds / 1e9)
            no_loss_s = max(0, no_loss_s)

        # Hz vs expected
        hz_obs = self.last_hz_observed
        exp_hz = self.expected_hz
        delay_ms = self.last_delay_ms

        # Status decision
        status, reason = self._classify_status(age_ms, delay_ms, loss10_pct, hz_obs, exp_hz)

        # Build aligned summary (fixed lines, right-aligned values)
        topic = self.heartbeat_topic
        wall_time = datetime.now().strftime("%H:%M:%S")

        # Line 1: Header with topic and timestamp
        line1 = f"HB IN  {topic}  {wall_time}"

        # Line 2: Status (only show reason when not GOOD)
        if status == "GOOD":
            line2 = f"STATUS : {status}"
        else:
            line2 = f"STATUS : {status:<8}  REASON : {reason}"

        # Line 3: Age, delay, Hz
        line3 = (
            f"AGE  : {self._fmt_ms(age_ms)} ms"
            f"   DELAY : {self._fmt_ms(delay_ms)} ms"
            f"   HZ : {self._fmt_hz(hz_obs, exp_hz)}"
        )

        # Line 4: Loss stats
        line4 = (
            f"LOSS10 : {self._fmt_pct(loss10_pct)} "
            f"({self._fmt_int(w10.missing,3)}/{self._fmt_int(w10.expected,3)})"
            f"   LOSS60 : {self._fmt_pct(loss60_pct)} "
            f"({self._fmt_int(w60.missing,4)}/{self._fmt_int(w60.expected,4)})"
        )

        # Line 5: Gap and streak info
        line5 = (
            f"LAST_GAP : {self.last_gap_wall_hms}"
            f"   NO_LOSS : {self._fmt_hhmmss(no_loss_s)}"
        )

        # Line 6: Burst and reorder stats
        line6 = (
            f"BURST60  : {w60.max_burst_missing:>4d}"
            f"         REORD60 : {w60.reordered:>4d}"
        )

        summary = "\n".join([line1, line2, line3, line4, line5, line6])

        msg = String()
        msg.data = summary
        self.summary_pub.publish(msg)

    def _classify_status(self, age_ms: int, delay_ms: int, loss10_pct: float, hz_obs: float, exp_hz: float) -> Tuple[str, str]:
        # LOST: Connection seems completely gone
        # Check age first - no messages for a long time
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

        # Loss
        if loss10_pct >= self.loss10_bad_pct:
            return "BAD", "loss10"
        if loss10_pct >= self.loss10_degraded_pct:
            return "DEGRADED", "loss10"

        # Hz deviation (message count based, accounting for window timing jitter)
        hz_dev_msgs = abs(hz_obs - exp_hz)
        if hz_dev_msgs >= self.hz_bad_tol_msgs:
            return "BAD", "hz"
        if hz_dev_msgs >= self.hz_degraded_tol_msgs:
            return "DEGRADED", "hz"
        # hz_dev_msgs <= hz_ok_tol_msgs is considered OK

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
