from __future__ import annotations

import importlib.util
import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
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


def test_e2e_slices_take_the_image_repository_from_the_publisher() -> None:
    """A slice may only consume images the `image` job confirmed are there.

    That job publishes what is missing and then reports its repository *only*
    if every content-addressed reference resolves, so a read-only
    `GITHUB_TOKEN` leaves the value empty and the slices build as they always
    did. Writing the repository into the e2e job directly would skip that
    probe: the slices would then fail on a pull that was never going to work,
    which is worse than the build this replaces.
    """
    workflow = yaml.safe_load((WORKFLOWS_DIR / "pr-merge-gate.yml").read_text(encoding="utf-8"))
    e2e = workflow["jobs"]["e2e"]

    assert "image" in e2e["needs"], "the e2e slices must wait for the image job"

    steps = [step for step in e2e["steps"] if "ROSOTACOM_IMAGE_CACHE" in (step.get("env") or {})]
    assert len(steps) == 1, "exactly one e2e step consumes the published images"
    assert steps[0]["env"]["ROSOTACOM_IMAGE_CACHE"] == "${{ needs.image.outputs.repository }}", (
        "ROSOTACOM_IMAGE_CACHE must come from the image job's output, not from a literal repository"
    )
    assert workflow["jobs"]["image"]["outputs"]["repository"], "the image job must publish its repository as an output"


def _recipe_pytest_invocations(recipe: str) -> list[list[str]]:
    """Every pytest argument list a recipe runs, with just's substitutions applied."""
    import shlex

    text = JUSTFILE_PATH.read_text(encoding="utf-8")
    # `[^\n:]*` so a parameterised recipe (`test-e2e-slice slice:`) matches too.
    match = re.search(rf"^{re.escape(recipe)}[^\n:]*:[^\n]*\n(?P<body>(?:[ \t]+[^\n]*\n?)+)", text, re.MULTILINE)
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


