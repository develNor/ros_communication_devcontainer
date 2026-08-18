"""Receiver-side playout pacer node: re-time a topic to `stamp + budget`.

Subscribes ``topic`` (any type with a ``header.stamp``, resolved dynamically),
holds each message until its release time from
:class:`com_py.playout_pacer_core.PlayoutSchedule`, and republishes on
``<topic><topic_suffix>`` (default ``/paced``) — in order, never dropping.
Inserted between the OTA unwrapper and a decoder chain it turns network jitter
into a constant, bounded age instead of visible stutter.

Debug topics next to the output: ``.../paced/budget_ms`` (current delay
budget) and ``.../paced/queue_depth``.
"""

from __future__ import annotations

import collections
import time

import rclpy
from rclpy.node import Node
from rosidl_runtime_py.utilities import get_message
from std_msgs.msg import Float64, Int32

from com_py.playout_pacer_core import PacerConfig, PlayoutSchedule
from com_py.qos import get_topic_qos, load_qos_config


class PlayoutPacerNode(Node):
    def __init__(self) -> None:
        super().__init__("playout_pacer")
        self.declare_parameter("topic", "")
        self.declare_parameter("msg_type", "ffmpeg_image_transport_msgs/msg/FFMPEGPacket")
        self.declare_parameter("topic_suffix", "/paced")
        self.declare_parameter("target_ms", 350.0)
        self.declare_parameter("min_ms", 100.0)
        self.declare_parameter("max_ms", 800.0)
        self.declare_parameter("adaptive", True)
        self.declare_parameter("qos_config_file", "")

        topic = self.get_parameter("topic").value
        if not topic:
            raise ValueError("playout_pacer requires a 'topic' parameter")
        msg_cls = get_message(self.get_parameter("msg_type").value)
        out_topic = topic + self.get_parameter("topic_suffix").value

        self.schedule = PlayoutSchedule(
            PacerConfig(
                target_ms=float(self.get_parameter("target_ms").value),
                min_ms=float(self.get_parameter("min_ms").value),
                max_ms=float(self.get_parameter("max_ms").value),
                adaptive=bool(self.get_parameter("adaptive").value),
            )
        )
        self._queue: collections.deque = collections.deque()

        qos_file = self.get_parameter("qos_config_file").value
        if qos_file:
            qos_config = load_qos_config(self.get_logger(), qos_file)
            qos = get_topic_qos(self.get_logger(), qos_config, topic, "playout_pacer")
        else:
            qos = 10
        self._pub = self.create_publisher(msg_cls, out_topic, qos)
        self._budget_pub = self.create_publisher(Float64, out_topic + "/budget_ms", 10)
        self._depth_pub = self.create_publisher(Int32, out_topic + "/queue_depth", 10)
        self.create_subscription(msg_cls, topic, self._on_message, qos)
        # 2 ms release tick: bounds added jitter to well below frame cadence.
        self.create_timer(0.002, self._release_due)
        self.create_timer(1.0, self._publish_debug)
        self.get_logger().info(f"pacing {topic} -> {out_topic}")

    def _on_message(self, msg) -> None:
        now = time.time()
        stamp = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
        release = self.schedule.on_message(stamp, now)
        self._queue.append((release, msg))

    def _release_due(self) -> None:
        now = time.time()
        while self._queue and self._queue[0][0] <= now:
            self._pub.publish(self._queue.popleft()[1])

    def _publish_debug(self) -> None:
        self._budget_pub.publish(Float64(data=float(self.schedule.budget_ms)))
        self._depth_pub.publish(Int32(data=len(self._queue)))


def main(args=None) -> None:
    rclpy.init(args=args)
    node = PlayoutPacerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
