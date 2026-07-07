from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WS_PY = REPO_ROOT / "src" / "rosotacom" / "resources" / "ws" / "ros2src" / "com_py"
sys.path.insert(0, str(WS_PY))

from com_py.link_bytes import LinkByteSampler, parse_proc_net_dev  # noqa: E402
from com_py.link_trace import (  # noqa: E402
    LinkTraceRecorder,
    build_link_trace_sample,
    run_modem_metrics_hook,
)


def _proc_net_dev(rx_bytes: int, rx_packets: int, tx_bytes: int, tx_packets: int) -> str:
    return "\n".join(
        [
            "Inter-|   Receive                                                |  Transmit",
            " face |bytes    packets errs drop fifo frame compressed multicast|"
            "bytes    packets errs drop fifo colls carrier compressed",
            f"  eth0:{rx_bytes:8d} {rx_packets:7d}    0    0    0     0          0         0 "
            f"{tx_bytes:8d} {tx_packets:7d}    0    0    0     0       0          0",
            "",
        ]
    )


def test_link_byte_sampler_reports_packet_and_byte_deltas(tmp_path: Path) -> None:
    proc_path = tmp_path / "dev"
    clock_values = iter([10.0, 12.0])
    sampler = LinkByteSampler("eth0", proc_path=str(proc_path), clock=lambda: next(clock_values))

    proc_path.write_text(_proc_net_dev(1_000, 10, 2_000, 20), encoding="utf-8")
    assert sampler.sample() is None

    proc_path.write_text(_proc_net_dev(2_000, 15, 3_500, 27), encoding="utf-8")
    sample = sampler.sample()

    assert sample is not None
    assert sample["interface"] == "eth0"
    assert sample["rx_bytes_delta"] == 1_000
    assert sample["tx_bytes_delta"] == 1_500
    assert sample["rx_packets_delta"] == 5
    assert sample["tx_packets_delta"] == 7
    assert sample["rx_kbps"] == pytest.approx(3.90625)
    assert sample["tx_kbps"] == pytest.approx(5.859375)


def test_parse_proc_net_dev_exposes_packet_counters() -> None:
    stats = parse_proc_net_dev(_proc_net_dev(123, 4, 567, 8))
    assert stats["eth0"] == {
        "rx_bytes": 123,
        "rx_packets": 4,
        "tx_bytes": 567,
        "tx_packets": 8,
    }


def test_build_link_trace_sample_marks_passive_rates_as_observed() -> None:
    row = build_link_trace_sample(
        {
            "generated_at": "2026-07-07T10:00:00.000",
            "peer": "a",
            "remote": "b",
            "clock_sync": {"rtt_ms": 12.5, "peer_offset_ms": -1.25, "assumption": "symmetric_path"},
            "topics": [
                {
                    "stages": [
                        {"stage": "com_in", "loss_pct": 2.5},
                    ],
                }
            ],
        },
        {
            "interface": "eth0",
            "window_s": 1.2345678,
            "rx_bytes_delta": 100,
            "tx_bytes_delta": 200,
            "rx_packets_delta": 3,
            "tx_packets_delta": 4,
            "rx_kbps": 0.8,
            "tx_kbps": 1.6,
        },
        modem_metrics={"available": True, "provenance": "modem_metrics_command", "metrics": {"rsrp_dbm": -88}},
        monotonic_s=42.1234567,
    )

    assert row["schema_version"] == 1
    assert row["kind"] == "link_trace"
    assert row["peer"] == "a"
    assert row["passive_counter_delta"]["provenance"] == "proc_net_dev_counter_delta"
    assert row["passive_counter_delta"]["observed_not_available_bandwidth"] is True
    assert row["passive_counter_delta"]["rx"]["bytes_delta"] == 100
    assert row["peer_probe"]["provenance"] == "echo_heartbeat_status_snapshot"
    assert row["peer_probe"]["rtt_ms"] == 12.5
    assert row["peer_probe"]["loss_pct"] == 2.5
    assert row["modem"]["metrics"]["rsrp_dbm"] == -88


def test_modem_metrics_hook_requires_json_object() -> None:
    def good_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 0, '{"rsrp_dbm": -90}', "")

    def bad_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 0, "[1, 2]", "")

    assert run_modem_metrics_hook("echo {}", runner=good_runner)["metrics"] == {"rsrp_dbm": -90}
    bad = run_modem_metrics_hook("echo []", runner=bad_runner)
    assert bad["available"] is False
    assert "not an object" in bad["reason"]


def test_link_trace_recorder_appends_jsonl_on_interval(tmp_path: Path) -> None:
    clock_values = iter([10.0, 10.5, 11.0])
    trace_path = tmp_path / "status" / "link_trace.jsonl"
    recorder = LinkTraceRecorder(str(trace_path), interval_s=1.0, clock=lambda: next(clock_values))
    snapshot = {"generated_at": "2026-07-07T10:00:00.000", "peer": "a", "remote": "b", "topics": []}

    assert recorder.maybe_write(snapshot, None) is True
    assert recorder.maybe_write(snapshot, None) is False
    assert recorder.maybe_write(snapshot, None) is True

    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert [row["kind"] for row in rows] == ["link_trace", "link_trace"]
