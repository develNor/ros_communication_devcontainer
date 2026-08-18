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


def _resolved(config: str) -> str:
    module = _load(OTA_CONFIGS / "get_ota_xml.py", "rosotacom_get_ota_xml_link")
    return str(module.main(config=config, host_ip="10.0.0.1", peer="10.0.0.2"))


def test_naming_only_the_middleware_still_selects_the_link_ready_template() -> None:
    generator = _load(GENERATOR_PY, "rosotacom_generate_session_files_link")

    assert generator._DDS_OTA_DEFAULT_CONFIG == {
        "cyclone": "cyclonedds_tuned.xml",
        "fastdds": "fastdds_tuned.xml",
    }
    for template in generator._DDS_OTA_DEFAULT_CONFIG.values():
        assert (OTA_CONFIGS / f"{template}.template").is_file()


def test_both_default_ota_templates_cap_the_fragment_at_the_same_size() -> None:
    cyclone = _resolved("cyclonedds_tuned.xml")
    fastdds = _resolved("fastdds_tuned.xml")

    assert f"<FragmentSize>{OTA_FRAGMENT_BYTES}B</FragmentSize>" in cyclone
    assert f"<MaxMessageSize>{OTA_FRAGMENT_BYTES}B</MaxMessageSize>" in cyclone
    assert f"<maxMessageSize>{OTA_FRAGMENT_BYTES}</maxMessageSize>" in fastdds


def test_both_default_ota_templates_pin_the_interface_and_the_peer() -> None:
    for config in ("cyclonedds_tuned.xml", "fastdds_tuned.xml"):
        resolved = _resolved(config)
        assert "10.0.0.1" in resolved, config  # the OTA interface, not every interface
        assert "10.0.0.2" in resolved, config  # unicast discovery of the one peer
        assert "#" not in resolved, config  # every placeholder substituted


def test_fastdds_tuned_drops_rather_than_blocks_the_writer() -> None:
    assert "<non_blocking_send>true</non_blocking_send>" in _resolved("fastdds_tuned.xml")


def test_fragmenting_fastdds_gets_the_asynchronous_publication_it_requires() -> None:
    """Fast DDS refuses to fragment in synchronous publication mode.

    The mode is an environment variable rather than a `<publishMode>` in the XML
    so it does not also depend on RMW_FASTRTPS_USE_QOS_FROM_XML being honoured —
    and every place that exports the OTA profile file has to set it, or the
    window that missed it sends nothing.
    """
    plugin_base = PLUGIN_BASE.read_text(encoding="utf-8")

    exports_profile = re.findall(
        r"fastdds\) export FASTDDS_DEFAULT_PROFILES_FILE=\"\$\{ota_config_file\}\";[^\n]*", plugin_base
    )
    assert len(exports_profile) == 3  # the COM, IN and OUT windows each bootstrap the OTA side
    assert exports_profile
    for occurrence in exports_profile:
        assert "RMW_FASTRTPS_PUBLICATION_MODE" in occurrence
        assert "ASYNCHRONOUS" in occurrence
