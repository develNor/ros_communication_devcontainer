"""Unit tests for the replay bag ground-truth + contract calibration (RFC 0002)."""

from __future__ import annotations

from pathlib import Path

from rosotacom.bag_ground_truth import (
    bag_ground_truth,
    validate_expect_against_bag,
)

# A minimal rosbag2 metadata.yaml: a fast stream, a 1 Hz static, and a held topic.
METADATA = """\
rosbag2_bagfile_information:
  duration:
    nanoseconds: 100000000000
  starting_time:
    nanoseconds_since_epoch: 0
  message_count: 10849
  topics_with_message_count:
    - topic_metadata:
        name: /tf
        type: tf2_msgs/msg/TFMessage
        offered_qos_profiles:
          - {reliability: reliable, durability: volatile}
      message_count: 10748
    - topic_metadata:
        name: /site
        type: std_msgs/msg/String
        offered_qos_profiles:
          - {reliability: reliable, durability: volatile}
      message_count: 101
    - topic_metadata:
        name: /tf_static
        type: tf2_msgs/msg/TFMessage
        offered_qos_profiles:
          - {reliability: reliable, durability: transient_local}
      message_count: 2
"""


def _write_bag(tmp_path: Path) -> Path:
    (tmp_path / "metadata.yaml").write_text(METADATA, encoding="utf-8")
    return tmp_path


def test_ground_truth_parses_counts_rates_and_qos(tmp_path: Path) -> None:
    gt = bag_ground_truth(_write_bag(tmp_path))
    assert set(gt) == {"/tf", "/site", "/tf_static"}
    assert gt["/tf"]["count"] == 10748
    assert round(gt["/tf"]["native_hz"], 0) == 107  # (10748-1)/100s
    assert gt["/site"]["native_hz"] == 1.0  # (101-1)/100s
    assert gt["/tf_static"]["durability"] == "transient_local"
    assert gt["/tf_static"]["msg_type"] == "tf2_msgs/msg/TFMessage"


def test_accepts_bag_directory_or_metadata_path(tmp_path: Path) -> None:
    _write_bag(tmp_path)
    assert bag_ground_truth(tmp_path) == bag_ground_truth(tmp_path / "metadata.yaml")


def test_validate_flags_hz_floor_above_native_rate(tmp_path: Path) -> None:
    gt = bag_ground_truth(_write_bag(tmp_path))
    # /site is a 1 Hz topic; an hz.min of 5 can never be delivered (OTA only thins).
    warnings = validate_expect_against_bag({"/site": {"hz": {"min": 5}}}, gt)
    assert len(warnings) == 1 and "/site" in warnings[0] and "native rate" in warnings[0]


def test_validate_passes_satisfiable_floor(tmp_path: Path) -> None:
    gt = bag_ground_truth(_write_bag(tmp_path))
    # /tf is 107 Hz native; a floor of 10 (post-decimation) is fine.
    assert validate_expect_against_bag({"/tf": {"hz": {"min": 10}}}, gt) == []


def test_validate_flags_static_topic_described_as_stream(tmp_path: Path) -> None:
    gt = bag_ground_truth(_write_bag(tmp_path))
    # /tf_static is published 2x transient_local; a stream hz contract misdescribes it
    # (it also trips the unsatisfiable-floor check, since 2 msgs/100s is ~0 Hz).
    warnings = validate_expect_against_bag({"/tf_static": {"hz": {"min": 1}}}, gt)
    assert any("mode: latched" in w for w in warnings)


def test_validate_latched_static_has_no_warning(tmp_path: Path) -> None:
    gt = bag_ground_truth(_write_bag(tmp_path))
    assert validate_expect_against_bag({"/tf_static": {"mode": "latched"}}, gt) == []


def test_validate_ignores_topics_absent_from_bag(tmp_path: Path) -> None:
    gt = bag_ground_truth(_write_bag(tmp_path))
    assert validate_expect_against_bag({"/not_in_bag": {"hz": {"min": 999}}}, gt) == []
