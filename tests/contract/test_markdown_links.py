from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
IGNORED_DIRS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "build", "dist"}
LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\((?P<target>[^)]+)\)")


def _markdown_files() -> list[Path]:
    return sorted(
        path
        for path in PACKAGE_ROOT.rglob("*.md")
        if not any(part in IGNORED_DIRS or part.endswith(".egg-info") for part in path.relative_to(PACKAGE_ROOT).parts)
    )


def _local_link_target(raw_target: str) -> str | None:
    target = raw_target.strip()
    if not target or target.startswith("#"):
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    return unquote(target.split("#", 1)[0])


def test_internal_markdown_links_resolve() -> None:
    failures: list[str] = []

    for markdown_path in _markdown_files():
        for match in LINK_RE.finditer(markdown_path.read_text(encoding="utf-8")):
            target = _local_link_target(match.group("target"))
            if target is None:
                continue
            resolved = (markdown_path.parent / target).resolve()
            if not resolved.exists():
                rel_path = markdown_path.relative_to(PACKAGE_ROOT)
                failures.append(f"{rel_path}: missing link target {target!r}")

    assert not failures, "\n".join(failures)
