from __future__ import annotations

import os
import re
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

HEARTBEAT_SMOKE_SESSIONS = [
    pytest.param("1_heartbeat_cyclone-ota", id="cyclone-ota"),
    pytest.param("1_heartbeat_zen-endpoints", id="zen-endpoints"),
    pytest.param("1_heartbeat_cyclone-local_zenoh-ros2dds-ota", id="cyclone-local-zenoh-ros2dds-ota"),
    pytest.param("1_heartbeat_fastdds", id="fastdds"),
    pytest.param(
        "1_heartbeat_fastdds-local_cyclone-ota",
        marks=pytest.mark.xfail(
            reason="Strict heartbeat smoke currently fails at the a->b inbound bridge topic.",
            strict=True,
        ),
        id="fastdds-local-cyclone-ota",
    ),
    pytest.param(
        "1_heartbeat_cyclone-local_fastdds-ota",
        marks=pytest.mark.xfail(
            reason="Strict heartbeat smoke currently fails at the a->b inbound bridge topic.",
            strict=True,
        ),
        id="cyclone-local-fastdds-ota",
    ),
]

EXPECTED_HEARTBEAT_CHECKS = (
    "OK: generated plugin.yaml files use literal CLI addresses",
    "OK: a->b inbound bridge heartbeat (/com/in/a/heartbeat_a)",
    "OK: a->b final heartbeat (/heartbeat_a)",
    "OK: b->a inbound bridge heartbeat (/com/in/b/heartbeat_b)",
    "OK: b->a final heartbeat (/heartbeat_b)",
)


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
    return project


@pytest.mark.parametrize("session_name", HEARTBEAT_SMOKE_SESSIONS)
def test_local_heartbeat_smoke_matrix_from_copied_example_project(
    copied_example_project: Path,
    session_name: str,
) -> None:
    result = _run(
        [
            sys.executable,
            "-m",
            "rosotacom",
            "smoke",
            session_name,
            "--rosotacom-config",
            str(copied_example_project / "rosotacom.yaml"),
            "--local-ip",
            "127.0.0.1",
        ],
        timeout=900,
    )

    for expected in EXPECTED_HEARTBEAT_CHECKS:
        assert expected in result.stdout

    artifact_matches = re.findall(r"Smoke artifacts: (.+)", result.stdout)
    assert artifact_matches
    artifact_dir = Path(artifact_matches[-1].strip())
    assert (artifact_dir / "config" / "a" / "plugin.yaml").is_file()
    assert (artifact_dir / "config" / "b" / "plugin.yaml").is_file()
    assert list((artifact_dir / "logs").glob("*/catmux/*/*.log"))
