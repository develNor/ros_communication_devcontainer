from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT_PATH = PACKAGE_ROOT / ".github" / "dependabot.yml"
IMAGE_SCAN_PATH = PACKAGE_ROOT / ".github" / "workflows" / "image-scan.yml"
MERGE_GATE_PATH = PACKAGE_ROOT / ".github" / "workflows" / "pr-merge-gate.yml"


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


def test_session_test_tier_markers_drive_both_test_matrices() -> None:
    """Every session declares test_tiers; the single-machine smoke matrix and the
    multi-machine set are derived from those markers (the single source of truth),
    so neither tier can silently drift. See docs/testing.md."""
    from rosotacom.cli import session_test_markers, sessions_in_tier

    markers = session_test_markers()  # raises if any session lacks valid markers
    assert markers, "no example sessions found"

    # Anti-drift guard: the heartbeat-RMW matrix is exactly the single-machine
    # smoke set. Adding/removing an RMW example must update its marker accordingly.
    assert set(sessions_in_tier("single_machine", {"ok"})) == {
        "1_heartbeat_cyclone-ota",
        "1_heartbeat_fastdds",
        "1_heartbeat_zen-endpoints",
        "1_heartbeat_fastdds-local_cyclone-ota",
        "1_heartbeat_cyclone-local_fastdds-ota",
        "1_heartbeat_cyclone-local_zenoh-ros2dds-ota",
    }

    # cyclone-ota-tuned hides local topics on a shared domain (no per-peer domain
    # split), so it is only provable multi-machine.
    assert markers["1_heartbeat_cyclone-ota-tuned"] == {"single_machine": "na", "multi_machine": "required"}
    assert "1_heartbeat_cyclone-ota-tuned" in sessions_in_tier("multi_machine", {"ok", "required"})

    # The smoke test must derive its matrix from the markers, not hardcode it.
    e2e_smoke = (PACKAGE_ROOT / "tests" / "e2e" / "test_smoke.py").read_text(encoding="utf-8")
    assert 'sessions_in_tier("single_machine", {"ok"})' in e2e_smoke
