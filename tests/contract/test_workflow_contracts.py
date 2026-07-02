from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
JUSTFILE_PATH = PACKAGE_ROOT / "justfile"
PRE_COMMIT_PATH = PACKAGE_ROOT / ".pre-commit-config.yaml"
PYPROJECT_PATH = PACKAGE_ROOT / "pyproject.toml"
WORKFLOWS_DIR = PACKAGE_ROOT / ".github" / "workflows"
IGNORED_MARKDOWN_DIRS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "build", "dist"}

RECIPE_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_-]*)[^:\n]*:(?!=)", re.MULTILINE)
COMMAND_INVOCATION_RE = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S+[ \t]+)*just[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_-]*)",
    re.MULTILINE,
)
INLINE_INVOCATION_RE = re.compile(r"^just[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_-]*)")
FENCED_CODE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


def _recipe_names() -> set[str]:
    return set(RECIPE_RE.findall(JUSTFILE_PATH.read_text(encoding="utf-8")))


def _just_command_invocations(text: str) -> set[str]:
    return set(COMMAND_INVOCATION_RE.findall(text))


def _markdown_files() -> list[Path]:
    return sorted(
        path
        for path in PACKAGE_ROOT.rglob("*.md")
        if not any(
            part in IGNORED_MARKDOWN_DIRS or part.endswith(".egg-info") for part in path.relative_to(PACKAGE_ROOT).parts
        )
    )


def _just_invocations_in_markdown(text: str) -> set[str]:
    names: set[str] = set()
    for context in FENCED_CODE_RE.findall(text):
        names |= _just_command_invocations(context)
    for context in INLINE_CODE_RE.findall(text):
        match = INLINE_INVOCATION_RE.match(context.strip())
        if match:
            names.add(match.group("name"))
    return names


def _workflow_run_blocks(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "run" and isinstance(child, str):
                yield child
            else:
                yield from _workflow_run_blocks(child)
    elif isinstance(value, list):
        for item in value:
            yield from _workflow_run_blocks(item)


def _hook_rev(pre_commit: str, repo_url: str) -> str:
    match = re.search(
        rf"^\s*-\s*repo:\s*{re.escape(repo_url)}\s*\n\s*rev:\s*(\S+)",
        pre_commit,
        re.MULTILINE,
    )
    assert match, f"no pre-commit repo entry found for {repo_url}"
    return match.group(1).strip()


def test_just_recipe_names_are_discovered() -> None:
    recipes = _recipe_names()

    assert {"setup", "lint", "typecheck", "test-contract", "docs", "check"} <= recipes


def test_referenced_just_recipes_exist() -> None:
    recipes = _recipe_names()
    failures: list[str] = []

    for workflow in sorted(WORKFLOWS_DIR.glob("*.yml")):
        workflow_doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        for run_block in _workflow_run_blocks(workflow_doc):
            for name in sorted(_just_command_invocations(run_block)):
                if name not in recipes:
                    failures.append(f"{workflow.relative_to(PACKAGE_ROOT)}: just {name}")

    for markdown in _markdown_files():
        for name in sorted(_just_invocations_in_markdown(markdown.read_text(encoding="utf-8"))):
            if name not in recipes:
                failures.append(f"{markdown.relative_to(PACKAGE_ROOT)}: just {name}")

    assert not failures, (
        "These `just <recipe>` references point at recipes that do not exist in the justfile:\n" + "\n".join(failures)
    )


def test_precommit_ruff_matches_pyproject_pin() -> None:
    pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")
    pin = re.search(r'"ruff==([^"]+)"', pyproject)
    assert pin, "no `ruff==` pin found in pyproject.toml [dev] dependencies"
    pyproject_version = pin.group(1)

    rev = _hook_rev(
        PRE_COMMIT_PATH.read_text(encoding="utf-8"),
        "https://github.com/astral-sh/ruff-pre-commit",
    )

    assert rev == f"v{pyproject_version}", (
        f"ruff pre-commit rev {rev!r} does not match pyproject pin "
        f"ruff=={pyproject_version}; keep local hooks and just/CI checks aligned"
    )
