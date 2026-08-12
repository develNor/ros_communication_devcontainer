"""Collection rules for the Docker-backed e2e suite, and the CI slice partition.

`just test-e2e-smoke` runs the whole suite in one process; CI runs the same
collection as thirteen parallel slices, selected with `--e2e-slice=<name>`. A
slice is a deselection of one shared collection, not a separate pytest
invocation with its own file list — which is what it used to be, and that shape
got two things wrong that a partition cannot get wrong:

- `-k "remote_assist"` missed the `[remote-assist-anonymized-*]` parameters,
  because the parameter id spells it with hyphens (#198 found and fixed that);
- `-k "remote_assist"` and `-k "anonymized"` both matched
  `test_local_remote_assist_anonymized_smoke_from_copied_example_project`, so
  the gate ran that test in two slices every run (#226). The contract test
  compared a *union* of the slices against the monolith, and a union hides an
  overlap.

The number next to each test is what it costs. #226 balanced six themed slices
on those numbers and the run came back within 25s of every prediction, so #235
used the same model to choose N instead of guessing it: thirteen slices, split
where the tests already differ, is 1.09x the fastest any partition could be.
"""

from __future__ import annotations

import os

import pytest

#: Seconds a job spends before pytest starts: checkout, Python, `just setup`,
#: `docker info`. Measured across the e2e jobs of merge-gate runs 31597657446
#: and 31634604888 (43-60s).
RUNNER_SETUP_SECONDS = 55.0

#: Seconds the first test in a job pays to build the rosotacom project image.
#: Every job pays it exactly once, whichever test runs first, so it is a
#: constant no partition can remove — see #236, which is about removing it.
#: Because it is per job and not per test, it is also what makes more slices
#: cost more: at thirteen, 33 minutes of every gate run is this.
#:
#: Measured over 21 cold/warm pairs across seven runs: median 153.7s. A pair is
#: one test's duration as its job's first test against its median duration when
#: something else went first. Run 31634604888 supplied five of them at once,
#: because rebalancing changed which test each slice starts with.
IMAGE_BUILD_SECONDS = 154.0

