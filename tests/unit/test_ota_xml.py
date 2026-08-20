from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "rosotacom"
        / "resources"
        / "ws"
        / "ota_configs"
        / "get_ota_xml.py"
    )
    spec = importlib.util.spec_from_file_location("test_get_ota_xml", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ota_xml_uses_resolved_literal_addresses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    template = tmp_path / "test.xml.template"
    template.write_text("<host>#host_ip</host><peer>#peer</peer><easy>#easy_mode_ip</easy>\n", encoding="utf-8")
    monkeypatch.setattr(module, "_template_path", lambda _name: str(template))

    rendered = module.main(
        config="test.xml",
        host_ip="10.0.0.10",
        peer="10.0.0.11",
        easy_mode_ip="10.0.0.10",
    )

    assert rendered == "<host>10.0.0.10</host><peer>10.0.0.11</peer><easy>10.0.0.10</easy>\n"


def test_ota_xml_renders_default_and_overridden_spdp_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    template = tmp_path / "test.xml.template"
    template.write_text("<SPDPInterval>#spdp_interval</SPDPInterval>\n", encoding="utf-8")
    monkeypatch.setattr(module, "_template_path", lambda _name: str(template))

    assert module.main(config="test.xml") == "<SPDPInterval>30s</SPDPInterval>\n"
    assert module.main(config="test.xml", spdp_interval="150s") == "<SPDPInterval>150s</SPDPInterval>\n"


def test_ota_xml_rejects_empty_resolved_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    template = tmp_path / "test.xml.template"
    template.write_text("<host>#host_ip</host>\n", encoding="utf-8")
    monkeypatch.setattr(module, "_template_path", lambda _name: str(template))

    with pytest.raises(ValueError, match="non-empty resolved address"):
        module.main(config="test.xml", host_ip=" ")


def test_ota_xml_rejects_invalid_spdp_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    template = tmp_path / "test.xml.template"
    template.write_text("<SPDPInterval>#spdp_interval</SPDPInterval>\n", encoding="utf-8")
    monkeypatch.setattr(module, "_template_path", lambda _name: str(template))

    with pytest.raises(ValueError, match="seconds or milliseconds"):
        module.main(config="test.xml", spdp_interval="2min")


def test_ota_xml_scopes_sections_to_their_domains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    template = tmp_path / "test.xml.template"
    template.write_text('<Domain Id="#ota_domain"/><Domain Id="#local_domain"/>\n', encoding="utf-8")
    monkeypatch.setattr(module, "_template_path", lambda _name: str(template))

    rendered = module.main(config="test.xml", local_domain="47", ota_domain="48")

    assert rendered == '<Domain Id="48"/><Domain Id="47"/>\n'


def test_ota_xml_refuses_a_domain_it_cannot_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A section Cyclone matches to no domain is silently ignored, so refuse first."""
    module = _module()
    template = tmp_path / "test.xml.template"
    template.write_text('<Domain Id="#ota_domain"/>\n', encoding="utf-8")
    monkeypatch.setattr(module, "_template_path", lambda _name: str(template))

    for bad in ("233", "-1", "any", "", "4 7"):
        with pytest.raises((ValueError, RuntimeError)):
            module.main(config="test.xml", ota_domain=bad)


def test_ota_xml_names_the_option_a_scoped_template_needs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    template = tmp_path / "test.xml.template"
    template.write_text('<Domain Id="#local_domain"/>\n', encoding="utf-8")
    monkeypatch.setattr(module, "_template_path", lambda _name: str(template))

    with pytest.raises(RuntimeError) as excinfo:
        module.main(config="test.xml")

    assert "--local-domain" in str(excinfo.value)


def test_shipped_scoped_template_carries_both_domains_and_only_ota_restrictions() -> None:
    """The point of the file: OTA settings on the OTA domain, local domain left alone.

    A section that pinned an interface or disabled multicast on the *local*
    domain would recreate the failure it exists to avoid.
    """
    module = _module()
    rendered = module.main(
        config="cyclonedds_scoped.xml",
        host_ip="10.0.0.10",
        peer="10.0.0.11",
        local_domain="47",
        ota_domain="48",
    )

    assert '<Domain Id="48">' in rendered
    assert '<Domain Id="47">' in rendered

    ota_section = rendered.split('<Domain Id="48">', 1)[1].split("</Domain>", 1)[0]
    local_section = rendered.split('<Domain Id="47">', 1)[1].split("</Domain>", 1)[0]

    assert "<AllowMulticast>false</AllowMulticast>" in ota_section
    assert "10.0.0.10" in ota_section and "10.0.0.11" in ota_section

    for restriction in ("AllowMulticast", "NetworkInterface", "Peer ", "MaxMessageSize"):
        assert restriction not in local_section, restriction
    assert "MaxAutoParticipantIndex" in local_section
