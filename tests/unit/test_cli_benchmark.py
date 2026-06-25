"""Host-deterministic tests for the benchmark CLI glue (RFC 0005 § 1d).

These test the *wiring* — the driver functions that connect the pure
``benchmark.py`` logic to a ``run_point`` probe — with a **stubbed probe**
that returns canned transit summaries. The pure logic itself is already
covered by ``test_benchmark.py``; these verify the CLI-level orchestration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rosotacom.cli_benchmark import (
    BENCHMARK_RESULT_FILE,
    DEFAULT_BENCHMARK_RMW,
    _parse_values,
    _prepare_benchmark_session_config,
    collect_transit_summary,
    drive_capacity,
    drive_ramp,
    drive_sweep,
    register_benchmark_parser,
)

# --------------------------------------------------------------------------- #
# Stubbed run_point
# --------------------------------------------------------------------------- #


def _make_stub_probe(
    *,
    loss_pct: float = 0.0,
    latency_p95: float = 50.0,
    breakpoint_size: int | None = None,
) -> Any:
    """A stubbed run_point that returns canned transit summaries.

    When ``breakpoint_size`` is set, the probe fails (100 % loss) above that
    size — simulating a capacity breakpoint for the binary-search tests.
    """

    def stub(*, profile: str | None, load: dict[str, Any], duration_s: float, out_dir: Path) -> dict[str, Any]:
        size = load.get("size_a", 1000)
        if breakpoint_size is not None and size > breakpoint_size:
            return {
                "topics": {
                    "/test": {
                        "expected": 100,
                        "delivered": 0,
                        "lost": 100,
                        "loss_pct": 100.0,
                        "reordered": 0,
                        "ota_hop_ms": {"p50": None, "p95": None},
                        "jitter_ms": {"p50": None, "p95": None},
                    }
                }
            }
        return {
            "topics": {
                "/test": {
                    "expected": 100,
                    "delivered": 100 - int(loss_pct),
                    "lost": int(loss_pct),
                    "loss_pct": loss_pct,
                    "reordered": 0,
                    "ota_hop_ms": {"p50": latency_p95 * 0.6, "p95": latency_p95},
                    "jitter_ms": {"p50": 2.0, "p95": 5.0},
                }
            }
        }

    return stub


# --------------------------------------------------------------------------- #
# Capacity driver
# --------------------------------------------------------------------------- #


def test_capacity_driver_finds_breakpoint_with_stubbed_probe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The capacity driver finds the binary-search breakpoint against a stubbed
    metric source that fails above 8000 B."""
    probe = _make_stub_probe(breakpoint_size=8000)
    result = drive_capacity(
        probe,
        profile="test-profile",
        knob="size",
        low=1000,
        high=70000,
        max_loss_pct=5.0,
        max_latency_ms=200.0,
        duration_s=1.0,
        out_dir=tmp_path,
    )
    assert result["capacity"] == 8000
    assert result["slice"]["knob"] == "size"
    assert result["slice"]["profile"] == "test-profile"
    # Budget file was written.
    assert (tmp_path / "budgets.jsonl").exists()
    result_doc = json.loads((tmp_path / BENCHMARK_RESULT_FILE).read_text(encoding="utf-8"))
    assert result_doc["configuration"]["thresholds"]["max_loss_pct"] == 5.0
    assert result_doc["result"]["capacity"] == 8000
    assert result_doc["verdict"]["passed"] is True
    assert result_doc["measurements"]["probes"]
    assert result_doc["measurements"]["probes"][0]["topics"][0]["loss_pct"] in (0.0, 100.0)
    assert "offered_bw=" in capsys.readouterr().out


def test_capacity_driver_returns_none_when_low_fails(tmp_path: Path) -> None:
    """If even the lowest probe value fails, capacity is None."""
    probe = _make_stub_probe(breakpoint_size=0)  # everything fails
    result = drive_capacity(
        probe,
        profile="p",
        knob="size",
        low=1000,
        high=10000,
        max_loss_pct=1.0,
        max_latency_ms=100.0,
        duration_s=1.0,
        out_dir=tmp_path,
    )
    assert result["capacity"] is None


# --------------------------------------------------------------------------- #
# Ramp driver
# --------------------------------------------------------------------------- #