def _e2e_conftest() -> ModuleType:
    """The slice manifest, loaded from tests/e2e/conftest.py by path.

    By path because `tests` is not an importable package and only pytest's own
    collection puts `tests/e2e` on `sys.path`; a contract test in a sibling
    directory cannot rely on that.
    """
    path = PACKAGE_ROOT / "tests" / "e2e" / "conftest.py"
    spec = importlib.util.spec_from_file_location("rosotacom_e2e_conftest", path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_e2e_slices_partition_the_whole_suite() -> None:
    """The slices must own exactly what the monolith collects, once each.

    `test-e2e-smoke` is `pytest tests/e2e/ -m e2e`, so it picks up any new file
    automatically; E2E_SLICES names node ids by hand and does not. Both halves
    have failed in production. Three files (anonymize, video-quality,
    benchmark-replay) and the two `[remote-assist-anonymized-*]` parameters
    once ran in no slice at all, because `-k "remote_assist"` misses a
    parameter id that spells it with hyphens. And
    `test_local_remote_assist_anonymized_smoke_from_copied_example_project` ran
    in *two* slices for as long as slices existed, because `-k "remote_assist"`
    and `-k "anonymized"` both match it — 262s of duplicated work per run that
    the old version of this test could not see, since it compared a union of
    the slices against the monolith and a union hides an overlap.
    """
    monolith = _collect_e2e_ids(["tests/e2e/", "-m", "e2e"])
    assert monolith, "collected no e2e tests; the monolith invocation changed"

    slices = _e2e_conftest().E2E_SLICES
    owned: set[str] = set()
    duplicated: dict[str, list[str]] = {}
    for name, tests in slices.items():
        for nodeid in tests:
            if nodeid in owned:
                duplicated.setdefault(nodeid, []).append(name)
            owned.add(nodeid)

    assert not duplicated, (
        "These e2e tests are owned by more than one slice, so the gate runs "
        "them twice:\n" + "\n".join(f"{nodeid} -> {names}" for nodeid, names in sorted(duplicated.items()))
    )
    assert not monolith - owned, (
        "These e2e tests run in `just test-e2e-smoke` but in no slice, so the "
        "merge gate and the release would not run them:\n" + "\n".join(sorted(monolith - owned))
    )
    assert not owned - monolith, (
        "These tests are owned by a slice but not collected by the monolith, "
        "so the two definitions disagree:\n" + "\n".join(sorted(owned - monolith))
    )


def test_workflow_matrices_expand_the_slices_that_exist() -> None:
    """The matrix names slices; E2E_SLICES defines them. A name in one and not
    the other is either a job that fails on an unknown `--e2e-slice` value or a
    set of tests nothing runs."""
    module = _e2e_conftest()
    assert sorted(_release_e2e_slices()) == sorted(module.E2E_SLICES), (
        f"the release matrix expands {sorted(_release_e2e_slices())} but E2E_SLICES defines {sorted(module.E2E_SLICES)}"
    )


def test_e2e_slices_stay_close_to_the_floor() -> None:
    """Balance is a number now, so it can be kept rather than rediscovered.

    Themed slices drifted to a 2.4x spread (7m39s against 18m31s) with nobody
    noticing, because the only place a per-test cost existed was a `pytest -q`
    total that nothing compared.

    Measured against the floor rather than the spread, which is what this test
    asserted at six slices and what stopped meaning anything at thirteen. The
    fixed cost per job compresses spread toward 1.0 as N grows, so a suite could
    go badly wrong while spread still read 1.1; and past N=12 the spread is set
    by tests too small to split rather than by imbalance. Distance to
    `floor_seconds()` says the one thing worth asserting at any N — the gate is
    near the fastest it could possibly be — and needs no retuning when a test is
    added.

    1.25x, not 1.0x, because the floor assumes tests can be divided arbitrarily
    and they cannot. At thirteen slices the partition sits at 1.09x.
    """
    module = _e2e_conftest()
    predicted = {name: module.predicted_slice_seconds(name) for name in module.E2E_SLICES}
    slowest = max(predicted.values())
    floor = module.floor_seconds()

    assert slowest <= 1.25 * floor, (
        f"the slowest e2e slice is predicted at {slowest / 60:.2f}m against a "
        f"{floor / 60:.2f}m floor ({slowest / floor:.2f}x). Move tests between slices, "
        f"or split the slowest one, in tests/e2e/conftest.py:\n{module.slice_cost_report()}"
    )


def test_the_floor_is_a_single_test_not_a_slice() -> None:
    """`floor_seconds()` has to stay the *suite's* floor, not the current
    partition's. It reads the slowest single test out of E2E_SLICES, so it
    would silently follow a bad partition if it summed a slice instead — and
    then the test above would be comparing the gate against itself.
    """
    module = _e2e_conftest()
    slowest_test = max(cost for tests in module.E2E_SLICES.values() for cost in tests.values())
    expected = module.RUNNER_SETUP_SECONDS + module.IMAGE_BUILD_SECONDS + slowest_test

    assert module.floor_seconds() == expected
    # The floor bounds the critical path, not every slice: a slice that does not
    # own the slowest test is free to be quicker than it.
    slowest_slice = max(module.predicted_slice_seconds(name) for name in module.E2E_SLICES)
    assert module.floor_seconds() <= slowest_slice, "the floor is above the critical path, so it is not a floor"


def test_slice_recipe_selects_by_slice_not_by_k() -> None:
    """One invocation, no `-k`. Every slice-shaped bug this repository has had
    came from a filter that was not the collection: a `-k` that missed a
    hyphenated parameter id, a `-k` that matched two slices' tests, and a `-k`
    from one file's selection deselecting every test in another file."""
    invocations = _recipe_pytest_invocations("test-e2e-slice")
    assert len(invocations) == 1, f"test-e2e-slice should run pytest once, runs {len(invocations)} times"
    args = invocations[0]
    assert "-k" not in args, f"test-e2e-slice must not filter with -k: {args}"
    assert "--e2e-slice={{slice}}" in args, f"test-e2e-slice must pass the slice through: {args}"


def test_each_slice_collects_exactly_what_it_owns() -> None:
    """The manifest is the intent; this is what pytest actually selects.

    Checked per slice rather than as a union, because a union is precisely what
    could not see one test being collected by two slices.
    """
    slices = _e2e_conftest().E2E_SLICES
    for name, tests in slices.items():
        collected = _collect_e2e_ids(["tests/e2e/", "-m", "e2e", f"--e2e-slice={name}"])
        assert collected == set(tests), f"slice {name!r} collects {sorted(collected)} but owns {sorted(tests)}"


class _FakeItem:
    """Enough of a pytest item for the collection hook."""

    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.keywords = {"e2e"}

    def add_marker(self, marker: object) -> None:
        pass


class _FakeConfig:
    def __init__(self, slice_name: str) -> None:
        self._slice_name = slice_name
        self.deselected: list[object] = []
        self.hook = self

    def getoption(self, name: str) -> str:
        assert name == "--e2e-slice"
        return self._slice_name

    def pytest_deselected(self, items: list[object]) -> None:
        self.deselected.extend(items)


def test_an_unowned_e2e_test_fails_every_slice_job() -> None:
    """An e2e test in no slice must stop the run, not quietly not run.

    Three e2e files once sat outside every slice for a release cycle and
    nothing went red, because the gate only ran what the slices named. Failing
    here means all six jobs say so on the first PR that adds a test.
    """
    module = _e2e_conftest()
    config = _FakeConfig("core")
    items = [_FakeItem("tests/e2e/test_smoke.py::test_a_test_nobody_assigned")]

    with pytest.raises(pytest.UsageError) as excinfo:
        module.pytest_collection_modifyitems(config, items)

    assert "test_a_test_nobody_assigned" in str(excinfo.value)
    assert "E2E_SLICES" in str(excinfo.value)


def test_every_workflow_installs_just_the_same_pinned_way() -> None:
    """`apt-get update` cost 9-15s in nineteen jobs to install one binary.

    It was also the only unpinned tool in CI: apt gave whatever noble shipped
    that week, so the `just` the gate ran changed without any commit saying so.
    Both are fixed by the shared action, and both come back the moment one
    workflow reintroduces the apt line — which is what this asserts.
    """
    offenders = [
        path.name
        for path in sorted(WORKFLOWS_DIR.glob("*.yml"))
        if "install -y just" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, "these workflows install just from apt instead of ./.github/actions/setup-just: " + ", ".join(
        offenders
    )

    action = PACKAGE_ROOT / ".github" / "actions" / "setup-just" / "action.yml"
    assert action.is_file(), "the shared setup-just action every workflow refers to does not exist"
    body = action.read_text(encoding="utf-8")
    assert "sha256sum --check" in body, "setup-just must verify the release it downloads"


def test_e2e_waits_only_for_the_cheap_gate_and_the_image() -> None:
    """The barrier `e2e` waits on has to stay cheaper than the image it also
    waits on, or it starts costing wall clock again.

    `needs: preflight-success` made every gate run wait 2.15 min for the
    five-version `merge-lightweight` matrix. It had stopped an e2e round once
    in sixty runs — on `workflow-lint`, which takes 0.12 min. `ci-success`
    still requires every preflight job, so this is a change to when thirteen
    runners start, not to what may merge.
    """
    workflow = yaml.safe_load((WORKFLOWS_DIR / "pr-merge-gate.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    assert set(jobs["e2e"]["needs"]) == {"quick-gate", "image"}, (
        f"e2e must wait for the cheap tripwire and the image, and nothing else: {jobs['e2e']['needs']}"
    )

    quick_gate_work = "\n".join(_workflow_run_blocks(jobs["quick-gate"]))
    expensive = [
        recipe for recipe in ("test-nondocker-cov", "test", "docs", "package") if f"just {recipe}" in quick_gate_work
    ]
    assert not expensive, "quick-gate exists to be cheaper than the image job, so it must not run: " + ", ".join(
        expensive
    )

    # And the jobs it stopped gating are still required to merge.
    ci_success = jobs["ci-success"]
    for name in ("workflow-lint", "build-lint", "merge-lightweight", "package"):
        assert name in ci_success["needs"], f"{name} no longer gates the merge at all"
