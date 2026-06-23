#!/usr/bin/env python3

import re

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


def _parse_pattern(pattern: str) -> list[str]:
    """
    Parse a pattern like "a*4,b*1" or "ax4,bx1" into a cyclic size sequence.
    """
    if not pattern.strip():
        return ["a"]

    tokens: list[str] = []
    for raw_token in pattern.split(","):
        token = raw_token.strip().lower()
        match = re.fullmatch(r"([ab])(?:[*x](\d+))?", token)
        if not match:
            raise ValueError(
                f"Invalid pattern token '{raw_token}'. Use tokens like 'a', 'b', 'ax4', or 'b*2'."
            )

        key = match.group(1)
        count = int(match.group(2) or "1")
        if count < 1:
            raise ValueError(f"Pattern token '{raw_token}' must repeat at least once.")
        tokens.extend([key] * count)

    if not tokens:
        raise ValueError("Pattern must contain at least one token.")

    return tokens


class SizedPublisher(Node):
    def __init__(self):
        super().__init__("sized_publisher")

        self.declare_parameter("topic", "/size_test")
        self.declare_parameter("size", 62000)
        self.declare_parameter("size_a", -1)
        self.declare_parameter("size_b", -1)
        self.declare_parameter("pattern", "")
        self.declare_parameter("rate", 10.0)
        self.declare_parameter("streams", 1)

        topic = self.get_parameter("topic").value
        default_size = int(self.get_parameter("size").value)
        size_a_param = int(self.get_parameter("size_a").value)
        size_b_param = int(self.get_parameter("size_b").value)
        pattern_param = str(self.get_parameter("pattern").value)
        rate = self.get_parameter("rate").value
        streams = max(1, int(self.get_parameter("streams").value))

        self.size_a = size_a_param if size_a_param >= 0 else default_size
        self.size_b = size_b_param
        self.uses_pattern = bool(pattern_param.strip()) or self.size_b >= 0
        if pattern_param.strip():
            self.pattern = _parse_pattern(pattern_param)
        elif self.size_b >= 0:
            self.pattern = ["a", "b"]
        else:
            self.pattern = ["a"]

        if any(token == "b" for token in self.pattern) and self.size_b < 0:
            raise ValueError(
                "Pattern references size 'b', but parameter 'size_b' is not set. "
                "Provide e.g. -p size_b:=70."
            )

        if self.size_a < 0:
            raise ValueError("size_a must be >= 0.")
        if self.size_b < -1:
            raise ValueError("size_b must be >= 0 when set.")

        self.streams_state: list[dict] = []
        for i in range(streams):
            stream_topic = f"{topic}_{i}" if streams > 1 else topic
            pub = self.create_publisher(SizedPayload, stream_topic, 10)
            self.streams_state.append({"publisher": pub, "seq": 0, "topic": stream_topic})

        period = 1.0 / rate
        self.timer = self.create_timer(period, self._publish)

        cycle_sizes = [self._size_for_token(token) for token in self.pattern]
        avg_size = sum(cycle_sizes) / len(cycle_sizes)
        bandwidth_bits = avg_size * 8 * rate
        bandwidth_bytes = avg_size * rate

        stream_suffix = f" x{streams} streams" if streams > 1 else ""
        topics_desc = ", ".join(s["topic"] for s in self.streams_state)

        if self.uses_pattern:
            cycle_desc = ", ".join(f"{token}:{self._size_for_token(token)} B" for token in self.pattern)
            self.get_logger().info(
                f"Publishing [{topics_desc}]{stream_suffix} at {rate} Hz, "
                f"pattern '{self._pattern_as_string()}' -> [{cycle_desc}]. "
                f"Average payload {_human_bytes(avg_size)}. "
                f"Average bandwidth per stream: {_human_bits(bandwidth_bits)}/s, {_human_bytes(bandwidth_bytes)}/s"
            )
        else:
            self.get_logger().info(
                f"Publishing [{topics_desc}]{stream_suffix} at {rate} Hz, "
                f"payload {_human_bytes(self.size_a)}. "
                f"Bandwidth per stream: {_human_bits(bandwidth_bits)}/s, {_human_bytes(bandwidth_bytes)}/s"
            )

    def _size_for_token(self, token: str) -> int:
        if token == "a":
            return self.size_a
        if token == "b":
            return self.size_b
        raise ValueError(f"Unsupported pattern token '{token}'.")

    def _pattern_as_string(self) -> str:
        compressed: list[str] = []
        current = self.pattern[0]
        count = 1

        for token in self.pattern[1:]:
            if token == current:
                count += 1
                continue
            compressed.append(f"{current}*{count}")
            current = token
            count = 1

        compressed.append(f"{current}*{count}")
        return ",".join(compressed)

    def _publish(self):
        for stream in self.streams_state:
            size = self._size_for_token(self.pattern[stream["seq"] % len(self.pattern)])
            msg = SizedPayload()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.seq = stream["seq"]
            msg.size = size
            msg.payload = list(b"\x42" * size)
            stream["publisher"].publish(msg)
            stream["seq"] += 1

        total_seq = sum(s["seq"] for s in self.streams_state)
        if total_seq % 100 == 0 and total_seq > 0:
            self.get_logger().debug(f"Published {total_seq} total messages across {len(self.streams_state)} stream(s)")


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