def test_ramp_driver_builds_curve_with_stubbed_probe(tmp_path: Path) -> None:
    """The ramp driver builds the latency-vs-load curve from stubbed points."""
    probe = _make_stub_probe(latency_p95=50.0)
    curve = drive_ramp(
        probe,
        profile="p",
        values=[1000, 2000, 4000],
        duration_s=1.0,
        out_dir=tmp_path,
    )
    assert len(curve) == 3
    assert all("value" in point and "metric" in point for point in curve)
    assert curve[0]["value"] == 1000.0
    # Curve file was written.
    assert (tmp_path / "curve.jsonl").exists()
    assert (tmp_path / BENCHMARK_RESULT_FILE).exists()


# --------------------------------------------------------------------------- #
# Recovery driver
# --------------------------------------------------------------------------- #


def test_recovery_driver_extracts_metrics_from_synthetic_timeline(tmp_path: Path) -> None:
    """The recovery driver feeds transit records through recovery_metrics."""
    from rosotacom.benchmark import OutageWindow, recovery_metrics

    # Build a synthetic events.jsonl under the expected path.
    events_dir = tmp_path / "logs" / "a" / "status"
    events_dir.mkdir(parents=True)
    events_path = events_dir / "events.jsonl"

    records = [
        {
            "kind": "transit",
            "topic": "/x",
            "seq": 0,
            "status": "delivered",
            "t_wrap": 0.0,
            "sections": {"ota_hop_ms": 100.0},
        },
        {
            "kind": "transit",
            "topic": "/x",
            "seq": 1,
            "status": "delivered",
            "t_wrap": 1.0,
            "sections": {"ota_hop_ms": 100.0},
        },
        {
            "kind": "transit",
            "topic": "/x",
            "seq": 2,
            "status": "lost",
            "t_wrap": None,
            "sections": {"ota_hop_ms": None},
        },
        {
            "kind": "transit",
            "topic": "/x",
            "seq": 3,
            "status": "delivered",
            "t_wrap": 3.0,
            "sections": {"ota_hop_ms": 100.0},
        },
        {
            "kind": "transit",
            "topic": "/x",
            "seq": 4,
            "status": "delivered",
            "t_wrap": 4.0,
            "sections": {"ota_hop_ms": 100.0},
        },
    ]
    events_path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )

    # Direct recovery_metrics call (the driver's core logic).
    from rosotacom.transit import join_transit_records, load_transit_records

    loaded = join_transit_records(load_transit_records([events_path]))
    metrics = recovery_metrics(
        loaded,
        OutageWindow(start=1.5, end=2.5),
        nominal_period_s=1.0,
        latched_topics=["/x"],
    )
    assert metrics.lost_during_outage == {"/x": 1}
    assert metrics.latched_rearrival == {"/x": True}


# --------------------------------------------------------------------------- #
# Sweep driver
# --------------------------------------------------------------------------- #


def test_sweep_driver_runs_grid_with_stubbed_probe(tmp_path: Path) -> None:
    """The sweep driver runs one point per profile and reports oracle results."""
    probe = _make_stub_probe(loss_pct=1.0, latency_p95=80.0)
    frontier = drive_sweep(
        probe,
        profile_grid=["profile-a", "profile-b"],
        max_loss_pct=5.0,
        max_latency_ms=200.0,
        duration_s=1.0,
        out_dir=tmp_path,
    )
    assert len(frontier) == 2
    assert frontier[0]["profile"] == "profile-a"
    assert frontier[1]["profile"] == "profile-b"
    # Both should pass with our lenient thresholds.
    assert all(row["passes"] for row in frontier)
    # Frontier file was written.
    assert (tmp_path / "frontier.jsonl").exists()
    assert (tmp_path / BENCHMARK_RESULT_FILE).exists()


# --------------------------------------------------------------------------- #
# Budget save/load/compare roundtrip (CLI-level)
# --------------------------------------------------------------------------- #


