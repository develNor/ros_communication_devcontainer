"""Slow-lane E2E for ``rosotacom benchmark ab`` (#22).

Two configs that differ only in ``throttle_hz`` on the synthetic load topic, run
under a tight rate-limited profile, must produce the expected directional
verdict: the light-throttle *baseline* offers little load and stays clean, while
the heavy-throttle *candidate* offers the full 20 Hz × 18 KB (≈2.9 Mbit/s) into a
1 Mbit/s uplink, congests, and therefore **regresses** on completeness/loss.
That the A/B driver detects this direction — on a real graph, real shaping — is
the end-to-end proof the pure-logic unit tests cannot give.

Docker-backed, so gated behind ``ROSOTACOM_RUN_E2E=1`` like the other e2e lanes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_TIMEOUT_S = 900
SMOKE_NETWORK_NAME = "rosotacom-smoke"
AB_PROFILE = "ab-congestion-ci"

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


def _ab_config(throttle_hz: int) -> dict[str, object]:
    """A bench_1_1-style single-stream config; only ``throttle_hz`` varies."""
    return {
        "peers": {"a": {}, "b": {}},
        "peer_settings": {"a": {"domain_id": 50}, "b": {"domain_id": 51}},
        "shared": {
            "use_status_overview": True,
            "ota_domain_id": 52,
            "rmw": "cyclone",
            "qos": {
                "defaults": {"depth": 1},
                "for_role": {"ota_sub": {"reliability": "best_effort"}, "ota_pub": {"reliability": "best_effort"}},
            },
        },
        "topics": {
            "a_to_b": [
                {
                    "topic": "/bench_capacity",
                    "type": "com_msgs/msg/SizedPayload",
                    "processing": {"use_ota_wrapper": True, "throttle_hz": throttle_hz},
                }
            ]
        },
    }


@pytest.fixture(scope="session")
def ab_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = tmp_path_factory.mktemp("rosotacom") / "examples"
    _run([sys.executable, "-m", "rosotacom", "examples", "create", str(project)], timeout=60)

    # Tight uplink: 20 Hz × 18 KB ≈ 2.9 Mbit/s does not fit 1 Mbit/s, but the
    # throttled 4 Hz ≈ 0.58 Mbit/s does — so the difference is pure congestion.
    (project / "profiles.yaml").write_text(
        f"profiles:\n  {AB_PROFILE}:\n    uplink:   {{ rate: 1mbit }}\n    downlink: {{ rate: 10mbit }}\n",
        encoding="utf-8",
    )
    baseline = project / "ab-configs" / "baseline"
    candidate = project / "ab-configs" / "unthrottled"
    baseline.mkdir(parents=True)
    candidate.mkdir(parents=True)
    (baseline / "session-definition.yaml").write_text(yaml.safe_dump(_ab_config(4)), encoding="utf-8")
    (candidate / "session-definition.yaml").write_text(yaml.safe_dump(_ab_config(20)), encoding="utf-8")
    return project


def _result_path(stdout: str) -> Path:
    prefix = "Benchmark result saved to "
    paths = [Path(line.removeprefix(prefix)) for line in stdout.splitlines() if line.startswith(prefix)]
    if not paths:
        raise AssertionError(f"benchmark ab did not report a result.json path.\nSTDOUT:\n{stdout}")
    return paths[-1]


def test_benchmark_ab_directional_verdict(ab_project: Path) -> None:
    """Heavier throttle (more offered load) regresses under congestion."""
    result = _run(
        [
            sys.executable,
            "-m",
            "rosotacom",
            "benchmark",
            "ab",
            "--rosotacom-config",
            str(ab_project / "rosotacom.yaml"),
            "--profile",
            AB_PROFILE,
            "--baseline",
            str(ab_project / "ab-configs" / "baseline"),
            "--candidate",
            f"unthrottled={ab_project / 'ab-configs' / 'unthrottled'}",
            "--size",
            "18000",
            "--rate-hz",
            "20",
            "--duration",
            "15",
            "--repeats",
            "1",
            "--rmw",
            "cyclone",
        ],
        timeout=BENCHMARK_TIMEOUT_S,
    )

    doc = json.loads(_result_path(result.stdout).read_text(encoding="utf-8"))
    assert doc["genre"] == "ab"
    # The candidate offered ~5× the baseline load into the same 1 Mbit/s pipe, so
    # the driver must call it a regression on the bottleneck-dominated metrics.
    assert doc["verdict"]["passed"] is False
    regressed = doc["verdict"]["regressed"].get("unthrottled", [])
    regressed_metrics = {metric for _topic, metric in regressed}
    assert regressed_metrics & {"loss_pct", "completeness_pct"}, doc["verdict"]
    # Exactly one candidate, and its per-run measurements were recorded.
    assert len(doc["result"]["candidates"]) == 1
    assert len(doc["measurements"]["runs"]) == 2  # baseline + candidate, 1 repeat each
