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


#: Every workflow that runs the e2e suite, and the job whose matrix names the
#: slices. All three must expand to the same list: the merge gate decides what
#: may land, the release decides what ships, and nightly is the only one that
#: runs against a tree nobody is watching.
E2E_SLICE_MATRICES = {
    "release.yml": "e2e",
    "pr-merge-gate.yml": "e2e",
    "nightly-e2e.yml": "smoke-e2e",
}


def _e2e_slices(workflow_name: str, job: str) -> list[str]:
    workflow = yaml.safe_load((WORKFLOWS_DIR / workflow_name).read_text(encoding="utf-8"))
    return list(workflow["jobs"][job]["strategy"]["matrix"]["slice"])


def _release_e2e_slices() -> list[str]:
    """The slice names the release matrix expands to."""
    return _e2e_slices("release.yml", E2E_SLICE_MATRICES["release.yml"])


def test_every_e2e_workflow_runs_the_same_slices() -> None:
    """One slice list, three workflows.

    Before this, the six e2e jobs in the merge gate were copy-pasted rather
    than a matrix, and `ci-success` listed `e2e-media` in `needs` while
    checking the results of only the other five — a failing media slice did not
    fail the gate. A matrix cannot lose a slice that way, and this keeps the
    three matrices from drifting apart instead.
    """
    lists = {name: _e2e_slices(name, job) for name, job in sorted(E2E_SLICE_MATRICES.items())}
    reference = lists["release.yml"]
    mismatched = {name: value for name, value in lists.items() if value != reference}
    assert not mismatched, f"e2e slice lists disagree; release.yml has {reference} but: " + "; ".join(
        f"{name} has {value}" for name, value in sorted(mismatched.items())
    )


def test_ci_success_checks_every_job_it_waits_for() -> None:
    """A job in `needs` that nothing checks is a gate that does not gate.

    `ci-success` runs with `if: always()`, so every needed job's failure has to
    be turned into an exit code by hand. `e2e-media` sat in `needs` for a
    release cycle with no matching check.
    """
    workflow = yaml.safe_load((WORKFLOWS_DIR / "pr-merge-gate.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["ci-success"]
    script = "\n".join(_workflow_run_blocks(job))
    unchecked = [name for name in job["needs"] if f"needs.{name}.result" not in script]
    assert not unchecked, (
        "ci-success waits for these jobs but never inspects their result, so "
        "their failure does not fail the gate: " + ", ".join(unchecked)
    )


def _recipe_pytest_invocations(recipe: str) -> list[list[str]]:
    """Every pytest argument list a recipe runs, with just's substitutions applied."""
    import shlex

    text = JUSTFILE_PATH.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(recipe)}\s*:[^\n]*\n(?P<body>(?:[ \t]+[^\n]*\n?)+)", text, re.MULTILINE)
    assert match, f"recipe {recipe!r} not found in the justfile"

    invocations: list[list[str]] = []
    for line in match.group("body").splitlines():
        line = line.strip()
        if "-m pytest" not in line:
            continue
        tokens = shlex.split(line.replace("{{python}}", "python"))
        start = tokens.index("pytest") + 1
        invocations.append([t for t in tokens[start:] if t != "-q"])
    return invocations


def _collect_e2e_ids(args: list[str]) -> set[str]:
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
        capture_output=True,
        text=True,
        cwd=PACKAGE_ROOT,
        env={**os.environ, "ROSOTACOM_RUN_E2E": "1"},
        check=False,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.startswith("tests/e2e/")}


def test_e2e_slices_partition_the_whole_suite() -> None:
    """The parallel slices must collect exactly what the monolith collects.

    `test-e2e-smoke` is `pytest tests/e2e/ -m e2e`, so it picks up any new file
    automatically; the slices name files and `-k` expressions by hand and do
    not. When the merge gate was split into slices, three files
    (anonymize, video-quality, benchmark-replay) and the two
    `[remote-assist-anonymized-*]` parameters silently stopped being covered
    there — `-k "remote_assist"` misses the latter because the parameter id
    spells it with hyphens. Nothing failed, because nightly and the release
    still ran the monolith. This test is what makes that drift loud.
    """
    monolith = _collect_e2e_ids(["tests/e2e/", "-m", "e2e"])
    assert monolith, "collected no e2e tests; the monolith invocation changed"

    covered: set[str] = set()
    for slice_name in _release_e2e_slices():
        for args in _recipe_pytest_invocations(f"test-e2e-{slice_name}"):
            covered |= _collect_e2e_ids(args)

    assert not monolith - covered, (
        "These e2e tests run in `just test-e2e-smoke` but in no slice, so the "
        "merge gate and the release would not run them:\n" + "\n".join(sorted(monolith - covered))
    )
    assert not covered - monolith, (
        "These tests are selected by a slice but not by the monolith, so the "
        "two definitions disagree:\n" + "\n".join(sorted(covered - monolith))
    )
