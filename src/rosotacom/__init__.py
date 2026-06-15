"""rosotacom package."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

_DISTRIBUTION_NAME = "rosotacom"


def _package_version() -> str:
    try:
        return version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _package_version()


def __getattr__(name: str) -> Any:
    """Lazily expose the historical root-module CLI attributes."""

    from . import cli

    try:
        return getattr(cli, name)
    except AttributeError as exc:
        raise AttributeError(f"module 'rosotacom' has no attribute {name!r}") from exc


__all__ = ["__version__"]
