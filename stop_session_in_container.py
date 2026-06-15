#!/usr/bin/env python3
"""Compatibility entrypoint for the legacy `stop_rosotacom` command."""

from rosotacom import stop_compat_main


if __name__ == "__main__":
    raise SystemExit(stop_compat_main())
