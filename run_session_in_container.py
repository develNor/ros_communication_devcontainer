#!/usr/bin/env python3
"""Compatibility entrypoint for the legacy `start_rosotacom` command."""

from rosotacom import start_compat_main


if __name__ == "__main__":
    raise SystemExit(start_compat_main())