#: Every e2e test, the slice that owns it, and its *warm* pytest `call` duration
#: in seconds — the median over seven runs of the invocations where the project
#: image already existed. Warm is the right unit because IMAGE_BUILD_SECONDS is
#: charged once per job on top; balancing warm costs balances wall clock.
#:
#: `derived` marks the three tests that have only ever run first in their job,
#: so their warm cost is the measured value minus IMAGE_BUILD_SECONDS. That
#: arithmetic is not a guess any more: #226 recorded five derived costs, #235's
#: rebalance moved two of them out of first position, and both came back within
#: 3s of the derivation (`link-latency` 146.9 derived / 145.0 measured,
#: `independent...in_parallel` 143.9 / 146.6).
#:
#: To refresh: every e2e invocation runs `--durations=0`, so a merge-gate run
#: prints every number here, and each slice's first test prints one more
#: measurement of IMAGE_BUILD_SECONDS. See docs/ci.md, "Balancing the e2e
#: slices".
E2E_SLICES: dict[str, dict[str, float]] = {
    # The smallest thing that has to work, split from `core` because two
    # heartbeat parameters and three chatter tests are two jobs' worth.
    "heartbeat": {
        "tests/e2e/test_smoke.py::test_local_heartbeat_smoke_matrix_from_copied_example_project[1-heartbeat]": 128.4,  # derived
        "tests/e2e/test_smoke.py::test_local_heartbeat_smoke_matrix_from_copied_example_project[status]": 120.8,
        # Skipped unless ROSOTACOM_RUN_FULL_E2E=1; nightly runs them one per job
        # through `just test-e2e-rmw`, so they cost this slice nothing.
        "tests/e2e/test_smoke.py::test_full_rmw_heartbeat_smoke_matrix[cyclone-local-zenoh-ros2dds-ota]": 0.0,
        "tests/e2e/test_smoke.py::test_full_rmw_heartbeat_smoke_matrix[cyclone-ota]": 0.0,
        "tests/e2e/test_smoke.py::test_full_rmw_heartbeat_smoke_matrix[fastdds]": 0.0,
        "tests/e2e/test_smoke.py::test_full_rmw_heartbeat_smoke_matrix[zen-endpoints]": 0.0,
    },
    "chatter": {
        "tests/e2e/test_smoke.py::test_native_chatter_scenario_starts_apps_and_communication_together": 103.3,
        "tests/e2e/test_smoke.py::test_local_native_chatter_smoke_from_copied_example_project[native-chatter]": 85.3,
        "tests/e2e/test_smoke.py::test_interactive_native_chatter_smoke_starts_full_local_debug_rig": 74.4,
    },
    # `transforms` was one slice over two unrelated questions. Compression and
    # payload size each keep their RMW pair together, because the pair is the
    # comparison: a red job here means "this transform, both transports" or
    # "this transform, one transport", which is the first thing you ask.
    "occupancy-grid": {
        "tests/e2e/test_smoke.py::test_local_compressed_occupancy_grid_smoke_from_copied_example_project[compressed-occupancy-grid]": 158.3,  # derived
        "tests/e2e/test_smoke.py::test_local_zenoh_compressed_occupancy_grid_smoke_from_copied_example_project[compressed-occupancy-grid-zenoh]": 150.4,
    },
    "sized-payload": {
        "tests/e2e/test_smoke.py::test_local_zenoh_sized_payload_smoke_from_copied_example_project[sized-payload-zenoh]": 172.5,
        "tests/e2e/test_smoke.py::test_local_wrapped_sized_payload_smoke_from_copied_example_project[sized-payload-fastdds]": 117.4,
    },
    # The full anonymized rig, alone: at 267s warm it is the slowest single test
    # in the suite and therefore sets the floor every other slice is measured
    # against. Nothing can share a job with it and stay under that floor.
    "remote-assist": {
        "tests/e2e/test_smoke.py::test_local_remote_assist_anonymized_smoke_from_copied_example_project[remote-assist-anonymized]": 267.0,
    },
    # Its two single-stream cuts, which share a trace with the rig above.
    "remote-assist-streams": {
        "tests/e2e/test_smoke.py::test_local_single_stream_anonymized_smoke_from_copied_example_project[remote-assist-anonymized-costmap]": 145.2,
        "tests/e2e/test_smoke.py::test_local_single_stream_anonymized_smoke_from_copied_example_project[remote-assist-anonymized-camera]": 145.1,
    },
    # What a live session exposes and what can be changed under it.
    "runtime-tools": {
        "tests/e2e/test_smoke.py::test_local_link_latency_smoke_exposes_metric_backbone[link-latency]": 145.0,
        "tests/e2e/test_timeline_stepping.py::test_timeline_steps_change_the_qdisc_tree_in_place": 1.4,
    },
    "media": {
        "tests/e2e/test_video_quality_e2e.py::test_synthetic_camera_pipeline_records_quality_metrics": 161.8,
        "tests/e2e/test_anonymize_e2e.py::test_anonymize_headless_end_to_end_preserves_keyframe_structure": 10.4,  # derived
    },
    "concurrency": {
        "tests/e2e/test_parallel_smoke.py::test_independent_local_smoke_tests_run_in_parallel": 146.6,
        "tests/e2e/test_parallel_smoke.py::test_second_same_target_smoke_aborts_and_leaves_first_intact": 125.0,
    },
    # The two benchmark verdicts, one job each: at 202s and 174s neither fits
    # anywhere without becoming the slowest slice.
    "benchmark-ab": {
        "tests/e2e/test_benchmark_ab.py::test_benchmark_ab_reliability_verdict": 202.5,
    },
    "benchmark-replay": {
        "tests/e2e/test_benchmark_replay.py::test_loss_boundaries_against_costmap_replay": 174.3,
    },
    # The band-asserted rows, which gate against committed bands
    # (docs/performance-bands.md), separated from the good/bad-case pair that
    # only checks the verdict logic. Different failures, different jobs.
    "benchmark-capacity": {
        "tests/e2e/test_benchmark_capacity.py::test_merge_gate_row_is_band_asserted[probe-loss-gop-tight-cyclone]": 141.6,
        "tests/e2e/test_benchmark_capacity.py::test_benchmark_probe_camera_load": 87.4,
    },
    "benchmark-capacity-cases": {
        "tests/e2e/test_benchmark_capacity.py::test_benchmark_capacity_good_case": 93.1,
        "tests/e2e/test_benchmark_capacity.py::test_benchmark_capacity_bad_case": 91.4,
    },
}


