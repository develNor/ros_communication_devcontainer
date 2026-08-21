"""The header-only OTA capture: its file, its filter, and its switch.

Three layers, like the status overview it is started beside:
  * the pcap a session produces, read back with a reader written from the pcap
    and SLL specifications rather than from the writer's own constants;
  * `shared.ota_pcap` validation in the session generator;
  * that a session which does not ask for it renders the command line it
    always did.
"""

from __future__ import annotations

import importlib.util
import socket
import struct
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WS = REPO_ROOT / "src" / "rosotacom" / "resources" / "ws"
WS_PY = WS / "ros2src" / "com_py"
GENERATOR_PY = WS / "session" / "creation" / "generate_session_files.py"
PLUGIN_BASE = WS / "session" / "content" / "base" / "session_plugin_base.yaml"
sys.path.insert(0, str(WS_PY))

from com_py import ota_pcap  # noqa: E402


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


generator = _load_module(GENERATOR_PY, "rosotacom_generate_session_files_pcap")


# ---------------------------------------------------------------------------
# the file
# ---------------------------------------------------------------------------


def read_pcap(raw: bytes) -> tuple[dict, list[dict]]:
    """A reader written from the specifications, not from the writer."""
    magic, major, minor, zone, sigfigs, snaplen, network = struct.unpack("<IHHiIII", raw[:24])
    header = {
        "magic": magic,
        "version": (major, minor),
        "zone": zone,
        "sigfigs": sigfigs,
        "snaplen": snaplen,
        "network": network,
    }
    packets, offset = [], 24
    while offset + 16 <= len(raw):
        secs, usecs, incl, orig = struct.unpack("<IIII", raw[offset : offset + 16])
        body = raw[offset + 16 : offset + 16 + incl]
        pkttype, hatype, halen, addr, protocol = struct.unpack("!HHH8sH", body[:16])
        packets.append(
            {
                "time": secs + usecs / 1e6,
                "incl": incl,
                "orig": orig,
                "pkttype": pkttype,
                "hatype": hatype,
                "halen": halen,
                "addr": addr[:halen],
                "protocol": protocol,
            }
        )
        offset += 16 + incl
    return header, packets


def test_the_file_is_a_microsecond_pcap_of_linux_cooked_packets() -> None:
    header, packets = read_pcap(ota_pcap.pcap_header(96))
    assert header["magic"] == 0xA1B2C3D4, "the classic magic; every reader takes it"
    assert header["version"] == (2, 4)
    assert header["snaplen"] == 96
    assert header["network"] == 113, "LINKTYPE_LINUX_SLL — the one that carries direction"
    assert packets == []


def test_a_packet_keeps_its_direction_its_true_length_and_its_kernel_time() -> None:
    """The three fields the analysis is for, through a writer/reader round trip.

    Direction is the point: `link_trace.jsonl` gives per-second byte counters
    per direction, and this gives it per packet.
    """
    body = ota_pcap.sll_header(ota_pcap.PACKET_OUTGOING, 65534, b"", ota_pcap.ETH_P_IP) + bytes(range(60))
    clipped = body[:32]
    raw = ota_pcap.pcap_header(32) + ota_pcap.record_header(1787297717, 8177, len(clipped), len(body)) + clipped

    _header, packets = read_pcap(raw)
    assert len(packets) == 1
    packet = packets[0]
    assert packet["pkttype"] == 4, "PACKET_OUTGOING: this one is uplink"
    assert packet["orig"] == len(body), "the wire length survives the truncation"
    assert packet["incl"] == 32
    assert packet["protocol"] == 0x0800
    assert packet["halen"] == 0, "a layer-3 tunnel has no link-layer address"
    assert packet["time"] == pytest.approx(1787297717.008177)


def test_an_ethernet_address_is_written_with_its_length_not_padded_into_it() -> None:
    """The other link type the fleet has: a control centre's LAN port."""
    mac = bytes.fromhex("001122334455")
    raw = (
        ota_pcap.pcap_header(96)
        + ota_pcap.record_header(1, 0, 16, 16)
        + ota_pcap.sll_header(0, 1, mac, ota_pcap.ETH_P_IP)
    )
    _header, packets = read_pcap(raw)
    assert packets[0]["halen"] == 6
    assert packets[0]["addr"] == mac
    assert packets[0]["hatype"] == 1


