"""rosotacom package."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, packages_distributions, version
from typing import Any


def _package_version() -> str:
    """Version of the distribution that ships this package.

    The distribution name is looked up, not hardcoded. This source is published
    as `rosotacom-dev` from the development fork and as `rosotacom` upstream, so
    a hardcoded name resolves under one of them and silently degrades to
    "0+unknown" under the other — which is what happened up to 2.3: every
    installed `rosotacom-dev` misreported its own version, so `--version` could
    not be compared against a pin, and the OTA source bundle wrote an invalid
    PEP 440 version into its generated metadata.
    """
    for distribution in packages_distributions().get(__name__, ()):
        try:
            return version(distribution)
        except PackageNotFoundError:
            continue
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
