from __future__ import annotations

import json
import sqlite3
import struct
import zlib
from pathlib import Path

import pytest
import yaml

from rosotacom.video_quality import (
    VideoFrame,
    compare_frame_sequences,
    parse_sensor_compressed_image_cdr,
    parse_sensor_image_cdr,
    psnr_db,
    read_frame_manifest,
    read_rosbag_frames,
    ssim,
    threshold_failures,
    write_frame_manifest,
    write_synthetic_pair,
)


def _frame(index: int, pts: int | None, data: bytes) -> VideoFrame:
    return VideoFrame(
        index=index,
        pts=pts,
        width=2,
        height=2,
        encoding="mono8",
        channels=1,
        data=data,
    )


def _cdr_string(offset: int, value: str) -> tuple[bytes, int]:
    pad = (-offset) % 4
    raw = value.encode() + b"\x00"
    chunk = b"\x00" * pad + struct.pack("<I", len(raw)) + raw
    return chunk, offset + len(chunk)


def _image_cdr(
    *,
    width: int,
    height: int,
    encoding: str,
    data: bytes,
    step: int | None = None,
    stamp_sec: int = 1,
    stamp_nanosec: int = 2,
) -> bytes:
    step = step or len(data) // height
    body = b""
    offset = 0
    body += struct.pack("<iI", stamp_sec, stamp_nanosec)
    offset += 8
    chunk, offset = _cdr_string(offset, "camera")
    body += chunk
    pad = (-offset) % 4
    body += b"\x00" * pad + struct.pack("<II", height, width)
    offset += pad + 8
    chunk, offset = _cdr_string(offset, encoding)
    body += chunk
    body += b"\x00"
    offset += 1
    pad = (-offset) % 4
    body += b"\x00" * pad + struct.pack("<I", step) + struct.pack("<I", len(data)) + data
    return b"\x00\x01\x00\x00" + body


def _png_rgb(width: int, height: int, data: bytes) -> bytes:
    if len(data) != width * height * 3:
        raise ValueError("RGB data length does not match width*height*3")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\x00" + data[y * width * 3 : (y + 1) * width * 3] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(
            b"IDAT",
            zlib.compress(rows),
        )
        + chunk(b"IEND", b"")
    )


def _compressed_image_cdr(
    *,
    format_: str,
    data: bytes,
    stamp_sec: int = 1,
    stamp_nanosec: int = 2,
) -> bytes:
    body = b""
    offset = 0
    body += struct.pack("<iI", stamp_sec, stamp_nanosec)
    offset += 8
    chunk, offset = _cdr_string(offset, "camera")
    body += chunk
    chunk, offset = _cdr_string(offset, format_)
    body += chunk
    pad = (-offset) % 4
    body += b"\x00" * pad + struct.pack("<I", len(data)) + data
    return b"\x00\x01\x00\x00" + body


def test_metric_math_on_tiny_arrays() -> None:
    assert psnr_db(bytes([0, 255]), bytes([0, 255])) == float("inf")
    assert ssim(bytes([0, 255]), bytes([0, 255])) == pytest.approx(1.0)

    assert psnr_db(bytes([10, 20, 30, 40]), bytes([10, 20, 30, 44])) == pytest.approx(42.110204, abs=1e-6)
    assert 0.99 < ssim(bytes([10, 20, 30, 40]), bytes([10, 20, 30, 44])) < 1.0


def test_pts_alignment_reports_lost_frames_separately() -> None:
    reference = [
        _frame(0, 10, bytes([10, 20, 30, 40])),
        _frame(1, 20, bytes([50, 60, 70, 80])),
        _frame(2, 30, bytes([90, 100, 110, 120])),
    ]
    degraded = [
        _frame(0, 10, bytes([10, 20, 30, 41])),
        _frame(1, 30, bytes([92, 101, 109, 121])),
        _frame(2, 40, bytes([0, 0, 0, 0])),
    ]

    report = compare_frame_sequences(reference, degraded, align="pts")

    assert report["alignment"] == "pts"
    assert report["delivery"] == {
        "reference_frames": 3,
        "degraded_frames": 3,
        "compared_frames": 2,
        "lost_frames": 1,
        "extra_degraded_frames": 1,
        "delivery_ratio": pytest.approx(0.666667),
        "loss_pct": pytest.approx(33.333333),
    }
    assert report["frames"][1]["status"] == "lost"
    assert report["frames"][1]["psnr_db"] is None
    assert report["extra_degraded_frames"][0]["pts"] == 40


def test_index_alignment_handles_missing_tail_without_pts() -> None:
    reference = [_frame(0, None, bytes([1, 2, 3, 4])), _frame(1, None, bytes([5, 6, 7, 8]))]
    degraded = [_frame(0, None, bytes([1, 2, 3, 5]))]

    report = compare_frame_sequences(reference, degraded, align="auto")

    assert report["alignment"] == "index"
    assert report["delivery"]["compared_frames"] == 1
    assert report["delivery"]["lost_frames"] == 1


