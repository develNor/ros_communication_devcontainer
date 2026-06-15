from __future__ import annotations

import subprocess
from importlib import resources
from pathlib import Path

import rosotacom.cli as cli

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def test_packaged_resources_include_cli_marker_examples_and_runtime_workspace() -> None:
    package = resources.files("rosotacom")

    expected = (
        "py.typed",
        "resources/ros2docker.json.example",
        "resources/examples/rosotacom.yaml",
        "resources/examples/.gitignore",
        "resources/examples/ros2docker.json",
        "resources/examples/data_dict.json",
        "resources/examples/sessions/1_heartbeat_fastdds/session-definition.yaml",
        "resources/ws/session/creation/run_session.py",
        "resources/ws/session/creation/catmux_log_setup.sh",
        "resources/ws/session/creation/strip_ansi.py",
        "resources/ws/session/content/address_resolution.py",
        "resources/ws/ota_configs/cyclonedds_tuned.xml.template",
        "resources/ws/ros2src/com_msgs/msg/Heartbeat.msg",
    )

    missing = [path for path in expected if not package.joinpath(path).is_file()]
    assert not missing


def test_cli_resource_constants_point_inside_package_resources() -> None:
    assert cli.RESOURCE_DIR == PACKAGE_ROOT / "src" / "rosotacom" / "resources"
    assert cli.WS_DIR == cli.RESOURCE_DIR / "ws"
    assert cli.DEFAULT_ROS2DOCKER_CONFIG == cli.RESOURCE_DIR / "ros2docker.json.example"
    assert cli.EXAMPLE_PROJECT_DIR == cli.RESOURCE_DIR / "examples"


def test_shell_entrypoints_pass_syntax_check() -> None:
    scripts = sorted(cli.EXAMPLE_PROJECT_DIR.glob("scripts/**/*.sh"))
    assert scripts
    for script in scripts:
        subprocess.run(["bash", "-n", str(script)], check=True)

    subprocess.run(["bash", "-n", str(cli.WS_DIR / "session" / "creation" / "catmux_log_setup.sh")], check=True)
    subprocess.run(
        ["python3", "-m", "py_compile", str(cli.WS_DIR / "session" / "creation" / "strip_ansi.py")],
        check=True,
    )
