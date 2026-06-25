#!/usr/bin/env python3

from __future__ import annotations

import argparse
import array
import json
import sys
from pathlib import Path

import rosbag2_py
import yaml
from rclpy.serialization import deserialize_message, serialize_message
from rosidl_runtime_py.utilities import get_message


def anonymize_msg(msg) -> None:
    """Recursively anonymize message fields in place, preserving structures and sizes."""
    if not hasattr(msg, "__slots__"):
        return

    for slot in msg.__slots__:
        val = getattr(msg, slot)
        if val is None:
            continue

        val_type_name = type(val).__name__
        val_module = getattr(type(val), "__module__", "")
        if val_type_name in ("Time", "Duration") and val_module.startswith("builtin_interfaces"):
            continue

        if hasattr(val, "__slots__"):
            anonymize_msg(val)
        elif isinstance(val, (list, tuple)):
            if isinstance(val, tuple):
                mut_val = list(val)
                for i in range(len(mut_val)):
                    if hasattr(mut_val[i], "__slots__"):
                        anonymize_msg(mut_val[i])
                    elif isinstance(mut_val[i], str):
                        mut_val[i] = "x" * len(mut_val[i])
                    elif isinstance(mut_val[i], (int, float)):
                        mut_val[i] = type(mut_val[i])(0)
                    elif isinstance(mut_val[i], bool):
                        mut_val[i] = False
                setattr(msg, slot, tuple(mut_val))
            else:
                for i in range(len(val)):
                    if hasattr(val[i], "__slots__"):
                        anonymize_msg(val[i])
                    elif isinstance(val[i], str):
                        val[i] = "x" * len(val[i])
                    elif isinstance(val[i], (int, float)):
                        val[i] = type(val[i])(0)
                    elif isinstance(val[i], bool):
                        val[i] = False
        elif isinstance(val, (bytes, bytearray)):
            setattr(msg, slot, type(val)(len(val)))
        elif isinstance(val, array.array):
            setattr(msg, slot, array.array(val.typecode, [0] * len(val)))
        elif isinstance(val, str):
            setattr(msg, slot, "x" * len(val))
        elif isinstance(val, bool):
            setattr(msg, slot, False)
        elif isinstance(val, (int, float)):
            setattr(msg, slot, type(val)(0))


def get_topics_info(metadata_path: Path) -> dict[str, dict]:
    if metadata_path.is_dir():
        metadata_path = metadata_path / "metadata.yaml"
    with open(metadata_path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    info = doc.get("rosbag2_bagfile_information", {})
    topics_info = {}
    for entry in info.get("topics_with_message_count", []):
        meta = entry.get("topic_metadata", {})
        name = meta.get("name")
        if name:
            topics_info[name] = {
                "type": meta.get("type"),
                "serialization_format": meta.get("serialization_format", "cdr"),
                "offered_qos_profiles": meta.get("offered_qos_profiles", []),
            }
    return topics_info


def main() -> int:
    parser = argparse.ArgumentParser(description="Anonymize a rosbag's messages and topics.")
    parser.add_argument("--input-bag", required=True, help="Path to input bag directory")
    parser.add_argument("--output-bag", required=True, help="Path to output bag directory")
    parser.add_argument("--topics-map", required=True, help="JSON string mapping original to anonymized topics")
    parser.add_argument("--storage-id", default="mcap", help="Rosbag2 storage identifier")
    args = parser.parse_args()

    input_path = Path(args.input_bag)
    output_path = Path(args.output_bag)
    topics_map = json.loads(args.topics_map)

    if not input_path.exists():
        print(f"Error: input bag '{input_path}' does not exist", file=sys.stderr)
        return 1

    topics_info = get_topics_info(input_path)

    missing_topics = [orig for orig in topics_map if orig not in topics_info]
    if missing_topics:
        print("Error: mapped topic(s) missing from the input bag:", file=sys.stderr)
        for topic in missing_topics:
            print(f"  {topic}", file=sys.stderr)
        return 1
    active_topics_map = dict(topics_map)

    # Initialize sequential reader
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(input_path), storage_id=args.storage_id),
        rosbag2_py.ConverterOptions("", ""),
    )

    # Initialize sequential writer
    writer = rosbag2_py.SequentialWriter()
    writer.open(
        rosbag2_py.StorageOptions(uri=str(output_path), storage_id=args.storage_id),
        rosbag2_py.ConverterOptions("", ""),
    )

    # Register output topics with the writer
    for orig, anonymized in active_topics_map.items():
        info = topics_info[orig]
        topic_metadata = rosbag2_py.TopicMetadata(
            id=0,
            name=anonymized,
            type=info["type"],
            serialization_format=info["serialization_format"],
            offered_qos_profiles=info["offered_qos_profiles"],
        )
        writer.create_topic(topic_metadata)

    # Read and anonymize messages
    print(f"Anonymizing bag: {input_path} -> {output_path}")
    count = 0
    while reader.has_next():
        topic, serialized_bytes, timestamp = reader.read_next()
        if topic in active_topics_map:
            anonymized_topic = active_topics_map[topic]
            msg_type = topics_info[topic]["type"]
            msg_cls = get_message(msg_type)
            msg = deserialize_message(serialized_bytes, msg_cls)
            anonymize_msg(msg)
            anonymized_bytes = serialize_message(msg)
            writer.write(anonymized_topic, anonymized_bytes, timestamp)
            count += 1

    print(f"Successfully wrote {count} anonymized messages to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