def test_parse_sensor_image_cdr_strips_padding_and_canonicalizes_bgr() -> None:
    # Two BGR pixels per row, plus one padding byte at the end of each row.
    data = bytes([1, 2, 3, 4, 5, 6, 99, 7, 8, 9, 10, 11, 12, 88])
    frame = parse_sensor_image_cdr(_image_cdr(width=2, height=2, encoding="bgr8", data=data, step=7))

    assert frame.pts == 1_000_000_002
    assert frame.encoding == "rgb8"
    assert frame.channels == 3
    assert frame.data == bytes([3, 2, 1, 6, 5, 4, 9, 8, 7, 12, 11, 10])


def test_parse_sensor_compressed_image_cdr_decodes_png() -> None:
    pixels = bytes([255, 0, 0, 0, 255, 0])
    frame = parse_sensor_compressed_image_cdr(
        _compressed_image_cdr(format_="png", data=_png_rgb(2, 1, pixels)),
    )

    assert frame.pts == 1_000_000_002
    assert frame.encoding == "rgb8"
    assert frame.channels == 3
    assert frame.data == pixels


def test_sqlite_rosbag_reader_loads_sensor_image_topic(tmp_path: Path) -> None:
    bag = tmp_path / "bag"
    bag.mkdir()
    cdr = _image_cdr(width=2, height=2, encoding="mono8", data=bytes([1, 2, 3, 4]))
    conn = sqlite3.connect(bag / "bag_0.db3")
    try:
        conn.execute(
            "create table topics (id integer primary key, name text, type text, serialization_format text, "
            "offered_qos_profiles text)"
        )
        conn.execute("create table messages (id integer primary key, topic_id integer, timestamp integer, data blob)")
        conn.execute(
            "insert into topics values (1, '/camera/image', 'sensor_msgs/msg/Image', 'cdr', '')",
        )
        conn.execute("insert into messages values (1, 1, 123, ?)", (cdr,))
        conn.commit()
    finally:
        conn.close()
    (bag / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "rosbag2_bagfile_information": {
                    "version": 5,
                    "storage_identifier": "sqlite3",
                    "relative_file_paths": ["bag_0.db3"],
                    "topics_with_message_count": [
                        {
                            "topic_metadata": {
                                "name": "/camera/image",
                                "type": "sensor_msgs/msg/Image",
                                "serialization_format": "cdr",
                            },
                            "message_count": 1,
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    frames = read_rosbag_frames(bag, "/camera/image")

    assert len(frames) == 1
    assert frames[0].data == bytes([1, 2, 3, 4])
    assert frames[0].recorded_time_ns == 123


def test_sqlite_rosbag_reader_loads_compressed_image_topic(tmp_path: Path) -> None:
    bag = tmp_path / "bag"
    bag.mkdir()
    pixels = bytes([255, 0, 0, 0, 255, 0])
    cdr = _compressed_image_cdr(format_="png", data=_png_rgb(2, 1, pixels))
    conn = sqlite3.connect(bag / "bag_0.db3")
    try:
        conn.execute(
            "create table topics (id integer primary key, name text, type text, serialization_format text, "
            "offered_qos_profiles text)"
        )
        conn.execute("create table messages (id integer primary key, topic_id integer, timestamp integer, data blob)")
        conn.execute(
            "insert into topics values (1, '/camera/image/compressed', 'sensor_msgs/msg/CompressedImage', 'cdr', '')",
        )
        conn.execute("insert into messages values (1, 1, 456, ?)", (cdr,))
        conn.commit()
    finally:
        conn.close()
    (bag / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "rosbag2_bagfile_information": {
                    "version": 5,
                    "storage_identifier": "sqlite3",
                    "relative_file_paths": ["bag_0.db3"],
                    "topics_with_message_count": [
                        {
                            "topic_metadata": {
                                "name": "/camera/image/compressed",
                                "type": "sensor_msgs/msg/CompressedImage",
                                "serialization_format": "cdr",
                            },
                            "message_count": 1,
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    frames = read_rosbag_frames(bag, "/camera/image/compressed")

    assert len(frames) == 1
    assert frames[0].encoding == "rgb8"
    assert frames[0].data == pixels
    assert frames[0].recorded_time_ns == 456


def test_frame_manifest_and_synthetic_pair_are_comparable(tmp_path: Path) -> None:
    ref_path, degraded_path = write_synthetic_pair(
        tmp_path,
        frames=5,
        width=4,
        height=4,
        quantization_step=4,
        drop_every=5,
    )

    report = compare_frame_sequences(read_frame_manifest(ref_path), read_frame_manifest(degraded_path), align="pts")

    assert report["delivery"]["reference_frames"] == 5
    assert report["delivery"]["lost_frames"] == 1
    assert not threshold_failures(report, min_mean_psnr=20.0, max_loss_pct=25.0)
    assert threshold_failures(report, max_loss_pct=0.0)

    roundtrip = tmp_path / "roundtrip.json"
    write_frame_manifest([_frame(0, 1, bytes([1, 2, 3, 4]))], roundtrip)
    assert json.loads(roundtrip.read_text(encoding="utf-8"))["frames"][0]["pts"] == 1
