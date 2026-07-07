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