# ---------------------------------------------------------------------------
# the filter
# ---------------------------------------------------------------------------


def ipv4(protocol: int, source: str = "10.254.0.32", dest: str = "10.254.0.37") -> bytes:
    return bytes([0x45, 0, 0, 40, 0, 0, 0, 0, 64, protocol, 0, 0]) + socket.inet_aton(source) + socket.inet_aton(dest)


def ipv6(next_header: int) -> bytes:
    return (
        bytes([0x60, 0, 0, 0, 0, 8, next_header, 64])
        + socket.inet_pton(socket.AF_INET6, "fd00::1")
        + socket.inet_pton(socket.AF_INET6, "fd00::2")
    )


@pytest.mark.parametrize(
    "protocol, packet, expected",
    [
        (ota_pcap.ETH_P_IP, ipv4(17), True),
        (ota_pcap.ETH_P_IP, ipv4(6), False),  # TCP
        (ota_pcap.ETH_P_IP, ipv4(1), False),  # ICMP: a ping is not the link
        (ota_pcap.ETH_P_IPV6, ipv6(17), True),
        (ota_pcap.ETH_P_IPV6, ipv6(58), False),  # ICMPv6
        (0x0806, b"\x00" * 40, False),  # ARP is not IP at all
        (ota_pcap.ETH_P_IP, b"\x45\x00", False),  # truncated: answered, not raised
    ],
)
def test_only_udp_over_ip_is_kept(protocol: int, packet: bytes, expected: bool) -> None:
    assert ota_pcap.is_udp(protocol, packet) is expected


def test_a_later_ipv4_fragment_is_kept_although_it_carries_no_udp_header() -> None:
    """The protocol field is in every fragment's IP header, which is what keeps
    a fragmented sample whole in the file rather than only its first piece —
    and fragments are exactly what this capture exists to see."""
    fragment = bytearray(ipv4(17))
    fragment[6:8] = (0x00B9).to_bytes(2, "big")  # offset 185, MF clear
    assert ota_pcap.is_udp(ota_pcap.ETH_P_IP, bytes(fragment)) is True


def test_the_peer_filter_matches_either_direction_and_nothing_else() -> None:
    up = ipv4(17, "10.254.0.32", "10.254.0.37")
    down = ipv4(17, "10.254.0.37", "10.254.0.32")
    elsewhere = ipv4(17, "10.254.0.32", "10.254.0.39")
    assert ota_pcap.wanted(ota_pcap.ETH_P_IP, up, "10.254.0.37") is True
    assert ota_pcap.wanted(ota_pcap.ETH_P_IP, down, "10.254.0.37") is True
    assert ota_pcap.wanted(ota_pcap.ETH_P_IP, elsewhere, "10.254.0.37") is False
    assert ota_pcap.wanted(ota_pcap.ETH_P_IP, elsewhere, None) is True


def test_the_sidecar_separates_a_gap_in_the_file_from_a_gap_on_the_link() -> None:
    """`kernel_drops` is the number that keeps a slow capture from being read
    as radio loss months later, which is the failure this whole artifact would
    otherwise invite."""
    capture = ota_pcap.Capture("tun0", Path("/tmp/x.pcap"), peer="10.254.0.37")
    capture.kernel_drops = 12
    report = capture.sidecar()
    assert report["kernel_drops"] == 12
    assert report["filter"] == "udp and host 10.254.0.37"
    assert report["promiscuous"] is False, "this host's own traffic, never anyone else's"
    assert report["linktype"] == "LINKTYPE_LINUX_SLL"


