from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def test_package_version_is_derived_from_scm_metadata() -> None:
    pyproject = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    package_init = (PACKAGE_ROOT / "src" / "rosotacom" / "__init__.py").read_text(encoding="utf-8")

    assert '\nversion = "0.1.0"' not in pyproject
    assert 'dynamic = ["version"]' in pyproject
    assert "setuptools-scm" in pyproject
    assert '__version__ = "0.1.0"' not in package_init


def test_console_scripts_target_package_cli() -> None:
    pyproject = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'rosotacom = "rosotacom.cli:main"' in pyproject
    assert 'start_rosotacom = "rosotacom.cli:start_compat_main"' in pyproject
    assert 'stop_rosotacom = "rosotacom.cli:stop_compat_main"' in pyproject


def test_videoquality_reader_dependencies_are_packaged() -> None:
    pyproject = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"mcap>=1.4,<2"' in pyproject
    assert '"Pillow>=10,<13"' in pyproject


def test_checkout_installer_enables_completion_during_venv_activation() -> None:
    installer = (PACKAGE_ROOT / "install.sh").read_text(encoding="utf-8")

    assert "# >>> rosotacom shell completion >>>" in installer
    assert '"$VIRTUAL_ENV/bin/rosotacom" completion zsh' in installer
    assert '"$VIRTUAL_ENV/bin/rosotacom" completion bash' in installer
    assert "case $- in" in installer  # Do not register completion in non-interactive scripts.


def test_python_support_contract_is_consistent() -> None:
    pyproject = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    contributing = (PACKAGE_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    ci_workflow = (PACKAGE_ROOT / ".github" / "workflows" / "pr-merge-gate.yml").read_text(encoding="utf-8")
    release_workflow = (PACKAGE_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert 'requires-python = ">=3.10"' in pyproject
    assert "3.10 through 3.14" in contributing
    for version in ("3.10", "3.11", "3.12", "3.13", "3.14"):
        assert f"Programming Language :: Python :: {version}" in pyproject
        assert version in ci_workflow
        assert version in release_workflow
