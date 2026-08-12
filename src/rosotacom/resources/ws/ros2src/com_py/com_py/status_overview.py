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
# \brief  rosotacom status / debugging overview node.
#
# Tracks, for every configured topic, the furthest pipeline stage it has
# reached and the first stage that is missing/broken, plus live metrics
# (last-message age, Hz, mean size, latency). Writes a machine-readable
# status.json (source of truth), a rendered status.txt, and an append-only
# events.jsonl (one line per state transition).
#
# Phase 1 observes only what the local peer's ROS graph exposes:
#   * local-domain stages are sampled directly for live metrics
#   * OTA-domain stages use graph metadata only and never create DataReaders;
#     activity is inferred from the adjacent local stage where possible
# End-to-end remote confirmation is reserved for a later phase; remote-side
# fields are reported as "unknown".
#
# The pure classification/rollup/rendering logic lives in
# status_overview_core (ROS-independent, unit-tested on the host); this file
# wires it to live ROS graph observers (one per ROS domain).
# ---------------------------------------------------------------------

from __future__ import annotations

import os
import threading
from typing import Dict, Optional

import yaml

import rclpy
from rclpy.context import Context
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rclpy.serialization import serialize_message
from rosidl_runtime_py.utilities import get_message

from com_py.link_bytes import LinkByteSampler, find_interface_for_ip
from com_py.link_trace import LinkTraceRecorder
from com_py.status_overview_core import (
    ClockOffsetEstimator,
    StageObservation,
    StatusAggregator,
    collect_latched_stage_topics,
    collect_stage_metadata,
    collect_stage_topics,
)

ECHO_HEARTBEAT_TYPE = "com_msgs/msg/EchoHeartbeat"
OTA_STAMPED_TYPE = "com_msgs/msg/OtaStamped"


def _stamp_seconds(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) / 1e9


