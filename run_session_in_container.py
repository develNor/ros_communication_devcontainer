#!/usr/bin/env python3
"""Compatibility entrypoint for the legacy `start_rosotacom` command."""

from __future__ import annotations

from rosotacom.cli import start_compat_main

if __name__ == "__main__":
    raise SystemExit(start_compat_main())
