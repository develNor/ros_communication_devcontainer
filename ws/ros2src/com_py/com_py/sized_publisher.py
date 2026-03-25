#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from com_msgs.msg import SizedPayload


def _human_bytes(n: float) -> str:
    """Format a byte count into a human-readable string (KB, MB, GB)."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1000.0:
            return f"{n:.3g} {unit}"
        n /= 1000.0
    return f"{n:.3g} TB"


def _human_bits(n: float) -> str:
    """Format a bit count into a human-readable string (Kb, Mb, Gb)."""
    for unit in ("bit", "Kb", "Mb", "Gb"):
        if abs(n) < 1000.0:
            return f"{n:.3g} {unit}"
        n /= 1000.0
    return f"{n:.3g} Tb"


class SizedPublisher(Node):
    def __init__(self):
        super().__init__("sized_publisher")

        self.declare_parameter("topic", "/size_test")
        self.declare_parameter("size", 62000)
        self.declare_parameter("rate", 10.0)

        topic = self.get_parameter("topic").value
        self.size = self.get_parameter("size").value
        rate = self.get_parameter("rate").value

        self.publisher = self.create_publisher(SizedPayload, topic, 10)
        self.seq = 0

        period = 1.0 / rate
        self.timer = self.create_timer(period, self._publish)

        bandwidth_bits = self.size * 8 * rate
        bandwidth_bytes = self.size * rate
        self.get_logger().info(
            f"Publishing topic {topic} with rate {rate} Hz, "
            f"payload {_human_bytes(self.size)}. "
            f"Bandwidth: {_human_bits(bandwidth_bits)}/s, {_human_bytes(bandwidth_bytes)}/s"
        )

    def _publish(self):
        msg = SizedPayload()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.seq = self.seq
        msg.size = self.size
        msg.payload = list(b"\x42" * self.size)

        self.publisher.publish(msg)
        self.seq += 1

        if self.seq % 100 == 0:
            self.get_logger().debug(f"Sent seq={self.seq - 1}")


def main(args=None):
    rclpy.init(args=args)
    node = SizedPublisher()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