def slice_owners() -> dict[str, str]:
    """Node id -> the slice that runs it."""
    return {nodeid: name for name, tests in E2E_SLICES.items() for nodeid in tests}


def predicted_slice_seconds(name: str) -> float:
    """Wall clock a slice's CI job should take, setup and image build included."""
    return RUNNER_SETUP_SECONDS + IMAGE_BUILD_SECONDS + sum(E2E_SLICES[name].values())


def floor_seconds() -> float:
    """The fastest any partition of this suite could possibly be.

    One job has to run the slowest single test, and it pays the fixed cost like
    every other job. No number of slices gets below this, which is why "how
    close to the floor" is the honest way to judge a partition and "spread" is
    not: spread is compressed toward 1.0 by the fixed cost as N grows, so it
    keeps looking fine while the gate stops improving.
    """
    return (
        RUNNER_SETUP_SECONDS
        + IMAGE_BUILD_SECONDS
        + max(cost for tests in E2E_SLICES.values() for cost in tests.values())
    )


def slice_cost_report() -> str:
    """The balance table, for a failure message or `just e2e-slice-costs`."""
    lines = [f"{'slice':<26} {'tests':>5} {'pytest':>8} {'job':>8}"]
    for name in sorted(E2E_SLICES, key=predicted_slice_seconds, reverse=True):
        job = predicted_slice_seconds(name)
        tests = E2E_SLICES[name]
        lines.append(f"{name:<26} {len(tests):5d} {sum(tests.values()):7.0f}s {job / 60:7.2f}m")
    slowest = max(map(predicted_slice_seconds, E2E_SLICES))
    floor = floor_seconds()
    lines.append(
        f"critical path {slowest / 60:.2f}m over {len(E2E_SLICES)} jobs, "
        f"{slowest / floor:.2f}x the {floor / 60:.2f}m floor"
    )
    return "\n".join(lines)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e-slice",
        default=None,
        choices=sorted(E2E_SLICES),
        help="Run only the e2e tests this CI slice owns; see E2E_SLICES in tests/e2e/conftest.py.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_e2e = pytest.mark.skip(reason="set ROSOTACOM_RUN_E2E=1 to run Docker E2E tests")
    run_e2e = os.environ.get("ROSOTACOM_RUN_E2E") == "1"

    for item in items:
        if "e2e" in item.keywords and not run_e2e:
            item.add_marker(skip_e2e)

    slice_name = config.getoption("--e2e-slice")
    if slice_name is None:
        return

    owners = slice_owners()
    e2e_items = [item for item in items if "e2e" in item.keywords]

    unassigned = sorted(item.nodeid for item in e2e_items if item.nodeid not in owners)
    if unassigned:
        # Loud on purpose, and in every slice at once. An e2e test that belongs
        # to no slice is a test the merge gate does not run, which is how three
        # files and two parameters went unchecked for a release cycle.
        raise pytest.UsageError(
            "These e2e tests belong to no slice, so no CI job would run them:\n"
            + "\n".join(unassigned)
            + "\n\nAdd each to E2E_SLICES in tests/e2e/conftest.py with its `--durations=0`"
            + " cost. Current balance:\n"
            + slice_cost_report()
        )

    deselected = [item for item in e2e_items if owners[item.nodeid] != slice_name]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        dropped = {id(item) for item in deselected}
        items[:] = [item for item in items if id(item) not in dropped]


if __name__ == "__main__":  # `just e2e-slice-costs`
    print(slice_cost_report())
