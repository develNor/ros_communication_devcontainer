from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT_PATH = PACKAGE_ROOT / ".github" / "dependabot.yml"
IMAGE_SCAN_PATH = PACKAGE_ROOT / ".github" / "workflows" / "image-scan.yml"
MERGE_GATE_PATH = PACKAGE_ROOT / ".github" / "workflows" / "pr-merge-gate.yml"
FULL_E2E_PATH = PACKAGE_ROOT / ".github" / "workflows" / "nightly-e2e.yml"
RELEASE_PATH = PACKAGE_ROOT / ".github" / "workflows" / "release.yml"


def _rmw_smoke_id(session_name: str) -> str:
    return session_name.removeprefix("1_heartbeat_").replace("_", "-")


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
    jobs = yaml.safe_load(merge_gate)["jobs"]

    assert "ci-success" in merge_gate
    assert "just test-nondocker-cov" in merge_gate
    assert "just package" in merge_gate

    # The gate runs the e2e suite as one matrix over named slices rather than
    # six copy-pasted jobs, so the slice names live in the matrix and the
    # recipe name in the `run:` line is `test-e2e-slice`. Which slices those
    # are, and that they add up to the whole suite, is
    # tests/contract/test_workflow_contracts.py's job.
    assert "just test-e2e-slice" in merge_gate
    # Named individually rather than compared to E2E_SLICES, so that dropping a
    # slice from the manifest and the matrix together still fails here. Which
    # slices exist is a balance decision (#226, #235); that the gate runs the
    # Docker smoke suite at all is what this file is about.
    assert set(jobs["e2e"]["strategy"]["matrix"]["slice"]) >= {
        "heartbeat",
        "chatter",
        "occupancy-grid",
        "sized-payload",
        "remote-assist",
        "remote-assist-streams",
        "runtime-tools",
        "media",
        "concurrency",
        "benchmark-ab",
        "benchmark-replay",
        "benchmark-capacity",
        "benchmark-capacity-cases",
    }


def test_merge_gate_requires_new_ci_jobs_and_no_masking() -> None:
    content = MERGE_GATE_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(content)
    jobs = data["jobs"]

    ci_success = jobs["ci-success"]
    needs = ci_success["needs"]
    expected_jobs = {
        "workflow-lint",
        "dependency-review",
        "build-lint",
        "merge-lightweight",
        "package",
        "quick-gate",
        "image",
        "e2e",
    }
    assert expected_jobs.issubset(set(needs))

    # Both gates the e2e slices wait on have to be green before any slice runs,
    # and the `if` has to say so job by job: `needs` alone does not stop a
    # dependency's failure from reaching a job that runs with `always()`.
    assert set(jobs["e2e"]["needs"]) == {"quick-gate", "image"}
    assert jobs["e2e"]["if"] == (
        "always() && !cancelled() && needs.quick-gate.result == 'success' && needs.image.result == 'success'"
    )

    # Every needed job's result must be turned into an exit code by hand,
    # because ci-success runs with `if: always()`. This used to list the e2e
    # jobs one by one and missed `e2e-media` entirely.
    check_step = next(step for step in ci_success["steps"] if step.get("name") == "Check required jobs")
    run_script = check_step["run"]
    for job in needs:
        if job == "dependency-review":
            assert f"needs.{job}.result" in run_script
        else:
            assert f"needs.{job}.result" + ' }}" != "success' in run_script

    # 2. required jobs do not use continue-on-error: true or || true masking
    for job_name in expected_jobs:
        job = jobs[job_name]
        assert job.get("continue-on-error", False) is False
        for step in job.get("steps", []):
            assert step.get("continue-on-error", False) is False
            run_cmd = step.get("run", "")
            assert "|| true" not in run_cmd
            assert "|| exit 0" not in run_cmd


def test_dependency_review_configuration() -> None:
    content = MERGE_GATE_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(content)

    dep_review_job = data["jobs"]["dependency-review"]
    step = next(step for step in dep_review_job["steps"] if "dependency-review-action" in step.get("uses", ""))

    with_opts = step["with"]
    assert with_opts["fail-on-severity"] == "high"
    assert "development" in with_opts["fail-on-scopes"]
    assert "runtime" in with_opts["fail-on-scopes"]

    # Check license policy denying strong-copyleft GPL/AGPL/LGPL
    deny_licenses = with_opts["deny-licenses"]
    for lic in ["GPL-1.0-only", "GPL-2.0-only", "GPL-3.0-only", "AGPL-3.0-only", "LGPL-2.1-only", "LGPL-3.0-only"]:
        assert lic in deny_licenses