def test_the_capture_needs_nothing_that_is_not_in_the_communication_image() -> None:
    """It runs inside the peer's container, which is not rebuilt for it."""
    import ast

    tree = ast.parse((WS_PY / "com_py" / "ota_pcap.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), sorted(imported - set(sys.stdlib_module_names))


# ---------------------------------------------------------------------------
# the switch
# ---------------------------------------------------------------------------


def _cfg(**ota_pcap_block) -> dict:
    cfg = {
        "peers": {"a": {}, "b": {}},
        "shared": {"use_status_overview": True, "use_heartbeat": True},
        "topics": {"a_to_b": [{"topic": "/chatter", "type": "std_msgs/msg/String"}]},
    }
    if ota_pcap_block:
        cfg["shared"]["ota_pcap"] = ota_pcap_block
    return cfg


def _generate(cfg: dict, output_dir: Path) -> None:
    generator.func(
        session_config_obj=cfg,
        output_dir=str(output_dir),
        force=True,
        peer_addresses={"a": "127.0.0.1", "b": "127.0.0.2"},
    )


def test_a_session_that_does_not_ask_for_it_renders_what_it_always_did(tmp_path: Path) -> None:
    """The sentence that makes the option safe to carry through a demo week.

    Asserted by rendering both and diffing, not by looking for a string: the
    off case emits nothing named `ota_pcap` at all, so a substring test would
    pass whatever the generator did.
    """
    off, on = tmp_path / "off", tmp_path / "on"
    _generate(_cfg(), off)
    _generate(_cfg(enabled=True), on)

    def rendered(root: Path) -> dict[str, str]:
        return {
            str(path.relative_to(root)): path.read_text(encoding="utf-8").replace(str(root), "<DIR>")
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    without, with_capture = rendered(off), rendered(on)
    assert set(without) == set(with_capture), "the same files, either way"
    changed = [name for name in without if without[name] != with_capture[name]]
    assert changed == ["a/plugin.yaml", "b/plugin.yaml"], changed

    for name in changed:
        added = [line.strip() for line in with_capture[name].splitlines() if line not in without[name].splitlines()]
        assert all(line.startswith("ota_pcap") for line in added), added


def test_enabling_it_carries_the_four_values_into_the_peer(tmp_path: Path) -> None:
    _generate(_cfg(enabled=True, snaplen=128, max_mb=500, peer_filter=False), tmp_path)
    found = [p for p in sorted(tmp_path.rglob("*.yaml")) if "ota_pcap" in p.read_text(encoding="utf-8").lower()]
    assert found, "the generated session names the capture somewhere"
    blob = "\n".join(p.read_text(encoding="utf-8") for p in found)
    assert "ota_pcap: true" in blob.lower()
    assert "128" in blob and "500" in blob


@pytest.mark.parametrize(
    "block, reason",
    [
        ({"enabled": "yes"}, "enabled must be boolean"),
        ({"peer_filter": 1}, "peer_filter must be boolean"),
        ({"snaplen": 0}, "snaplen below the floor"),
        ({"snaplen": 99999}, "snaplen above a jumbo frame"),
        ({"snaplen": True}, "a bool is not a snaplen"),
        ({"max_mb": 0}, "a ceiling of nothing"),
        ({"unknown": 1}, "an unknown key is a typo, not a feature"),
    ],
)
def test_a_malformed_block_is_refused_when_the_session_is_read(block, reason) -> None:
    with pytest.raises(RuntimeError):
        generator._validate_session_template_cfg(_cfg(**block)), reason


def test_it_requires_the_node_that_starts_it() -> None:
    """The capture is started by `status_overview`, which is also what resolves
    the OTA interface from the peer's own address — including the loopback
    refusal (#267). Without it there is nothing to hang the capture on."""
    cfg = _cfg(enabled=True)
    cfg["shared"]["use_status_overview"] = False
    with pytest.raises(RuntimeError, match="use_status_overview"):
        _generate(cfg, Path("/tmp/unused"))


def test_the_template_defaults_it_off() -> None:
    """A parameter that defaults on would capture packets for every rosotacom
    user who never asked. The switch is the session definition, not the base."""
    base = yaml.safe_load(PLUGIN_BASE.read_text(encoding="utf-8"))
    assert base["parameters"]["ota_pcap"] is False
    assert base["parameters"]["ota_pcap_snaplen"] == ota_pcap.DEFAULT_SNAPLEN
    assert base["parameters"]["ota_pcap_max_mb"] == ota_pcap.DEFAULT_MAX_MB


def test_the_node_is_handed_the_remote_peers_address_to_narrow_the_capture() -> None:
    """On a VPN tunnel the whole device is the link and the filter changes
    nothing; on a LAN port it is the difference between the link and every
    other conversation the machine is having."""
    text = PLUGIN_BASE.read_text(encoding="utf-8")
    assert "-p ota_pcap_peer_ip:=${ip_remote}" in text
