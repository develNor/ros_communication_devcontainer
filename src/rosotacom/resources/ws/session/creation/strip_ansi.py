#!/usr/bin/env python3
"""Strip ANSI/VT100 control sequences from stdin to stdout."""

from __future__ import annotations

import re
import sys


_ANSI_PATTERN = re.compile(
    rb"\x1b\[[0-9;?]*[ -/]*[@-~]"
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    rb"|\x1b[@-Z\\-_]"
)


def _strip(chunk: bytes) -> bytes:
    return _ANSI_PATTERN.sub(b"", chunk)


def main() -> None:
    src = sys.stdin.buffer
    dst = sys.stdout.buffer
    pending = b""
    while True:
        chunk = src.read1(4096)
        if not chunk:
            break
        pending += chunk
        cut = pending.rfind(b"\n")
        if cut < 0:
            continue
        dst.write(_strip(pending[: cut + 1]))
        dst.flush()
        pending = pending[cut + 1:]
    if pending:
        dst.write(_strip(pending))
        dst.flush()


if __name__ == "__main__":
    main()
