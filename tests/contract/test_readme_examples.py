from __future__ import annotations

import argparse
from pathlib import Path

import yaml

import rosotacom.cli as cli

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def test_readme_documents_current_entrypoints_and_workflow_files() -> None:
    readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")

    assert "rosotacom --version" in readme
    assert "python -m rosotacom --version" in readme
    assert "CONTRIBUTING.md" in readme
    assert "docs/ci.md" in readme


def test_source_checkout_rosotacom_yaml_loads() -> None:
    runtime = cli._load_runtime_config(argparse.Namespace(rosotacom_config=str(PACKAGE_ROOT / "rosotacom.yaml")))

    assert runtime.ros2docker_config == cli.EXAMPLE_PROJECT_DIR / "ros2docker.json"
    assert runtime.session_configs_dir == cli.EXAMPLE_PROJECT_DIR / "sessions"
    assert runtime.data_dict == cli.EXAMPLE_PROJECT_DIR / "data_dict.json"


def test_packaged_example_setup_paths_are_relative_to_example_root() -> None:
    setup = yaml.safe_load((cli.EXAMPLE_PROJECT_DIR / "rosotacom.yaml").read_text(encoding="utf-8"))

    assert setup == {
        "ros2docker_config": "ros2docker.json",
        "session_configs_dir": "sessions",
        "data_dict": "data_dict.json",
    }


def test_packaged_example_project_contains_documented_heartbeat_session() -> None:
    readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")

    assert "`1_heartbeat_fastdds`" in readme
    assert (cli.EXAMPLE_PROJECT_DIR / "sessions" / "1_heartbeat_fastdds" / "session-definition.yaml").is_file()
    assert (cli.EXAMPLE_PROJECT_DIR / "scripts" / "1_heartbeat" / "run_machine_a.sh").is_file()
