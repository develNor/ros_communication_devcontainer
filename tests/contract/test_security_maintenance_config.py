from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT_PATH = PACKAGE_ROOT / ".github" / "dependabot.yml"
IMAGE_SCAN_PATH = PACKAGE_ROOT / ".github" / "workflows" / "image-scan.yml"
MERGE_GATE_PATH = PACKAGE_ROOT / ".github" / "workflows" / "pr-merge-gate.yml"
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


def test_session_test_tier_markers_drive_both_test_matrices() -> None:
    """Every session declares test_tiers; the single-machine smoke matrix and the
    multi-machine set are derived from those markers (the single source of truth),
    so neither tier can silently drift. See docs/testing.md."""
    from rosotacom.cli import session_test_markers, sessions_in_tier

    markers = session_test_markers()  # raises if any session lacks valid markers
    assert markers, "no example sessions found"

    # Anti-drift guard: these examples are exactly the single-machine smoke set.
    # Adding/removing an example must update its marker accordingly.
    assert set(sessions_in_tier("single_machine", {"ok"})) == {
        "1_heartbeat_cyclone-ota",
        "1_heartbeat_fastdds",
        "1_heartbeat_zen-endpoints",
        "1_heartbeat_fastdds-local_cyclone-ota",
        "1_heartbeat_cyclone-local_fastdds-ota",
        "1_heartbeat_cyclone-local_zenoh-ros2dds-ota",
        "2_native_chatter",
        "3_comp_occ_grid",
        "4_comp_occ_grid_zen",
        "5_sized_payload",
        "6_sized_payload_zen",
    }

    # cyclone-ota-tuned hides local topics on a shared domain (no per-peer domain
    # split), so it is only provable multi-machine.
    assert markers["1_heartbeat_cyclone-ota-tuned"] == {"single_machine": "na", "multi_machine": "required"}
    assert set(sessions_in_tier("multi_machine", {"ok", "required"})) == {
        "1_heartbeat_cyclone-local_fastdds-ota",
        "1_heartbeat_cyclone-local_zenoh-ros2dds-ota",
        "1_heartbeat_cyclone-ota",
        "1_heartbeat_cyclone-ota-tuned",
        "1_heartbeat_fastdds",
        "1_heartbeat_fastdds-local_cyclone-ota",
        "1_heartbeat_zen-endpoints",
        "2_native_chatter",
        "3_comp_occ_grid",
        "4_comp_occ_grid_zen",
        "5_sized_payload",
        "6_sized_payload_zen",
    }

    # The smoke test must derive its matrix from the markers, not hardcode it.
    e2e_smoke = (PACKAGE_ROOT / "tests" / "e2e" / "test_smoke.py").read_text(encoding="utf-8")
    assert 'sessions_in_tier("single_machine", {"ok"})' in e2e_smoke
