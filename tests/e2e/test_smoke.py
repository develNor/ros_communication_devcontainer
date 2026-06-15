from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.e2e


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


def test_local_heartbeat_smoke_from_copied_example_project(tmp_path: Path) -> None:
    project = tmp_path / "rosotacom_examples"

    _run([sys.executable, "-m", "rosotacom", "examples", "create", str(project)], timeout=60)
    result = _run(
        [
            sys.executable,
            "-m",
            "rosotacom",
            "smoke",
            "--rosotacom-config",
            str(project / "rosotacom.yaml"),
            "--local-ip",
            "127.0.0.1",
        ],
        timeout=900,
    )

    assert "OK: generated plugin.yaml files use literal CLI addresses" in result.stdout
