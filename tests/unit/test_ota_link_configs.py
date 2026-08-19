"""The OTA templates a session gets when it only names a middleware.

`shared.rmw` is meant to be an interchangeable choice: a session says `cyclone`
or `fastdds` and gets a configuration that survives a cellular tunnel either
way. That promise lives in two places — the default template per implementation
and what those templates actually set — and neither is visible from a session
definition, so it is pinned here.

The fragment cap is the one that decides whether a link works at all. Left at
its default, Fast DDS hands a 38 kB camera keyframe to the kernel as one
datagram and the peer has to reassemble ~27 IP fragments as a unit; Cyclone has
always sent 1200 B RTPS fragments here. Both templates must agree.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
OTA_CONFIGS = REPO_ROOT / "src" / "rosotacom" / "resources" / "ws" / "ota_configs"
GENERATOR_PY = (
    REPO_ROOT / "src" / "rosotacom" / "resources" / "ws" / "session" / "creation" / "generate_session_files.py"
)
PLUGIN_BASE = (
    REPO_ROOT / "src" / "rosotacom" / "resources" / "ws" / "session" / "content" / "base" / "session_plugin_base.yaml"
)

OTA_FRAGMENT_BYTES = 1200


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _without_comments(xml: str) -> str:
    """The rationale is in the XML comments and names the elements it argues about."""
    return re.sub(r"<!--.*?-->", "", xml, flags=re.DOTALL)


def _resolved(config: str) -> str:
    module = _load(OTA_CONFIGS / "get_ota_xml.py", "rosotacom_get_ota_xml_link")
    return str(module.main(config=config, host_ip="10.0.0.1", peer="10.0.0.2", easy_mode_ip="10.0.0.2"))


def test_naming_only_the_middleware_still_selects_the_link_ready_template() -> None:
    generator = _load(GENERATOR_PY, "rosotacom_generate_session_files_link")

    assert generator._DDS_OTA_DEFAULT_CONFIG == {
        "cyclone": "cyclonedds_tuned.xml",
        "fastdds": "fastdds_tuned.xml",
    }
    for template in generator._DDS_OTA_DEFAULT_CONFIG.values():
        assert (OTA_CONFIGS / f"{template}.template").is_file()


def test_only_cyclone_caps_the_datagram_and_fast_dds_must_not() -> None:
    """The obvious symmetry is wrong, and writing it costs the whole link.

    CycloneDDS carries this link at 1200 B RTPS fragments. Measured on the bench
    pair (2026-08-18/19, Fast DDS 3.2.4), a `<maxMessageSize>` below the sample
    size stops the sample from ever arriving — at 1200 B and at 8192 B, with a
    synchronous writer and with an asynchronous one — while an 84 B heartbeat on
    the same link keeps flowing. Fast DDS lets IP fragment a large sample here;
    that is a difference in behaviour to state, not one to paper over.
    """
    cyclone = _resolved("cyclonedds_tuned.xml")

    assert f"<FragmentSize>{OTA_FRAGMENT_BYTES}B</FragmentSize>" in cyclone
    assert f"<MaxMessageSize>{OTA_FRAGMENT_BYTES}B</MaxMessageSize>" in cyclone

    for config in ("fastdds_tuned.xml", "fastdds_easy_mode.xml"):
        resolved = _without_comments(_resolved(config))
        assert "<maxMessageSize>" not in resolved, config
        assert "max_msg_size=" not in resolved, config


def test_both_default_ota_templates_pin_the_interface_and_the_peer() -> None:
    for config in ("cyclonedds_tuned.xml", "fastdds_tuned.xml"):
        resolved = _resolved(config)
        assert "10.0.0.1" in resolved, config  # the OTA interface, not every interface
        assert "10.0.0.2" in resolved, config  # unicast discovery of the one peer
        assert "#" not in resolved, config  # every placeholder substituted


def test_fastdds_tuned_drops_rather_than_blocks_the_writer() -> None:
    assert "<non_blocking_send>true</non_blocking_send>" in _resolved("fastdds_tuned.xml")


def test_no_ota_window_forces_a_publication_mode_any_more() -> None:
    """Asynchronous publication existed to make fragmentation work; nothing does.

    It is not free: on the packaged 12 kB bench stream an asynchronous writer
    measured 9.3 Hz at 285 ms where the synchronous default held 10 Hz at a few
    ms, so forcing it now would be a pessimisation with no fragmentation to pay
    for it.
    """
    plugin_base = PLUGIN_BASE.read_text(encoding="utf-8")

    exports_profile = re.findall(
        r"fastdds\) export FASTDDS_DEFAULT_PROFILES_FILE=\"\$\{ota_config_file\}\";[^\n]*", plugin_base
    )
    assert len(exports_profile) == 3  # the COM, IN and OUT windows each bootstrap the OTA side
    for occurrence in exports_profile:
        assert "RMW_FASTRTPS_PUBLICATION_MODE" not in occurrence
    assert "<publishMode>" not in _without_comments(_resolved("fastdds_tuned.xml"))


def test_native_zenoh_carries_the_sessions_inter_host_transport(tmp_path: Path) -> None:
    """The transport between the two routers is a link decision, so a session makes it.

    Only the connecting peer opens an endpoint; the listening peer keeps TCP on
    7447 regardless, because its own nodes reach it over `tcp/localhost:7447`.
    """
    import contextlib
    import io

    import yaml

    generator = _load(GENERATOR_PY, "rosotacom_generate_session_files_zenoh")
    cfg = {
        "peers": {"a": {}, "b": {}},
        "peer_settings": {"a": {"domain_id": 50}, "b": {"domain_id": 51}},
        "shared": {
            "ota_domain_id": 52,
            "use_heartbeat": True,
            "rmw": {
                "local": "zenoh",
                "ota": {"zenoh_connect_endpoints": {"transport": "udp", "main_peer": "a"}},
            },
        },
        "topics": {"a_to_b": [], "b_to_a": [{"topic": "/x", "type": "std_msgs/msg/Bool"}]},
    }
    with contextlib.redirect_stdout(io.StringIO()):
        generator.func(
            session_config_obj=cfg,
            output_dir=str(tmp_path),
            force=True,
            peer_addresses={"a": "10.0.0.1", "b": "10.0.0.2"},
        )

    plugins = {
        peer: yaml.safe_load((tmp_path / peer / "plugin.yaml").read_text(encoding="utf-8"))["parameters"]
        for peer in "ab"
    }
    for peer, parameters in plugins.items():
        assert parameters["zen_transport"] == "udp", peer
        assert parameters["zen_main_ip"] == "10.0.0.1", peer
    assert plugins["a"]["zen_connect"] is False
    assert plugins["b"]["zen_connect"] is True

    plugin_base = PLUGIN_BASE.read_text(encoding="utf-8")
    assert 'connect/endpoints=["\'"${zen_transport}"\'/' in plugin_base
    assert 'listen/endpoints=["tcp/[::]:7447"' in plugin_base


def test_native_zenoh_refuses_a_transport_it_cannot_configure(tmp_path: Path) -> None:
    """tls/quic need certificates the generator has nowhere to put."""
    generator = _load(GENERATOR_PY, "rosotacom_generate_session_files_zenoh_bad")
    cfg = {
        "peers": {"a": {}, "b": {}},
        "peer_settings": {"a": {"domain_id": 50}, "b": {"domain_id": 51}},
        "shared": {
            "ota_domain_id": 52,
            "rmw": {"local": "zenoh", "ota": {"zenoh_connect_endpoints": {"transport": "quic"}}},
        },
        "topics": {"a_to_b": [], "b_to_a": [{"topic": "/x", "type": "std_msgs/msg/Bool"}]},
    }
    import pytest

    with pytest.raises(RuntimeError, match="transport must be one of"):
        generator.func(
            session_config_obj=cfg,
            output_dir=str(tmp_path),
            force=True,
            peer_addresses={"a": "10.0.0.1", "b": "10.0.0.2"},
        )


def test_easy_mode_configures_its_transport_without_replacing_it() -> None:
    """Easy mode configures its own transport mix, so the knobs have to ride on it.

    `useBuiltinTransports=false` plus a custom descriptor — what the other Fast
    DDS template does — would take the discovery-server path with it.
    """
    resolved = _resolved("fastdds_easy_mode.xml")

    assert "<easy_mode_ip>10.0.0.2</easy_mode_ip>" in resolved
    assert 'non_blocking="true"' in resolved
    assert "<useBuiltinTransports>" not in resolved
    assert "#" not in resolved


def test_fastdds_tuned_keeps_the_same_host_path_open() -> None:
    """A split OTA domain carries a same-host peer, and pinning it away kills the link.

    With `shared.ota_domain_id` set, the stock `domain_bridge` process sits on
    the OTA domain speaking whatever RMW the *local* side uses. An OTA
    participant that may only use the tunnel address cannot discover it, and the
    failure reads as a dead link: every stage flows up to `com_out` and nothing
    is ever published on `/ota/...`.
    """
    resolved = _resolved("fastdds_tuned.xml")

    assert '<interface name="127.0.0.1" netmask_filter="OFF"/>' in resolved
    assert resolved.count("<address>127.0.0.1</address>") == 3  # default, metatraffic, initial peers
    # An initial peer without a port probes participant indices 0..range; the
    # Fast DDS default of 4 is smaller than an OTA domain can hand out.
    assert "<maxInitialPeersRange>10</maxInitialPeersRange>" in resolved


def test_every_peer_gets_the_stage_types_its_bridges_need(tmp_path: Path) -> None:
    """The session declares what each stage carries, so a bridge need not ask the graph.

    Generated for every session, not only when a status overview was asked for:
    the bridge and relay nodes need it either way, and over a transport that
    carries data but not the ROS graph it is the only thing that lets them
    create an endpoint at all.
    """
    import contextlib
    import io

    import yaml

    generator = _load(GENERATOR_PY, "rosotacom_generate_session_files_types")
    cfg = {
        "peers": {"a": {}, "b": {}},
        "peer_settings": {"a": {"domain_id": 50}, "b": {"domain_id": 51}},
        "shared": {"ota_domain_id": 52, "use_heartbeat": True, "rmw": "cyclone"},
        "topics": {
            "a_to_b": [],
            "b_to_a": [{"topic": "/cam", "type": "std_msgs/msg/Bool", "processing": {"use_ota_wrapper": True}}],
        },
    }
    with contextlib.redirect_stdout(io.StringIO()):
        generator.func(
            session_config_obj=cfg,
            output_dir=str(tmp_path),
            force=True,
            peer_addresses={"a": "10.0.0.1", "b": "10.0.0.2"},
        )

    for peer in "ab":
        declared = yaml.safe_load((tmp_path / peer / "topic_types.yaml").read_text(encoding="utf-8"))["topic_types"]
        # The wrapped OTA stage is the one a receiving bridge cannot look up.
        assert declared["/ota/b/cam/ota_stamped"] == "com_msgs/msg/OtaStamped", peer
        assert declared["/heartbeat_b"] == "com_msgs/msg/EchoHeartbeat", peer


def test_the_zenoh_bridge_defaults_to_the_transport_that_measured_better(tmp_path: Path) -> None:
    """TCP, not UDP, and the reason is a measurement rather than a preference.

    On the bench pair (2026-08-19, the 2026-08-17 drive's loss process, 12 kB at
    10 Hz) the UDP bridge delivered in one run of five and lost 66% of that one;
    the TCP bridge delivered in every run at ~1% loss. UDP stays selectable.
    """
    import contextlib
    import io

    import yaml

    generator = _load(GENERATOR_PY, "rosotacom_generate_session_files_bridge")
    cfg = {
        "peers": {"a": {}, "b": {}},
        "peer_settings": {"a": {"domain_id": 50}, "b": {"domain_id": 51}},
        "shared": {
            "ota_domain_id": 52,
            "use_heartbeat": True,
            "rmw": {"local": "cyclone", "ota": "zenoh_ros2dds"},
        },
        "topics": {"a_to_b": [], "b_to_a": [{"topic": "/x", "type": "std_msgs/msg/Bool"}]},
    }
    with contextlib.redirect_stdout(io.StringIO()):
        generator.func(
            session_config_obj=cfg,
            output_dir=str(tmp_path),
            force=True,
            peer_addresses={"a": "10.0.0.1", "b": "10.0.0.2"},
        )

    parameters = yaml.safe_load((tmp_path / "a" / "plugin.yaml").read_text(encoding="utf-8"))["parameters"]
    assert parameters["zen_transport"] == "tcp"
