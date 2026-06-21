"""Lightweight link-byte measurement from the kernel's own interface counters.

The OTA link runs over a (typically dedicated) network interface -- a VPN tunnel
such as ``tun1`` in the cross-host setup. The kernel already counts every byte
that crosses it in ``/proc/net/dev`` (``rx_bytes`` / ``tx_bytes``), so measuring
link traffic needs no packet capture, no ``tshark``, and no privileges: we read
two counters, diff them over a window, and divide by elapsed time.

This replaces the ``sudo tshark`` path in ``topic_monitor`` (see RFC 0002
"link overhead"): the status overview already measures the *ROS payload*
bandwidth per stage, and this module supplies the *wire* bandwidth, so the two
together give the overhead ratio ``link / payload`` (=~1 healthy; >>1 means
retransmits, shadow connections, or a bad RMW/QoS).

Pure parsing is split out (``parse_proc_net_dev``) so it is unit-testable against
a fixture without touching the real filesystem.
"""

from __future__ import annotations

import subprocess
import time
from typing import Dict, Optional, Tuple

PROC_NET_DEV = "/proc/net/dev"

# Field order on each data row of /proc/net/dev, after "<iface>:".
# Receive: bytes packets errs drop fifo frame compressed multicast
# Transmit: bytes packets errs drop fifo colls carrier compressed
_RX_BYTES_IDX = 0
_TX_BYTES_IDX = 8


def parse_proc_net_dev(text: str) -> Dict[str, Dict[str, int]]:
    """Parse /proc/net/dev contents -> {iface: {'rx_bytes', 'tx_bytes'}}."""
    out: Dict[str, Dict[str, int]] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue  # the two header rows have no "<iface>:"
        name, _, rest = line.partition(":")
        iface = name.strip()
        fields = rest.split()
        if len(fields) <= _TX_BYTES_IDX:
            continue
        try:
            out[iface] = {
                "rx_bytes": int(fields[_RX_BYTES_IDX]),
                "tx_bytes": int(fields[_TX_BYTES_IDX]),
            }
        except ValueError:
            continue
    return out


def parse_ip_addr_for_ip(text: str, ip: str) -> Optional[str]:
    """Find the interface owning ``ip`` in `ip -o addr show` output.

    Each line looks like: ``5: tun1    inet 10.254.0.32/24 scope global tun1 ...``
    We resolve the interface by address rather than a brittle shell regex so the
    OTA link interface (e.g. the VPN tunnel) is found reliably from the peer's
    own ip_local, with no dependence on catmux/shell variable expansion.
    """
    if not ip:
        return None
    for line in text.splitlines():
        parts = line.split()
        if "inet" not in parts and "inet6" not in parts:
            continue
        for key in ("inet", "inet6"):
            if key in parts:
                idx = parts.index(key)
                if idx + 1 < len(parts) and parts[idx + 1].split("/")[0] == ip:
                    return parts[1]
    return None


def find_interface_for_ip(ip: str) -> Optional[str]:
    """The interface owning ``ip``, via `ip -o addr show` (None if not found)."""
    if not ip:
        return None
    try:
        out = subprocess.run(
            ["ip", "-o", "addr", "show"], capture_output=True, text=True, timeout=5, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_ip_addr_for_ip(out, ip)


def read_iface_counters(iface: str, proc_path: str = PROC_NET_DEV) -> Optional[Tuple[int, int]]:
    """Return (rx_bytes, tx_bytes) for ``iface`` now, or None if unavailable."""
    try:
        with open(proc_path, encoding="utf-8") as fp:
            stats = parse_proc_net_dev(fp.read())
    except OSError:
        return None
    row = stats.get(iface)
    if row is None:
        return None
    return row["rx_bytes"], row["tx_bytes"]


def kbps(delta_bytes: int, elapsed_s: float) -> float:
    """Bytes over a window -> kilobits per second (matches topic_monitor units)."""
    if elapsed_s <= 0:
        return 0.0
    return (delta_bytes * 8.0 / 1024.0) / elapsed_s


class LinkByteSampler:
    """Diffs interface byte counters between calls to report rx/tx Kbit/s.

    The first ``sample`` only primes the baseline (returns None); each later call
    returns the rate since the previous call. Counter wraps (negative deltas) are
    treated as no data for that window rather than reported as a spike.
    """

    def __init__(self, iface: str, proc_path: str = PROC_NET_DEV, clock=time.monotonic) -> None:
        self.iface = iface
        self._proc_path = proc_path
        self._clock = clock
        self._last_t: Optional[float] = None
        self._last_rx: Optional[int] = None
        self._last_tx: Optional[int] = None

    def sample(self) -> Optional[Dict[str, float]]:
        counters = read_iface_counters(self.iface, self._proc_path)
        if counters is None:
            return None
        rx, tx = counters
        now = self._clock()
        prev_t, prev_rx, prev_tx = self._last_t, self._last_rx, self._last_tx
        self._last_t, self._last_rx, self._last_tx = now, rx, tx
        if prev_t is None or prev_rx is None or prev_tx is None:
            return None
        elapsed = now - prev_t
        d_rx, d_tx = rx - prev_rx, tx - prev_tx
        if d_rx < 0 or d_tx < 0 or elapsed <= 0:
            return None  # counter reset/wrap -- skip this window
        return {
            "interface": self.iface,
            "rx_kbps": kbps(d_rx, elapsed),
            "tx_kbps": kbps(d_tx, elapsed),
            "window_s": elapsed,
        }
