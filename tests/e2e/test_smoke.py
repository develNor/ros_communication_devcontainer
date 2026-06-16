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
    pytest.param(
        "1_heartbeat_cyclone-local_zenoh-ros2dds-ota",
        marks=pytest.mark.xfail(
            reason=(
                "zenoh_ros2dds OTA double-bridges the heartbeat (~2x, ~21 Hz) over OTA domain 48, "
                "exceeding the rate bound. Network-namespace isolation did not resolve it; "
                "tracked as a follow-up to fix the zenoh-bridge-ros2dds / domain_bridge scoping."
            ),
            strict=True,
        ),
        id="cyclone-local-zenoh-ros2dds-ota",
    ),
    pytest.param("1_heartbeat_fastdds", id="fastdds"),
    pytest.param("1_heartbeat_fastdds-local_cyclone-ota", id="fastdds-local-cyclone-ota"),
    pytest.param("1_heartbeat_cyclone-local_fastdds-ota", id="cyclone-local-fastdds-ota"),
]

EXPECTED_HEARTBEAT_CHECKS = (
    "OK: generated plugin.yaml files use literal CLI addresses",
    "OK: a->b inbound bridge heartbeat (/com/in/a/heartbeat_a)",
    "OK: a->b final heartbeat (/heartbeat_a)",
    "OK: b->a inbound bridge heartbeat (/com/in/b/heartbeat_b)",
    "OK: b->a final heartbeat (/heartbeat_b)",
)

# Heartbeat publishers emit at 10 Hz; received rate should stay close to that.
EXPECTED_HEARTBEAT_HZ = 10.0
HEARTBEAT_HZ_MIN = 5.0
HEARTBEAT_HZ_MAX = 20.0
# End-to-end heartbeat latency must stay well below one second.
MAX_HEARTBEAT_DELAY_S = 1.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ERROR_PATTERNS = (
    re.compile(r"\[ERROR\]"),
    re.compile(r"\[FATAL\]"),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"terminate called"),
    re.compile(r"Segmentation fault"),
    re.compile(r"core dumped"),
)
_METRIC_RE = re.compile(
    r"SMOKE_METRIC topic=(?P<topic>\S+) container=(?P<container>\S+) "
    r"hz=(?P<hz>\S+) delay_s=(?P<delay>\S+) label=(?P<label>.+)"
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


# Force smoke artifacts into the repo workspace instead of the pytest tmp dir so
# CI's "Upload smoke session artifacts" step (path: session-instances/) actually
# captures catmux/domain-bridge logs when a smoke check fails.
SESSION_INSTANCES_DIR = PACKAGE_ROOT / "session-instances"


def _smoke_command(project: Path, session_name: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "rosotacom",
        "smoke",
        session_name,
        "--rosotacom-config",
        str(project / "rosotacom.yaml"),
        "--session-instances-dir",
        str(SESSION_INSTANCES_DIR),
        "--local-ip",
        "127.0.0.1",
    ]


def _artifact_dir(stdout: str) -> Path:
    matches = re.findall(r"Smoke artifacts: (.+)", stdout)
    assert matches, f"no 'Smoke artifacts:' line in smoke output:\n{stdout}"
    return Path(matches[-1].strip())


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _collect_log_files(artifact_dir: Path) -> list[Path]:
    logs_dir = artifact_dir / "logs"
    files = list(logs_dir.glob("*/catmux/*/*.log"))
    files += list(logs_dir.glob("*/docker.log"))
    return sorted(files)


def _scan_logs_for_errors(artifact_dir: Path) -> list[str]:
    hits: list[str] = []
    for path in _collect_log_files(artifact_dir):
        text = _strip_ansi(path.read_text(encoding="utf-8", errors="replace"))
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(pattern.search(line) for pattern in _ERROR_PATTERNS):
                rel = path.relative_to(artifact_dir)
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    return hits


def _parse_metrics(stdout: str) -> list[dict[str, object]]:
    metrics: list[dict[str, object]] = []
    for line in stdout.splitlines():
        match = _METRIC_RE.search(line)
        if not match:
            continue
        metrics.append(
            {
                "topic": match["topic"],
                "container": match["container"],
                "hz": None if match["hz"] == "nan" else float(match["hz"]),
                "delay_s": None if match["delay"] == "nan" else float(match["delay"]),
                "label": match["label"].strip().strip("'\""),
            }
        )
    return metrics


def _assert_no_ros_or_catmux_errors(session_name: str, artifact_dir: Path) -> None:
    log_files = _collect_log_files(artifact_dir)
    assert log_files, f"no catmux/docker logs found under {artifact_dir}"

    errors = _scan_logs_for_errors(artifact_dir)
    assert not errors, f"ROS/catmux logs for {session_name} contain errors:\n" + "\n".join(errors)


def _assert_heartbeat_rate_and_latency_within_bounds(session_name: str, stdout: str) -> None:
    metrics = _parse_metrics(stdout)
    final_heartbeat_metrics = [m for m in metrics if "final heartbeat" in str(m["label"])]
    assert final_heartbeat_metrics, f"no final heartbeat metrics in smoke output for {session_name}:\n{stdout}"

    hz_problems: list[str] = []
    delay_problems: list[str] = []
    for metric in final_heartbeat_metrics:
        hz = metric["hz"]
        delay_s = metric["delay_s"]
        if not isinstance(hz, float) or not (HEARTBEAT_HZ_MIN <= hz <= HEARTBEAT_HZ_MAX):
            hz_problems.append(f"{metric['label']} ({metric['topic']}): hz={hz}")
        if not isinstance(delay_s, float) or delay_s >= MAX_HEARTBEAT_DELAY_S:
            delay_problems.append(f"{metric['label']} ({metric['topic']}): delay_s={delay_s}")

    assert not hz_problems, (
        f"{session_name} heartbeat rate outside [{HEARTBEAT_HZ_MIN}, {HEARTBEAT_HZ_MAX}] Hz "
        f"(expected ~{EXPECTED_HEARTBEAT_HZ} Hz):\n" + "\n".join(hz_problems)
    )
    assert not delay_problems, f"{session_name} heartbeat latency >= {MAX_HEARTBEAT_DELAY_S}s:\n" + "\n".join(
        delay_problems
    )


@pytest.mark.parametrize("session_name", HEARTBEAT_SMOKE_SESSIONS)
def test_local_heartbeat_smoke_matrix_from_copied_example_project(
    copied_example_project: Path,
    session_name: str,
) -> None:
    result = _run(_smoke_command(copied_example_project, session_name), timeout=900)

    for expected in EXPECTED_HEARTBEAT_CHECKS:
        assert expected in result.stdout

    artifact_dir = _artifact_dir(result.stdout)
    assert (artifact_dir / "config" / "a" / "plugin.yaml").is_file()
    assert (artifact_dir / "config" / "b" / "plugin.yaml").is_file()
    assert list((artifact_dir / "logs").glob("*/catmux/*/*.log"))

    _assert_no_ros_or_catmux_errors(session_name, artifact_dir)
    _assert_heartbeat_rate_and_latency_within_bounds(session_name, result.stdout)
