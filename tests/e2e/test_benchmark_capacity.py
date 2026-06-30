from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_TIMEOUT_S = 900
SMOKE_NETWORK_NAME = "rosotacom-smoke"
CAPACITY_PROFILE = "cellular-4g-capacity-ci"
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("ROSOTACOM_RUN_E2E") != "1",
        reason="Docker-backed E2E tests require ROSOTACOM_RUN_E2E=1.",
    ),
]


def _cleanup_smoke_network() -> None:
    inspect = subprocess.run(
        ["docker", "network", "inspect", SMOKE_NETWORK_NAME, "--format", "{{json .Containers}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if inspect.returncode == 0 and inspect.stdout.strip():
        try:
            containers = json.loads(inspect.stdout)
        except json.JSONDecodeError:
            containers = {}
        for container_id in containers:
            subprocess.run(["docker", "rm", "-f", container_id], text=True, capture_output=True, check=False)
    subprocess.run(["docker", "network", "rm", SMOKE_NETWORK_NAME], text=True, capture_output=True, check=False)


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=PACKAGE_ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _cleanup_smoke_network()
        raise AssertionError(f"Command timed out after {timeout}s: {' '.join(command)}") from exc
    if result.returncode != 0:
        raise AssertionError(
            f"Command failed with {result.returncode}: {' '.join(command)}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


@pytest.fixture(scope="session")
def copied_example_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = tmp_path_factory.mktemp("rosotacom") / "examples"

    _run([sys.executable, "-m", "rosotacom", "examples", "create", str(project)], timeout=60)

    # Keep the smoke deterministic: these cases validate capacity against the
    # rate limit, not stochastic packet-loss sampling.
    profiles_yaml = project / "profiles.yaml"
    profiles_yaml.write_text(
        "profiles:\n"
        f"  {CAPACITY_PROFILE}:\n"
        "    uplink:   { rate: 1mbit,  delay: 180ms, jitter: 50ms,\n"
        "                distribution: normal, loss: 0%, loss_correlation: 0% }\n"
        "    downlink: { rate: 10mbit, delay: 100ms, jitter: 30ms, loss: 0% }\n",
        encoding="utf-8",
    )
    return project


def test_benchmark_capacity_good_case(copied_example_project: Path) -> None:
    """✅ Good case (1B@20Hz, --max-loss 30): PASS"""
    cmd = [
        sys.executable,
        "-m",
        "rosotacom",
        "benchmark",
        "capacity",
        "--rosotacom-config",
        str(copied_example_project / "rosotacom.yaml"),
        "--profile",
        CAPACITY_PROFILE,
        "--knob",
        "size",
        "--low",
        "1",
        "--high",
        "1",
        "--max-loss",
        "30",
        "--max-latency-ms",
        "1000",
        "--duration",
        "10",
        "--repeats",
        "1",
        "--rmw",
        "cyclone",
    ]
    result = _run(cmd, timeout=BENCHMARK_TIMEOUT_S)
    # The output should contain: Capacity: size=1
    assert "Capacity: size=1" in result.stdout


def test_benchmark_capacity_bad_case(copied_example_project: Path) -> None:
    """✅ Bad case (18KB@20Hz = 2.88Mbit/s on 1Mbit/s, --max-loss 30): FAIL"""
    cmd = [
        sys.executable,
        "-m",
        "rosotacom",
        "benchmark",
        "capacity",
        "--rosotacom-config",
        str(copied_example_project / "rosotacom.yaml"),
        "--profile",
        CAPACITY_PROFILE,
        "--knob",
        "size",
        "--low",
        "18000",
        "--high",
        "18000",
        "--max-loss",
        "30",
        "--max-latency-ms",
        "1000",
        "--duration",
        "10",
        "--repeats",
        "1",
        "--rmw",
        "cyclone",
    ]
    result = _run(cmd, timeout=BENCHMARK_TIMEOUT_S)
    # The output should contain: Capacity: size=None
    assert "Capacity: size=None" in result.stdout
