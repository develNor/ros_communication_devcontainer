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
# \date    2026-02-25
#
# ---------------------------------------------------------------------

from __future__ import annotations

"""
Latch relay node ("latch_relay").

Subscribes to topics and only republishes when the serialized message content
changes compared to the last published message.  This converts high-frequency
"status" topics (e.g. published at 10 Hz but rarely changing value) into
publish-on-change semantics, drastically reducing unnecessary OTA traffic.

Designed to pair with transient_local QoS on the publisher side so that new
subscribers immediately receive the latest value without waiting for the next
change.

Typical usage in a session template:
    processing:
      latch: true
    qos:
      reliability: reliable
      durability: transient_local
      for_role:
        latch_sub:
          durability: volatile     # match the original publisher
"""

from typing import List

import rclpy
from rclpy.node import Node
from rclpy.serialization import serialize_message

from com_py.pair_management import PairRefreshMixin
from com_py.pub_sub_pair import PubSubPair
from com_py.qos import load_qos_config


class LatchPubSubPair(PubSubPair):
    """PubSubPair that only forwards messages whose serialized bytes differ from
    the previously published message (content-based deduplication)."""

    def __init__(self, *, node, **kwargs):
        self._last_bytes: bytes | None = None
        self._suppressed: int = 0
        super().__init__(node=node, **kwargs)

    def _callback(self, msg):
        try:
            current_bytes = serialize_message(msg)
        except Exception as exc:
            # If serialization fails, always forward (safe fallback).
            self.logger.warning(
                f"[LatchPubSubPair] serialize_message failed for sub='{self.sub_topic}': {exc}. "
                "Forwarding message unconditionally."
            )
            self.publisher.publish(msg)
            return

        if current_bytes == self._last_bytes:
            self._suppressed += 1
            return  # No change — skip publishing.

        self._last_bytes = current_bytes

        if self.first_msg:
            self.logger.info(
                f"[LatchPubSubPair] FIRST msg on sub='{self.sub_topic}' => pub='{self.pub_topic}'"
            )
            self.first_msg = False
        elif self._suppressed > 0:
            self.logger.debug(
                f"[LatchPubSubPair] Change detected on sub='{self.sub_topic}' "
                f"(suppressed {self._suppressed} duplicate(s))"
            )

        self._suppressed = 0
        self.publisher.publish(msg)


class LatchRelay(Node, PairRefreshMixin):
    """Node that manages multiple LatchPubSubPair instances."""

    def __init__(self) -> None:
        super().__init__("latch_relay")

        # -----------------------
        # Parameters
        # -----------------------
        self.topic_suffix = self.declare_parameter("topic_suffix", "/latched").value
        self.qos_config_file = self.declare_parameter("qos_config_file", "").value
        self.sub_role = "latch_sub"
        self.pub_role = "latch_pub"

        self.latch_topics: list[str] = (
            self.declare_parameter("latch_topics", rclpy.Parameter.Type.STRING_ARRAY).value
            or []
        )

        # QoS
        self.qos_config = load_qos_config(self.get_logger(), self.qos_config_file)

        # -----------------------
        # Startup logging
        # -----------------------
        self.get_logger().info("=== LatchRelay configuration ===")
        self.get_logger().info(f"topic_suffix='{self.topic_suffix}'")
        self.get_logger().info(f"latch_topics={self.latch_topics}")

        # -----------------------
        # Build pairs
        # -----------------------
        self.pairs: List[PubSubPair] = []

        for base in self.latch_topics:
            self.pairs.append(
                LatchPubSubPair(
                    node=self,
                    base_topic_name=base,
                    sub_topic=base,
                    pub_topic=f"{base}{self.topic_suffix}",
                    sub_role=self.sub_role,
                    pub_role=self.pub_role,
                    qos_config=self.qos_config,
                )
            )

        self.init_pair_refresh(period_s=5.0, log_prefix="LatchRelay")


def main(args=None) -> None:
    rclpy.init(args=args)
    node = LatchRelay()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("Shutting down LatchRelay …")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
