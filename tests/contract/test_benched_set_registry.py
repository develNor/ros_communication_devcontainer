"""Cross-file contracts of the benched set (RFC 0007 §4).

The registry is only a gate if its references hold: every row names a committed
profile, every gated metric has a calibrated band, random elements are seeded,
and the CI lanes actually consume the registry instead of hardcoding rows.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rosotacom.benched_set import GateRow, load_registry, profiles_for_row
from rosotacom.benchmark import UNCALIBRATED_FINGERPRINT, load_bands
from rosotacom.network_profiles import Profile, load_profiles_file

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
BUDGETS_PATH = PACKAGE_ROOT / "budgets.jsonl"
PROFILES_PATH = PACKAGE_ROOT / "src" / "rosotacom" / "resources" / "examples" / "profiles.yaml"
WORKFLOWS_DIR = PACKAGE_ROOT / ".github" / "workflows"
GATE_WORKFLOW = WORKFLOWS_DIR / "benchmark-gate.yml"
CALIBRATE_WORKFLOW = WORKFLOWS_DIR / "benchmark-calibrate.yml"
JUSTFILE = PACKAGE_ROOT / "justfile"


@pytest.fixture(scope="module")
def rows() -> list[GateRow]:
    return load_registry()


@pytest.fixture(scope="module")
def profiles() -> dict[str, Profile]:
    return load_profiles_file(PROFILES_PATH)


def test_every_row_names_a_committed_public_profile(rows: list[GateRow], profiles: dict[str, Profile]) -> None:
    referenced = {profile for row in rows for profile in profiles_for_row(row)}
    missing = sorted(referenced - set(profiles))
    assert not missing, (
        f"registry rows reference profiles {missing} that are not committed in {PROFILES_PATH.name}; "
        "gate profiles are public, plain-netem, and live next to the other example profiles"
    )


def _random_netem_directions(profile: Profile) -> list[str]:
    """Directions whose netem uses randomness (jitter/loss/reorder/duplicate)."""

    def shaping_is_random(shaping: object) -> bool:
        return shaping is not None and any(
            getattr(shaping, name, None) is not None
            for name in ("jitter_ms", "loss_pct", "reorder_pct", "duplicate_pct")
        )

    random_dirs: list[str] = []
    segments = profile.timeline if profile.is_timeline else [profile]
    for index, segment in enumerate(segments):
        for direction in ("uplink", "downlink"):
            shaping = getattr(segment, direction, None)
            if shaping_is_random(shaping):
                random_dirs.append(f"{profile.name}[{index}].{direction}")
    return random_dirs


def test_public_gate_profiles_use_deterministic_netem(rows: list[GateRow], profiles: dict[str, Profile]) -> None:
    """Public gate profiles must shape with *deterministic* netem only —
    rate/delay bottlenecks, no random loss/jitter/reorder/duplicate.

    The vision's "seed policy is explicit" is realizable for the seeded *load*
    (application-level RNG in sized_publisher, committed in the registry) but
    NOT for random *netem* on the public runner class: the tc on GitHub-hosted
    runners rejects the netem `seed` option (observed: `netem … loss 8% seed 1
    -> What is "seed"?`), so a seeded-random public profile cannot even be
    installed there, and an unseeded one is non-deterministic — either way it
    must not gate. Determinism for public rows therefore comes from the pinned
    bottleneck (rate/delay), which needs no seed. Seeded-random netem stays in
    the operator harness lanes on a tc that supports it (RFC 0007 §3, honest
    limits)."""
    problems: list[str] = []
    for profile_name in sorted({profile for row in rows for profile in profiles_for_row(row)}):
        problems += _random_netem_directions(profiles[profile_name])
    assert not problems, (
        "random netem in public gate profiles (github-hosted tc cannot seed it, so it cannot gate "
        f"deterministically): {problems}. Use a rate/delay bottleneck instead."
    )


def test_every_gated_metric_has_a_calibrated_band(rows: list[GateRow]) -> None:
    """The registry without calibrated bands is a monitor pretending to gate:
    every (row, profile, gated metric) needs a band whose provenance names a
    real runner class (mint them: the benchmark-calibrate workflow +
    `rosotacom benchmark calibrate`)."""
    bands = {band.key: band for band in load_bands(BUDGETS_PATH)}
    problems: list[str] = []
    for row in rows:
        for metric in row.metrics:
            band = bands.get((row.id, row.profile, metric))
            if band is None:
                problems.append(f"({row.id}, {row.profile}, {metric}): no band committed")
            elif band.provenance.fingerprint == UNCALIBRATED_FINGERPRINT:
                problems.append(f"({row.id}, {row.profile}, {metric}): band is uncalibrated")
    assert not problems, "budgets.jsonl is missing calibrated bands:\n" + "\n".join(problems)


def test_committed_bands_belong_to_registry_rows(rows: list[GateRow]) -> None:
    """No orphan bands: every committed band is one a benched row still gates,
    so a renamed/removed row cannot leave stale envelopes behind."""
    expected = {(row.id, row.profile, metric) for row in rows for metric in row.metrics}
    orphans = [band.key for band in load_bands(BUDGETS_PATH) if band.key not in expected]
    assert not orphans, f"budgets.jsonl carries bands no registry row gates: {orphans}"


def test_gate_workflows_exist_and_consume_the_registry(rows: list[GateRow]) -> None:
    """The lanes read the benched set through the CLI; no workflow hardcodes a
    row id (adding a row must be a registry edit, not a CI edit)."""
    assert GATE_WORKFLOW.is_file(), "the nightly benchmark gate workflow is missing"
    assert CALIBRATE_WORKFLOW.is_file(), "the calibration workflow is missing"

    gate_text = GATE_WORKFLOW.read_text(encoding="utf-8")
    calibrate_text = CALIBRATE_WORKFLOW.read_text(encoding="utf-8")
    assert "benchmark rows" in gate_text, "the gate workflow must list its matrix via `benchmark rows`"
    assert "benchmark row " in gate_text or "benchmark row \\" in gate_text or 'benchmark row "' in gate_text
    assert "gate-summary" in gate_text, "the gate workflow must aggregate the machine-readable verdicts"
    assert "benchmark rows" in calibrate_text
    assert "benchmark calibrate" in calibrate_text

    for workflow in sorted(WORKFLOWS_DIR.glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        for row in rows:
            assert row.id not in text, (
                f"{workflow.name} hardcodes benched row {row.id!r}; workflows must read the registry"
            )

    import yaml

    gate_doc = yaml.safe_load(gate_text)
    triggers = gate_doc.get("on") or gate_doc.get(True) or {}
    assert "schedule" in triggers, "the benchmark gate must run nightly"
    assert "workflow_dispatch" in triggers, "the benchmark gate must be manually dispatchable"


def test_merge_gate_lane_actually_selects_the_benchmark_e2e() -> None:
    """Regression contract for the silent deselection this work found: the
    runtime-tools recipe must run both benchmark E2E files
    (tests/e2e/test_benchmark_capacity.py and tests/e2e/test_benchmark_ab.py) in a
    pytest invocation that carries no `-k` filter (a `-k` from another file's
    selection would deselect every benchmark test without failing)."""
    recipe = re.search(
        r"^test-e2e-runtime-tools:\n((?:\t.*\n?)+)",
        JUSTFILE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert recipe, "justfile recipe test-e2e-runtime-tools is missing"
    for e2e_file in ("test_benchmark_capacity.py", "test_benchmark_ab.py"):
        benchmark_lines = [line for line in recipe.group(1).splitlines() if e2e_file in line]
        assert benchmark_lines, f"test-e2e-runtime-tools no longer runs tests/e2e/{e2e_file}"
        poisoned = [line for line in benchmark_lines if re.search(r"\s-k\s", line)]
        assert not poisoned, f"a -k filter would silently deselect the benchmark E2E {e2e_file}: {poisoned}"
