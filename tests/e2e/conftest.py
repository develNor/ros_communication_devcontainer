"""Collection rules for the Docker-backed e2e suite, and the CI slice partition.

`just test-e2e-smoke` runs the whole suite in one process; CI runs the same
collection as six parallel slices, selected with `--e2e-slice=<name>`. A slice
is a deselection of one shared collection, not a separate pytest invocation
with its own file list — which is what it used to be, and that shape got two
things wrong that a partition cannot get wrong:

- `-k "remote_assist"` missed the `[remote-assist-anonymized-*]` parameters,
  because the parameter id spells it with hyphens (#198 found and fixed that);
- `-k "remote_assist"` and `-k "anonymized"` both matched
  `test_local_remote_assist_anonymized_smoke_from_copied_example_project`, so
  the gate ran that test in two slices every run. The contract test compared a
  *union* of the slices against the monolith, and a union hides an overlap.

The number next to each test is what it costs, which is the other half of
#226: slices were grouped by theme and nothing recorded a per-test cost, so
the imbalance (2.4x between the fastest and slowest job) was invisible unless
somebody diffed job durations by hand. Balance is now a reviewable number, and
`tests/contract/test_workflow_contracts.py` fails when it drifts.
"""

from __future__ import annotations

import os

import pytest

#: Seconds a job spends before pytest starts: checkout, Python, `just setup`,
#: `docker info`. Measured across the six e2e jobs of merge-gate run
#: 31597657446 (43-60s).
RUNNER_SETUP_SECONDS = 55.0

#: Seconds the first test in a job pays to build the rosotacom project image.
#: Every job pays this exactly once, whichever test happens to run first, so it
#: is a constant that balancing cannot remove — only running fewer, larger
#: slices can. Measured as the gap between the two invocations of the one test
#: that used to run in two slices: median 419s as its job's first test against
#: median 262s as a later one, over the six runs of 2026-08-11/12.
IMAGE_BUILD_SECONDS = 155.0

