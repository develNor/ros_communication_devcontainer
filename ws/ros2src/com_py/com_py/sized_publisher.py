#!/usr/bin/env python3

import time

import rclpy
from rclpy.node import Node
from com_msgs.msg import SizedPayload


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
            f"Publishing topic {topic} with rate {rate} Hz and payload size {self.size} bytes. "
            f"Bandwidth: {bandwidth_bits:.0f} bit/s, {bandwidth_bytes:.0f} byte/s"
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
            self.get_logger().info(f"Sent seq={self.seq - 1}")


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
