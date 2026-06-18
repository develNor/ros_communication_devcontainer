from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT_PATH = PACKAGE_ROOT / ".github" / "dependabot.yml"
IMAGE_SCAN_PATH = PACKAGE_ROOT / ".github" / "workflows" / "image-scan.yml"
MERGE_GATE_PATH = PACKAGE_ROOT / ".github" / "workflows" / "pr-merge-gate.yml"
FULL_E2E_PATH = PACKAGE_ROOT / ".github" / "workflows" / "nightly-e2e.yml"
RELEASE_PATH = PACKAGE_ROOT / ".github" / "workflows" / "release.yml"


def test_dependabot_groups_weekly_actions_and_python_updates() -> None:
    dependabot = DEPENDABOT_PATH.read_text(encoding="utf-8")

    assert 'package-ecosystem: "github-actions"' in dependabot
    assert 'package-ecosystem: "pip"' in dependabot
    assert dependabot.count('interval: "weekly"') == 2
    assert "github-actions:" in dependabot
    assert "python-dependencies:" in dependabot
    assert "auto-merge" not in dependabot.lower()


def test_image_scan_is_advisory_and_not_a_pr_check() -> None:
    image_scan = IMAGE_SCAN_PATH.read_text(encoding="utf-8")
    merge_gate = MERGE_GATE_PATH.read_text(encoding="utf-8")

    assert "name: image-scan" in image_scan
    assert '- cron: "23 3 * * *"' in image_scan
    assert "workflow_dispatch:" in image_scan
    assert "pull_request:" not in image_scan
    assert "IMAGE_TAG: rosotacom:scan-${{ github.sha }}" in image_scan
    assert "aquasecurity/trivy-action@v0.36.0" in image_scan
    assert 'exit-code: "0"' in image_scan
    assert "image-scan" not in merge_gate
    assert "trivy" not in merge_gate.lower()


def test_merge_gate_requires_non_docker_package_and_docker_smoke() -> None:
    merge_gate = MERGE_GATE_PATH.read_text(encoding="utf-8")

    assert "ci-success" in merge_gate
    assert "just test-nondocker-cov" in merge_gate
    assert "just package" in merge_gate
    assert "just test-e2e-smoke" in merge_gate


def test_full_e2e_workflow_is_manual_promotion_support_not_scheduled() -> None:
    full_e2e = FULL_E2E_PATH.read_text(encoding="utf-8")

    assert "name: Manual Full E2E" in full_e2e
    assert "workflow_dispatch:" in full_e2e
    assert "schedule:" not in full_e2e
    assert "ROSOTACOM_RUN_FULL_E2E=1" in full_e2e


def test_release_publishes_only_for_a_version_tag_via_repository_configuration() -> None:
    release = RELEASE_PATH.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in release
    assert "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')" in release
    assert "repository-url: ${{ vars.PYPI_PUBLISH_URL }}" in release
    assert "PYPI_PUBLISH_URL is not configured" in release
    assert "PYPI_PUBLISH_URL must be an HTTPS endpoint" in release
    assert "name: Release" in release
    assert "publish-testpypi" not in release
    assert "https://upload.pypi.org" not in release
    assert "https://test.pypi.org" not in release


def test_local_check_derivation_and_generated_rmw_matrix_drive_test_configs() -> None:
    """The old two-axis markers are gone: OTA membership is default, while local
    smoke eligibility is derived from per-peer domains unless a config opts out
    with local_check: false. See docs/testing.md."""
    from rosotacom.cli import local_check_sessions, ota_suite_sessions, session_local_checks

    examples_dir = PACKAGE_ROOT / "src" / "rosotacom" / "resources" / "examples" / "sessions"
    rmw_matrix_dir = PACKAGE_ROOT / "tests" / "sessions" / "rmw_matrix"

    session_files = [
        *examples_dir.glob("*/session-definition.yaml"),
        *rmw_matrix_dir.glob("*/session-definition.yaml"),
    ]
    for session_file in session_files:
        assert "test_tiers:" not in session_file.read_text(encoding="utf-8")

    assert set(local_check_sessions(examples_dir)) == {
        "1_heartbeat",
        "1_heartbeat_status",
        "2_native_chatter",
        "3_comp_occ_grid",
        "4_comp_occ_grid_zen",
        "5_sized_payload",
        "6_sized_payload_zen",
    }
    assert set(ota_suite_sessions(examples_dir)) == set(local_check_sessions(examples_dir))

    rmw_local = session_local_checks(rmw_matrix_dir)
    assert rmw_local["1_heartbeat_cyclone-ota-tuned"] is False
    assert set(local_check_sessions(rmw_matrix_dir)) == set(rmw_local) - {"1_heartbeat_cyclone-ota-tuned"}
    assert set(ota_suite_sessions(rmw_matrix_dir)) == set(rmw_local)

    subprocess.run(
        [sys.executable, str(PACKAGE_ROOT / "tests" / "sessions" / "generate_rmw_matrix.py"), "--check"],
        check=True,
    )

    e2e_smoke = (PACKAGE_ROOT / "tests" / "e2e" / "test_smoke.py").read_text(encoding="utf-8")
    assert "local_check_sessions()" in e2e_smoke
    assert "ROSOTACOM_RUN_FULL_E2E" in e2e_smoke
