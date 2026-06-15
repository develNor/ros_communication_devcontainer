#!/usr/bin/env python3
"""Compatibility entrypoint for the legacy `stop_rosotacom` command."""

from __future__ import annotations

from rosotacom.cli import stop_compat_main

if __name__ == "__main__":
    raise SystemExit(stop_compat_main())