def test_actionlint_pins_are_in_sync() -> None:
    pre_commit_path = PACKAGE_ROOT / ".pre-commit-config.yaml"
    pre_commit_data = yaml.safe_load(pre_commit_path.read_text(encoding="utf-8"))

    actionlint_repo = next(repo for repo in pre_commit_data["repos"] if "github.com/rhysd/actionlint" in repo["repo"])
    pre_commit_version = actionlint_repo["rev"].lstrip("v")

    merge_gate_content = MERGE_GATE_PATH.read_text(encoding="utf-8")
    merge_gate_data = yaml.safe_load(merge_gate_content)
    workflow_lint_job = merge_gate_data["jobs"]["workflow-lint"]
    install_step = next(step for step in workflow_lint_job["steps"] if step.get("name") == "Install actionlint")
    assert f'ACTIONLINT_VERSION="{pre_commit_version}"' in install_step["run"]


def test_codeowners_covers_high_risk_paths() -> None:
    codeowners_path = PACKAGE_ROOT / ".github" / "CODEOWNERS"
    assert codeowners_path.is_file()

    content = codeowners_path.read_text(encoding="utf-8")
    lines = [line.strip() for line in content.splitlines() if line.strip() and not line.startswith("#")]

    expected_paths = {
        "/.github/",
        "/pyproject.toml",
        "/justfile",
        "/docs/release.md",
        "/docs/release-notes/",
        "/src/rosotacom/resources/",
    }

    covered_paths = set()
    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            path = parts[0]
            owner = parts[1]
            assert owner == "@develNor"
            covered_paths.add(path)

    assert expected_paths.issubset(covered_paths)


def test_full_e2e_workflow_runs_nightly_and_supports_manual_dispatch() -> None:
    full_e2e = FULL_E2E_PATH.read_text(encoding="utf-8")

    assert "name: Nightly E2E" in full_e2e
    assert '- cron: "37 2 * * *"' in full_e2e
    assert "workflow_dispatch:" in full_e2e
    assert "name: smoke-e2e" in full_e2e
    assert "rmw-matrix:" in full_e2e
    assert "just test-e2e-rmw" in full_e2e


def test_nightly_rmw_matrix_matches_local_checkable_smoke_params() -> None:
    from rosotacom.cli import local_check_sessions, session_local_checks

    rmw_matrix_dir = PACKAGE_ROOT / "tests" / "sessions" / "rmw_matrix"
    workflow = yaml.safe_load(FULL_E2E_PATH.read_text(encoding="utf-8"))

    actual_sessions = workflow["jobs"]["rmw-matrix"]["strategy"]["matrix"]["session"]
    expected_sessions = [_rmw_smoke_id(name) for name in local_check_sessions(rmw_matrix_dir)]

    assert actual_sessions == expected_sessions
    assert session_local_checks(rmw_matrix_dir)["1_heartbeat_cyclone-ota-tuned"] is False
    assert "cyclone-ota-tuned" not in actual_sessions


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
        "7_latched_static",
        "8_drop",
        "9_throttle",
        "10_restamp",
        "11_trickle",
        "12_content_integrity",
        "13_link_latency",
        "14_remote_assist_anonymized",
        "15_remote_assist_anonymized_costmap",
        "16_remote_assist_anonymized_camera",
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


def test_generated_rmw_matrix_omits_mixed_dds_split_domain_cases() -> None:
    rmw_matrix_dir = PACKAGE_ROOT / "tests" / "sessions" / "rmw_matrix"

    def rmw_name(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and len(value) == 1:
            return next(iter(value))
        raise AssertionError(f"unexpected rmw side spec: {value!r}")

    unsupported_cases: list[str] = []
    for session_file in sorted(rmw_matrix_dir.glob("*/session-definition.yaml")):
        session = yaml.safe_load(session_file.read_text(encoding="utf-8"))
        rmw = session["shared"]["rmw"]
        local = rmw_name(rmw["local"])
        ota = rmw_name(rmw["ota"])
        if {local, ota} == {"cyclone", "fastdds"}:
            unsupported_cases.append(session_file.parent.name)

    assert not unsupported_cases