def test_budget_roundtrip_from_capacity_run(tmp_path: Path) -> None:
    """A capacity run writes a budget file that can be loaded and compared."""
    from rosotacom.benchmark import BudgetKey, Direction, MetricSpec, compare_to_budget, find_baseline, load_budget

    probe = _make_stub_probe(breakpoint_size=5000)
    drive_capacity(
        probe,
        profile="p",
        knob="size",
        low=1000,
        high=10000,
        max_loss_pct=5.0,
        max_latency_ms=200.0,
        duration_s=1.0,
        out_dir=tmp_path,
    )
    entries = load_budget(tmp_path / "budgets.jsonl")
    assert len(entries) == 1
    baseline = find_baseline(entries, profile="p", genre="capacity")
    assert baseline is not None
    assert baseline.metrics["capacity_size"] == 5000.0

    # Compare against a slightly lower current value — no regression within tolerance.
    specs = [MetricSpec("capacity_size", Direction.HIGHER_IS_BETTER, rel_tolerance=0.1)]
    comparison = compare_to_budget(
        BudgetKey(sha="test", profile="p", genre="capacity"),
        specs,
        baseline.metrics,
        {"capacity_size": 4800.0},
    )
    assert not comparison.regressed  # 4800 is within 10% of 5000


# --------------------------------------------------------------------------- #
# Value parser
# --------------------------------------------------------------------------- #


def test_parse_values_comma_separated() -> None:
    assert _parse_values("1000,2000,4000") == [1000.0, 2000.0, 4000.0]


def test_parse_values_range_syntax() -> None:
    values = _parse_values("1000:5000:1000")
    assert values == [1000.0, 2000.0, 3000.0, 4000.0, 5000.0]


def test_parse_values_linspace_syntax() -> None:
    values = _parse_values("0..100/5")
    assert len(values) == 5
    assert values[0] == 0.0
    assert values[-1] == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
# Transit-record collection
# --------------------------------------------------------------------------- #


def test_collect_transit_summary_from_fixture(tmp_path: Path) -> None:
    """Transit records from a fixture directory structure are summarized correctly."""
    events_dir = tmp_path / "logs" / "a" / "status"
    events_dir.mkdir(parents=True)
    events_path = events_dir / "events.jsonl"
    records = [
        {
            "kind": "transit",
            "topic": "/t",
            "seq": 0,
            "status": "delivered",
            "t_wrap": 0.0,
            "sections": {"ota_hop_ms": 50.0},
        },
        {
            "kind": "transit",
            "topic": "/t",
            "seq": 1,
            "status": "delivered",
            "t_wrap": 0.1,
            "sections": {"ota_hop_ms": 55.0},
        },
        {
            "kind": "transit",
            "topic": "/t",
            "seq": 2,
            "status": "lost",
            "t_wrap": None,
            "sections": {"ota_hop_ms": None},
        },
    ]
    events_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    summary = collect_transit_summary(tmp_path)
    topics = summary.get("topics", {})
    # There should be one topic entry.
    assert len(topics) >= 1
    topic_key = next(iter(topics))
    assert topics[topic_key]["expected"] == 3
    assert topics[topic_key]["delivered"] == 2
    assert topics[topic_key]["lost"] == 1


# --------------------------------------------------------------------------- #
# Argparse wiring
# --------------------------------------------------------------------------- #


def test_benchmark_subcommand_arg_parsing() -> None:
    """The benchmark subcommand and its sub-subcommands parse without error."""
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_benchmark_parser(subparsers)

    # Capacity.
    args = parser.parse_args(
        [
            "benchmark",
            "capacity",
            "--profile",
            "p",
            "--knob",
            "size",
            "--low",
            "1000",
            "--high",
            "10000",
            "--max-loss",
            "5",
            "--max-latency-ms",
            "200",
        ]
    )
    assert args.benchmark_command == "capacity"
    assert args.knob == "size"
    assert args.rmw == DEFAULT_BENCHMARK_RMW

    args = parser.parse_args(
        [
            "benchmark",
            "capacity",
            "--profile",
            "p",
            "--knob",
            "size",
            "--low",
            "1000",
            "--high",
            "10000",
            "--max-loss",
            "5",
            "--max-latency-ms",
            "200",
            "--rmw",
            "fastdds",
        ]
    )
    assert args.rmw == "fastdds"

    # Ramp.
    args = parser.parse_args(["benchmark", "ramp", "--profile", "p", "--values", "1000,2000"])
    assert args.benchmark_command == "ramp"

    # Recovery.
    args = parser.parse_args(["benchmark", "recovery", "--profile", "timeline-p"])
    assert args.benchmark_command == "recovery"

    # Sweep.
    args = parser.parse_args(["benchmark", "sweep", "--profile-grid", "p1,p2"])
    assert args.benchmark_command == "sweep"

    # Plot.
    args = parser.parse_args(["benchmark", "plot", "results.jsonl"])
    assert args.benchmark_command == "plot"
    assert args.input == "results.jsonl"


