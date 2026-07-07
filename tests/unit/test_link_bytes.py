"""Unit tests for the link-byte sampler (RFC 0002 'link overhead').

link_bytes is pure Python (no rclpy), loaded by file path like the other
in-container modules so the host suite can exercise it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
LINK_BYTES_PY = REPO_ROOT / "src" / "rosotacom" / "resources" / "ws" / "ros2src" / "com_py" / "com_py" / "link_bytes.py"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lb = _load(LINK_BYTES_PY, "rosotacom_link_bytes")

# /proc/net/dev: two header rows (no ":", skipped), then lo + a tunnel interface.
# Columns: rx_bytes rx_pkts rx_errs rx_drop rx_fifo rx_frame rx_comp rx_mcast
#          tx_bytes tx_pkts tx_errs tx_drop tx_fifo tx_colls tx_carrier tx_comp
PROC_NET_DEV = """\
Inter-|   Receive                |  Transmit
 face |bytes    packets ... |bytes    packets ...
    lo: 123456 900 0 0 0 0 0 0 123456 900 0 0 0 0 0 0
  tun1: 1000000 5000 0 0 0 0 0 0 2000000 6000 0 0 0 0 0 0
"""


def test_parse_proc_net_dev_extracts_rx_tx() -> None:
    stats = lb.parse_proc_net_dev(PROC_NET_DEV)
    assert stats["tun1"] == {
        "rx_bytes": 1000000,
        "rx_packets": 5000,
        "tx_bytes": 2000000,
        "tx_packets": 6000,
    }
    assert stats["lo"]["rx_bytes"] == 123456


def test_parse_skips_headers_and_garbage() -> None:
    stats = lb.parse_proc_net_dev("garbage\n  bad: not numbers here\n" + PROC_NET_DEV)
    assert set(stats) == {"lo", "tun1"}


def test_kbps_conversion() -> None:
    # 1024 bytes over 1s == 8 kbit/s
    assert lb.kbps(1024, 1.0) == 8.0
    assert lb.kbps(100, 0.0) == 0.0


def _sampler_over(frames: list[str], times: list[float]):
    """A sampler whose /proc read and clock are driven by fixtures."""
    state = {"i": 0}

    def fake_read(iface, proc_path):  # noqa: ARG001
        stats = lb.parse_proc_net_dev(frames[state["i"]])
        return stats.get(iface)

    clock = {"i": 0}

    def fake_clock():
        t = times[clock["i"]]
        return t

    s = lb.LinkByteSampler("tun1", clock=fake_clock)
    # Patch the module-level reader the sampler uses.
    orig = lb.read_iface_counter_row
    lb.read_iface_counter_row = fake_read
    try:
        results = []
        for _ in frames:
            results.append(s.sample())
            state["i"] += 1
            clock["i"] += 1
    finally:
        lb.read_iface_counter_row = orig
    return results


def test_sampler_first_call_primes_then_reports_rate() -> None:
    frame0 = PROC_NET_DEV
    # +1024 rx, +2048 tx after 1 second.
    frame1 = PROC_NET_DEV.replace("1000000 5000", "1001024 5001").replace("2000000 6000", "2002048 6001")
    results = _sampler_over([frame0, frame1], [10.0, 11.0])
    assert results[0] is None  # primed
    assert results[1]["rx_kbps"] == lb.kbps(1024, 1.0)
    assert results[1]["tx_kbps"] == lb.kbps(2048, 1.0)
    assert results[1]["rx_packets_delta"] == 1
    assert results[1]["tx_packets_delta"] == 1
    assert results[1]["interface"] == "tun1"


def test_sampler_counter_reset_is_skipped() -> None:
    frame0 = PROC_NET_DEV
    frame1 = PROC_NET_DEV.replace("1000000", "10").replace("2000000", "20")  # counters dropped (reset)
    results = _sampler_over([frame0, frame1], [10.0, 11.0])
    assert results[1] is None


IP_ADDR_OUT = (
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
    "5: tun1    inet 10.254.0.32/24 scope global tun1\\       valid_lft forever preferred_lft forever\n"
    "2: eth0    inet 172.17.0.2/16 scope global eth0\\       valid_lft forever preferred_lft forever\n"
)


def test_parse_ip_addr_resolves_interface_by_address() -> None:
    assert lb.parse_ip_addr_for_ip(IP_ADDR_OUT, "10.254.0.32") == "tun1"
    assert lb.parse_ip_addr_for_ip(IP_ADDR_OUT, "127.0.0.1") == "lo"
    assert lb.parse_ip_addr_for_ip(IP_ADDR_OUT, "172.17.0.2") == "eth0"


def test_parse_ip_addr_unknown_or_empty() -> None:
    assert lb.parse_ip_addr_for_ip(IP_ADDR_OUT, "10.0.0.99") is None
    assert lb.parse_ip_addr_for_ip(IP_ADDR_OUT, "") is None