#: Every e2e test, the slice that owns it, and its *warm* pytest `call`
#: duration in seconds — the median over those six runs of a run where the
#: project image already existed. Warm is the right unit because
#: IMAGE_BUILD_SECONDS is charged once per job on top; balancing warm costs is
#: therefore the same thing as balancing wall clock.
#:
#: `derived` marks a test that has only ever run as its job's first test, so
#: its warm cost is the measured value minus IMAGE_BUILD_SECONDS. Two sibling
#: parameters check that arithmetic: `[1-heartbeat]` derives to 128.6s against
#: a measured 123.1s for `[status]`, and `[compressed-occupancy-grid]` derives
#: to 159.9s against a measured 150.4s for its zenoh twin.
#:
#: To refresh: every e2e invocation runs `--durations=0`, so a merge-gate run
#: prints every number here. See docs/ci.md, "Balancing the e2e slices".
E2E_SLICES: dict[str, dict[str, float]] = {
    # Heartbeat and chatter: the smallest thing that has to work.
    "core": {
        "tests/e2e/test_smoke.py::test_local_heartbeat_smoke_matrix_from_copied_example_project[1-heartbeat]": 128.6,  # derived
        "tests/e2e/test_smoke.py::test_local_heartbeat_smoke_matrix_from_copied_example_project[status]": 123.1,
        "tests/e2e/test_smoke.py::test_native_chatter_scenario_starts_apps_and_communication_together": 104.1,
        "tests/e2e/test_smoke.py::test_local_native_chatter_smoke_from_copied_example_project[native-chatter]": 85.3,
        "tests/e2e/test_smoke.py::test_interactive_native_chatter_smoke_starts_full_local_debug_rig": 73.8,
        # Skipped unless ROSOTACOM_RUN_FULL_E2E=1; nightly runs them one per
        # job through `just test-e2e-rmw`, so they cost this slice nothing.
        "tests/e2e/test_smoke.py::test_full_rmw_heartbeat_smoke_matrix[cyclone-local-zenoh-ros2dds-ota]": 0.0,
        "tests/e2e/test_smoke.py::test_full_rmw_heartbeat_smoke_matrix[cyclone-ota]": 0.0,
        "tests/e2e/test_smoke.py::test_full_rmw_heartbeat_smoke_matrix[fastdds]": 0.0,
        "tests/e2e/test_smoke.py::test_full_rmw_heartbeat_smoke_matrix[zen-endpoints]": 0.0,
    },
    # What the transport does to a payload: compression and size, each over
    # both RMW paths.
    "transforms": {
        "tests/e2e/test_smoke.py::test_local_zenoh_sized_payload_smoke_from_copied_example_project[sized-payload-zenoh]": 172.3,
        "tests/e2e/test_smoke.py::test_local_compressed_occupancy_grid_smoke_from_copied_example_project[compressed-occupancy-grid]": 159.9,  # derived
        "tests/e2e/test_smoke.py::test_local_zenoh_compressed_occupancy_grid_smoke_from_copied_example_project[compressed-occupancy-grid-zenoh]": 150.4,
        "tests/e2e/test_smoke.py::test_local_wrapped_sized_payload_smoke_from_copied_example_project[sized-payload-fastdds]": 117.3,
    },
    # All three anonymized remote-assist scenarios. The full rig and its two
    # single-stream cuts belong together: they share a trace and a failure
    # mode, and splitting them across slices is how one of them ended up
    # running twice.
    "remote-assist": {
        "tests/e2e/test_smoke.py::test_local_remote_assist_anonymized_smoke_from_copied_example_project[remote-assist-anonymized]": 262.0,
        "tests/e2e/test_smoke.py::test_local_single_stream_anonymized_smoke_from_copied_example_project[remote-assist-anonymized-costmap]": 147.8,
        "tests/e2e/test_smoke.py::test_local_single_stream_anonymized_smoke_from_copied_example_project[remote-assist-anonymized-camera]": 146.3,
    },
    # Everything built on top of a running session: the latency probe, live
    # timeline shaping, and the two benchmark verdicts.
    "runtime-tools": {
        "tests/e2e/test_benchmark_ab.py::test_benchmark_ab_reliability_verdict": 202.5,
        "tests/e2e/test_benchmark_replay.py::test_loss_boundaries_against_costmap_replay": 174.2,
        "tests/e2e/test_smoke.py::test_local_link_latency_smoke_exposes_metric_backbone[link-latency]": 146.9,  # derived
        "tests/e2e/test_timeline_stepping.py::test_timeline_steps_change_the_qdisc_tree_in_place": 1.4,
    },
    # The band-asserted capacity probes, split out of `runtime-tools`: they
    # were two thirds of the slowest slice and they gate on committed bands
    # (docs/performance-bands.md), so a red job here means one thing.
    "benchmark-capacity": {
        "tests/e2e/test_benchmark_capacity.py::test_merge_gate_row_is_band_asserted[probe-loss-gop-tight-cyclone]": 142.0,
        "tests/e2e/test_benchmark_capacity.py::test_benchmark_capacity_good_case": 93.1,
        "tests/e2e/test_benchmark_capacity.py::test_benchmark_capacity_bad_case": 91.6,
        "tests/e2e/test_benchmark_capacity.py::test_benchmark_probe_camera_load": 88.4,
    },
    # Two themes in one slice, because at 177s and 265s neither fills a job and
    # a job costs 210s before it runs a test. The name says so rather than
    # hiding it under one of the two.
    "media-concurrency": {
        "tests/e2e/test_video_quality_e2e.py::test_synthetic_camera_pipeline_records_quality_metrics": 164.3,
        "tests/e2e/test_parallel_smoke.py::test_independent_local_smoke_tests_run_in_parallel": 143.9,  # derived
        "tests/e2e/test_parallel_smoke.py::test_second_same_target_smoke_aborts_and_leaves_first_intact": 120.8,
        "tests/e2e/test_anonymize_e2e.py::test_anonymize_headless_end_to_end_preserves_keyframe_structure": 12.3,  # derived
    },
}


def slice_owners() -> dict[str, str]:
    """Node id -> the slice that runs it."""
    return {nodeid: name for name, tests in E2E_SLICES.items() for nodeid in tests}


def predicted_slice_seconds(name: str) -> float:
    """Wall clock a slice's CI job should take, setup and image build included."""
    return RUNNER_SETUP_SECONDS + IMAGE_BUILD_SECONDS + sum(E2E_SLICES[name].values())


def slice_cost_report() -> str:
    """The balance table, for a failure message or `just e2e-slice-costs`."""
    lines = [f"{'slice':<20} {'tests':>5} {'pytest':>8} {'job':>8}"]
    for name in sorted(E2E_SLICES, key=predicted_slice_seconds, reverse=True):
        job = predicted_slice_seconds(name)
        tests = E2E_SLICES[name]
        lines.append(f"{name:<20} {len(tests):5d} {sum(tests.values()):7.0f}s {job / 60:7.1f}m")
    slowest = max(map(predicted_slice_seconds, E2E_SLICES))
    fastest = min(map(predicted_slice_seconds, E2E_SLICES))
    lines.append(f"critical path {slowest / 60:.1f}m, spread {slowest / fastest:.2f}x, {len(E2E_SLICES)} jobs")
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
