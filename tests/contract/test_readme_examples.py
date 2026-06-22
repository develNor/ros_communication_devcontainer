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
    assert runtime.session_configs_dir == (cli.EXAMPLE_PROJECT_DIR / "sessions",)
    assert runtime.scenario_configs_dir == (cli.EXAMPLE_PROJECT_DIR / "scenarios",)
    assert runtime.session_instances_dir == PACKAGE_ROOT / "session-instances"
    assert runtime.deployment is None


def test_packaged_example_setup_paths_are_relative_to_example_root() -> None:
    setup = yaml.safe_load((cli.EXAMPLE_PROJECT_DIR / "rosotacom.yaml").read_text(encoding="utf-8"))

    assert setup == {
        "ros2docker_config": "ros2docker.json",
        "session_configs_dir": ["sessions"],
        "scenario_configs_dir": ["scenarios"],
        "session_instances_dir": "session-instances",
    }


def test_packaged_example_project_contains_documented_heartbeat_session() -> None:
    readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")

    assert "`1_heartbeat`" in readme
    assert "rosotacom scenario start 2_native_chatter" in readme
    assert "rosotacom smoke 2_native_chatter --interactive" in readme
    assert "rosotacom ota-smoke 2_native_chatter \\" in readme
    assert "--peer-ssh b=robot-b" in readme
    assert (cli.EXAMPLE_PROJECT_DIR / "sessions" / "1_heartbeat" / "session-definition.yaml").is_file()
    assert (cli.EXAMPLE_PROJECT_DIR / "scenarios" / "2_native_chatter" / "scenario-definition.yaml").is_file()
    assert (cli.EXAMPLE_PROJECT_DIR / "scripts" / "1_heartbeat" / "run_machine_a.sh").is_file()


def test_packaged_native_chatter_scenario_and_application_configs_load() -> None:
    runtime = cli._load_runtime_config(
        argparse.Namespace(rosotacom_config=str(cli.EXAMPLE_PROJECT_DIR / "rosotacom.yaml"))
    )
    resolved = cli._resolve_scenario("2_native_chatter", runtime)

    definition = cli._load_scenario_definition(resolved)

    assert definition.session == "2_native_chatter"
    assert set(definition.applications) == {"a", "b"}


def test_public_docs_do_not_contain_private_host_inventory_details() -> None:
    public_text = "\n".join(
        [
            (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8"),
            (PACKAGE_ROOT / "docs" / "testing.md").read_text(encoding="utf-8"),
        ]
    )

    for private_token in ("tks-lamborghini", "tks-majestic", "majestic_go914", "10.254.0."):
        assert private_token not in public_text