def test_runtime_config_parses_benchmarks_dir_and_profiles(tmp_path: Path) -> None:
    import yaml

    from rosotacom.cli import _load_runtime_config

    config_file = tmp_path / "rosotacom.yaml"
    ros2docker = tmp_path / "ros2docker.json"
    ros2docker.write_text("{}", encoding="utf-8")

    config_data = {
        "ros2docker_config": str(ros2docker),
        "session_configs_dir": [],
        "scenario_configs_dir": [],
        "session_instances_dir": "session-instances",
        "profiles": "profiles.yaml",
        "benchmarks_dir": "benchmarks",
    }
    config_file.write_text(yaml.safe_dump(config_data), encoding="utf-8")

    # Write a dummy profiles file
    profiles_file = tmp_path / "profiles.yaml"
    profiles_file.write_text("profiles: {}", encoding="utf-8")

    import argparse

    args = argparse.Namespace(
        rosotacom_config=str(config_file),
        ros2docker_config=None,
        session_configs_dir=None,
        scenario_configs_dir=None,
        session_instances_dir=None,
        deployment=None,
        profiles_file=None,
    )
    runtime = _load_runtime_config(args)

    assert runtime.profiles_file == profiles_file
    assert runtime.benchmarks_dir == tmp_path / "benchmarks"


def test_benchmark_session_copy_pins_requested_rmw(tmp_path: Path) -> None:
    import argparse

    import yaml

    project = tmp_path / "project"
    session_dir = project / "sessions" / "bench_1_1_capacity"
    session_dir.mkdir(parents=True)
    (session_dir / "session-definition.yaml").write_text(
        "peers:\n  a: {}\n  b: {}\nshared:\n  rmw: fastdds\n",
        encoding="utf-8",
    )
    ros2docker = project / "ros2docker.json"
    ros2docker.write_text("{}", encoding="utf-8")
    config_file = project / "rosotacom.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "ros2docker_config": str(ros2docker),
                "session_configs_dir": ["sessions"],
                "session_instances_dir": "session-instances",
            }
        ),
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    args = argparse.Namespace(
        rosotacom_config=str(config_file),
        ros2docker_config=None,
        session_configs_dir=None,
        scenario_configs_dir=None,
        session_instances_dir=None,
        deployment=None,
        profiles_file=None,
        artifacts_dir=None,
        rmw="cyclone",
    )

    context = _prepare_benchmark_session_config(args, "bench_1_1_capacity", run_dir)

    copied_config = Path(context["config_path"])
    copied = yaml.safe_load(copied_config.read_text(encoding="utf-8"))
    assert copied["shared"]["rmw"] == "cyclone"
    assert args.session_configs_dir[0] == str(run_dir / "session-configs")
    assert context["runtime_implementation"] == "rmw_cyclonedds_cpp"


def test_tee_stream_and_stdout_redirection(tmp_path: Path) -> None:
    import subprocess
    import sys

    from rosotacom.cli_benchmark import log_stdout_stderr_to_file

    log_file = tmp_path / "test_log.txt"

    with log_stdout_stderr_to_file(log_file):
        print("Hello from test stdout")
        print("Hello from test stderr", file=sys.stderr)

        # Test hooked subprocess.run
        res = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('sub stdout\\n'); sys.stderr.write('sub stderr\\n')",
            ],
            capture_output=False,
        )
        assert res.returncode == 0

    log_content = log_file.read_text(encoding="utf-8")
    assert "Hello from test stdout" in log_content
    assert "Hello from test stderr" in log_content
    assert "sub stdout" in log_content
    assert "sub stderr" in log_content