class StageObserver(Node):
    """
    Observes a set of stage topics within a single ROS domain context.

    Performs periodic graph introspection (publisher/subscriber counts). For
    local-domain topics it also dynamically subscribes to measure size and
    header-based latency. OTA observers are graph-only so status monitoring can
    never create an additional OTA payload stream.
    """

    def __init__(self, node_name: str, context: Optional[Context],
                 topics: Dict[str, Optional[str]], refresh_interval_s: float,
                 *, subscribe_to_messages: bool = True,
                 stage_metadata: Optional[Dict[str, list[dict]]] = None,
                 latched_topics: Optional[set] = None,
                 clock_estimator: Optional[ClockOffsetEstimator] = None):
        super().__init__(node_name, context=context)
        self.observations: Dict[str, StageObservation] = {
            t: StageObservation(type_str=hint, graph_only=not subscribe_to_messages)
            for t, hint in topics.items()
        }
        self._refresh_interval_s = refresh_interval_s
        self._subscribe_to_messages = subscribe_to_messages
        self._stage_metadata = stage_metadata or {}
        self._latched_topics = latched_topics or set()
        self.clock_estimator = clock_estimator or ClockOffsetEstimator()
        self.create_timer(self._refresh_interval_s, self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        try:
            graph = {name: types for name, types in self.get_topic_names_and_types()}
        except Exception:  # pragma: no cover - defensive
            graph = {}

        for topic, obs in self.observations.items():
            try:
                obs.pub_count = self.count_publishers(topic)
                obs.sub_count = self.count_subscribers(topic)
            except Exception:  # pragma: no cover - defensive
                pass

            if not self._subscribe_to_messages:
                continue
            if obs.subscribed:
                continue
            type_str = obs.type_str
            if not type_str:
                graph_types = graph.get(topic)
                type_str = graph_types[0] if graph_types else None
            if not type_str or obs.pub_count == 0:
                continue
            try:
                msg_class = get_message(type_str)
            except Exception:
                msg_class = None
            if msg_class is None:
                continue
            qos = self._observation_qos(topic)
            try:
                self.create_subscription(msg_class, topic, self._make_cb(topic), qos)
                obs.subscribed = True
                obs.type_str = type_str
            except Exception:  # pragma: no cover - defensive
                continue

    def _observation_qos(self, topic: str) -> QoSProfile:
        """QoS for the observation subscription, matched to the live publisher.

        A VOLATILE reader only receives samples sent *after* it matches, so it
        cannot read a TRANSIENT_LOCAL writer's already-published held sample. A
        genuinely static latched topic publishes exactly once at startup, so a
        hardcoded best-effort/volatile observer turns stage observation into a
        startup race -- it intermittently misses that single sample and reports a
        false STALLED even though the pipeline delivered and durably holds the
        value. Adopting the publisher's offered durability/reliability (request ==
        offered) is always compatible and replays the durable history. Falls back
        to best-effort/volatile when no publisher QoS is available.

        A declared latched role (issue #231, Option A) does not wait for that
        fallback: its publisher offers TRANSIENT_LOCAL by contract and retains
        the value, so the subscription requests TRANSIENT_LOCAL/RELIABLE from the
        role rather than racing a live-publisher probe that may run before the
        single publish is visible. This matching subscription is itself
        observable on the graph (it raises the topic's subscriber count), which
        is the price Option A pays over a purely passive volatile reader.
        """
        if topic in self._latched_topics:
            return QoSProfile(
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
                history=HistoryPolicy.KEEP_LAST,
                depth=10,
            )
        durability = DurabilityPolicy.VOLATILE
        reliability = ReliabilityPolicy.BEST_EFFORT
        try:
            infos = self.get_publishers_info_by_topic(topic)
        except Exception:  # pragma: no cover - defensive
            infos = []
        for info in infos:
            pub_qos = getattr(info, "qos_profile", None)
            if pub_qos is None:
                continue
            if pub_qos.durability == DurabilityPolicy.TRANSIENT_LOCAL:
                durability = DurabilityPolicy.TRANSIENT_LOCAL
            if pub_qos.reliability == ReliabilityPolicy.RELIABLE:
                reliability = ReliabilityPolicy.RELIABLE
        return QoSProfile(
            reliability=reliability,
            durability=durability,
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
        )

    def _make_cb(self, topic: str):
        def cb(msg):
            obs = self.observations[topic]
            try:
                size = len(serialize_message(msg))
            except Exception:
                size = 0
            now_ros = self.get_clock().now()
            now_ros_s = now_ros.nanoseconds / 1e9
            delay_s: Optional[float] = None
            raw_delay_s: Optional[float] = None
            rtt_s: Optional[float] = None
            clock_offset_s: Optional[float] = None
            seq: Optional[int] = None
            transit = None

            if obs.type_str == ECHO_HEARTBEAT_TYPE:
                contexts = self._stage_metadata.get(topic, [])
                is_inbound = any(
                    context.get("direction") == "inbound" for context in contexts
                )
                if is_inbound:
                    seq = int(msg.seq)
                    if bool(msg.echo_valid):
                        self.clock_estimator.update(
                            t1_s=_stamp_seconds(msg.echo_t1),
                            t2_s=_stamp_seconds(msg.echo_t2),
                            t3_s=_stamp_seconds(msg.echo_t3),
                            t4_s=now_ros_s,
                            sample_id=int(msg.echo_seq),
                        )
                    estimate = self.clock_estimator.estimate()
                    if estimate is not None:
                        clock_offset_s = estimate["offset_s"]
                        rtt_s = estimate["rtt_s"]
                        delay_s = (
                            now_ros_s
                            + clock_offset_s
                            - _stamp_seconds(msg.header.stamp)
                        )
                        if delay_s < 0.0:
                            delay_s = None
                else:
                    delay_s = now_ros_s - _stamp_seconds(msg.header.stamp)
            elif obs.type_str == OTA_STAMPED_TYPE:
                contexts = self._stage_metadata.get(topic, [])
                com_in = next(
                    (
                        context
                        for context in contexts
                        if context.get("stage") == "com_in"
                        and context.get("direction") == "inbound"
                    ),
                    None,
                )
                if com_in is not None:
                    seq = int(msg.seq)
                    t_wrap_s = _stamp_seconds(msg.header.stamp)
                    raw_delay_s = now_ros_s - t_wrap_s
                    estimate = self.clock_estimator.estimate()
                    if estimate is not None:
                        clock_offset_s = estimate["offset_s"]
                        rtt_s = estimate["rtt_s"]
                        delay_s = now_ros_s + clock_offset_s - t_wrap_s
                    transit = {
                        "peer": com_in.get("peer"),
                        "source": com_in.get("source"),
                        "target": com_in.get("target"),
                        "topic": com_in.get("base"),
                        "direction": com_in.get("direction"),
                        "stage": com_in.get("stage"),
                        "t_wrap": t_wrap_s,
                        "t_com_in": now_ros_s,
                    }
            else:
                header = getattr(msg, "header", None)
                stamp = getattr(header, "stamp", None) if header is not None else None
                if stamp is not None:
                    try:
                        msg_time = rclpy.time.Time.from_msg(stamp)
                        d = (now_ros - msg_time).nanoseconds / 1e9
                        if -1.0 < d < 1000.0:  # guard unsynced clocks / zero stamps
                            delay_s = d
                    except Exception:
                        delay_s = None

            obs.record(
                size,
                delay_s,
                seq=seq,
                raw_delay_s=raw_delay_s,
                rtt_s=rtt_s,
                clock_offset_s=clock_offset_s,
                transit=transit,
            )

        return cb


class StatusOverview(Node):
    """Coordinator node: owns the local-domain observer, the aggregator timer,
    and (when domains are split) a second observer running in the OTA context."""

    def __init__(self) -> None:
        super().__init__("status_overview")

        self.declare_parameter("status_spec_file", "")
        self.declare_parameter("output_dir", "")
        self.declare_parameter("write_interval_s", 2.0)
        self.declare_parameter("liveness_window_s", 3.0)
        self.declare_parameter("stale_after_s", 3.0)
        self.declare_parameter("refresh_interval_s", 5.0)
        self.declare_parameter("delay_good_ms", 100.0)
        self.declare_parameter("delay_bad_ms", 200.0)
        # OTA link interface for link-overhead measurement (e.g. the VPN tunnel).
        # Prefer ota_local_ip (the peer's OTA address) and resolve the interface
        # from it; ota_interface is an explicit override. Both empty -> disabled
        # (the snapshot's `link` block is null).
        self.declare_parameter("ota_interface", "")
        self.declare_parameter("ota_local_ip", "")
        self.declare_parameter("link_trace", False)
        self.declare_parameter("link_trace_interval_s", 1.0)
        self.declare_parameter("link_trace_modem_command", "")
        self.declare_parameter("link_trace_modem_timeout_s", 2.0)

        spec_file = str(self.get_parameter("status_spec_file").value or "").strip()
        output_dir = str(self.get_parameter("output_dir").value or "").strip()
        if not spec_file:
            raise ValueError("Parameter 'status_spec_file' is required")
        if not output_dir:
            output_dir = os.path.join(os.path.dirname(spec_file), "status")

        with open(spec_file, "r", encoding="utf-8") as fp:
            self.spec = yaml.safe_load(fp) or {}

        self.write_interval_s = float(self.get_parameter("write_interval_s").value)
        self.refresh_interval_s = float(self.get_parameter("refresh_interval_s").value)

        local_domain_id = self.spec.get("local_domain_id")
        ota_domain_id = self.spec.get("ota_domain_id")
        need_ota_ctx = ota_domain_id is not None and ota_domain_id != local_domain_id

        by_domain = collect_stage_topics(self.spec)
        metadata_by_domain = collect_stage_metadata(self.spec)
        latched_by_domain = collect_latched_stage_topics(self.spec)
        clock_estimator = ClockOffsetEstimator()

        self._ota_context: Optional[Context] = None
        self._ota_executor: Optional[MultiThreadedExecutor] = None
        self._ota_thread: Optional[threading.Thread] = None
        self._ota_observer: Optional[StageObserver] = None
        self._ota_observer_uses_separate_context = False
        observers: Dict[str, StageObserver] = {}

        # Local-domain stages are sampled for payload metrics.
        local_topics = dict(by_domain.get("local", {}))
        self._local_observer = StageObserver(
            "status_overview_local_obs",
            None,
            local_topics,
            self.refresh_interval_s,
            stage_metadata=metadata_by_domain.get("local"),
            latched_topics=latched_by_domain.get("local"),
            clock_estimator=clock_estimator,
        )
        observers["local"] = self._local_observer

        # OTA stages always have a distinct graph-only observer, even if the
        # local and OTA domain IDs are equal. This makes the no-OTA-DataReader
        # guarantee independent of session topology.
        ota_topics = dict(by_domain.get("ota", {}))
        if ota_topics:
            ota_context: Optional[Context] = None
            if need_ota_ctx:
                self._ota_context = Context()
                rclpy.init(context=self._ota_context, domain_id=int(ota_domain_id))
                ota_context = self._ota_context
                self._ota_observer_uses_separate_context = True
            self._ota_observer = StageObserver(
                "status_overview_ota_obs",
                ota_context,
                ota_topics,
                self.refresh_interval_s,
                subscribe_to_messages=False,
            )
            observers["ota"] = self._ota_observer
            if self._ota_observer_uses_separate_context:
                self._ota_executor = MultiThreadedExecutor(
                    num_threads=2, context=self._ota_context
                )
                self._ota_executor.add_node(self._ota_observer)
                self._ota_thread = threading.Thread(
                    target=self._ota_executor.spin, daemon=True
                )
                self._ota_thread.start()
            self.get_logger().info(
                f"status_overview: graph-only OTA observer running in domain "
                f"{ota_domain_id} (no payload subscriptions)"
            )

        ota_interface = str(self.get_parameter("ota_interface").value or "").strip()
        ota_local_ip = str(self.get_parameter("ota_local_ip").value or "").strip()
        if not ota_interface and ota_local_ip:
            ota_interface = find_interface_for_ip(ota_local_ip) or ""
            if not ota_interface:
                self.get_logger().info(
                    f"status_overview: no interface found for OTA address '{ota_local_ip}'; "
                    "link-overhead measurement disabled"
                )
        link_sampler = None
        if ota_interface:
            link_sampler = LinkByteSampler(ota_interface)
            self.get_logger().info(
                f"status_overview: link-overhead sampling on interface '{ota_interface}'"
                + (f" (resolved from {ota_local_ip})" if ota_local_ip else "")
            )

        link_trace_recorder = None
        if bool(self.get_parameter("link_trace").value):
            link_trace_path = os.path.join(output_dir, "link_trace.jsonl")
            link_trace_recorder = LinkTraceRecorder(
                link_trace_path,
                interval_s=float(self.get_parameter("link_trace_interval_s").value),
                modem_metrics_command=str(self.get_parameter("link_trace_modem_command").value or ""),
                modem_metrics_timeout_s=float(self.get_parameter("link_trace_modem_timeout_s").value),
            )
            self.get_logger().info(
                f"status_overview: link trace enabled; writing {link_trace_path}"
            )

        self.aggregator = StatusAggregator(
            self.get_logger(),
            self.spec,
            output_dir,
            observers,
            liveness_window_s=float(self.get_parameter("liveness_window_s").value),
            stale_after_s=float(self.get_parameter("stale_after_s").value),
            delay_good_ms=float(self.get_parameter("delay_good_ms").value),
            delay_bad_ms=float(self.get_parameter("delay_bad_ms").value),
            link_sampler=link_sampler,
            clock_estimator=clock_estimator,
            link_trace_recorder=link_trace_recorder,
        )

        self.create_timer(self.write_interval_s, self._on_write)
        self.get_logger().info(
            f"status_overview started: peer={self.spec.get('peer')} "
            f"topics={len(self.spec.get('topics', []))} output={output_dir}"
        )

    def add_local_nodes(self, executor: MultiThreadedExecutor) -> None:
        executor.add_node(self)
        executor.add_node(self._local_observer)
        if (
            self._ota_observer is not None
            and not self._ota_observer_uses_separate_context
        ):
            executor.add_node(self._ota_observer)

    def _on_write(self) -> None:
        self.aggregator.write()

    def shutdown(self) -> None:
        if self._ota_executor is not None:
            try:
                self._ota_executor.shutdown()
            except Exception:
                pass
        if self._ota_context is not None:
            try:
                self._ota_context.try_shutdown()
            except Exception:
                pass


def main(args=None) -> None:
    rclpy.init(args=args)
    node = StatusOverview()
    executor = MultiThreadedExecutor(num_threads=4)
    node.add_local_nodes(executor)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.shutdown()
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
