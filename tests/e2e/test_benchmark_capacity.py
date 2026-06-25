from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("ROSOTACOM_RUN_E2E") != "1",
        reason="Docker-backed E2E tests require ROSOTACOM_RUN_E2E=1.",
    ),
]


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=PACKAGE_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
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

    # Overwrite profiles.yaml to include cellular-4g-degraded profile
    profiles_yaml = project / "profiles.yaml"
    profiles_yaml.write_text(
        "profiles:\n"
        "  cellular-4g-degraded:\n"
        "    uplink:   { rate: 1mbit,  delay: 180ms, jitter: 50ms,\n"
        "                distribution: normal, loss: 3%, loss_correlation: 25% }\n"
        "    downlink: { rate: 10mbit, delay: 100ms, jitter: 30ms, loss: 1% }\n",
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
        "cellular-4g-degraded",
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
    ]
    result = _run(cmd, timeout=300)
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
        "cellular-4g-degraded",
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
    ]
    result = _run(cmd, timeout=300)
    # The output should contain: Capacity: size=None
    assert "Capacity: size=None" in result.stdout
