from __future__ import annotations

import os
import subprocess
from importlib import resources
from pathlib import Path

import yaml

import rosotacom.cli as cli

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def test_packaged_resources_include_curated_examples_and_runtime_workspace() -> None:
    package = resources.files("rosotacom")

    expected = (
        "py.typed",
        "resources/ros2docker.json.example",
        "resources/examples/rosotacom.yaml",
        "resources/examples/.gitignore",
        "resources/examples/ros2docker.json",
        "resources/examples/profiles.yaml",
        "resources/examples/deployment.example.yaml",
        "resources/examples/sessions/1_heartbeat/session-definition.yaml",
        "resources/examples/sessions/1_heartbeat_status/session-definition.yaml",
        "resources/examples/sessions/14_remote_assist_anonymized/session-definition.yaml",
        "resources/examples/scenarios/2_native_chatter/scenario-definition.yaml",
        "resources/ws/session/creation/run_session.py",
        "resources/ws/session/creation/catmux_log_setup.sh",
        "resources/ws/session/creation/strip_ansi.py",
        "resources/ws/ota_configs/cyclonedds_tuned.xml.template",
        "resources/ws/ota_configs/fastdds_unicast.xml.template",
        "resources/ws/ros2src/com_msgs/msg/EchoHeartbeat.msg",
    )

    missing = [path for path in expected if not package.joinpath(path).is_file()]
    assert not missing

    ota_stamped = package.joinpath("resources/ws/ros2src/com_msgs/msg/OtaStamped.msg").read_text(encoding="utf-8")
    echo = package.joinpath("resources/ws/ros2src/com_msgs/msg/EchoHeartbeat.msg").read_text(encoding="utf-8")
    assert "source_stamp" not in ota_stamped
    assert "uint64 seq" in ota_stamped
    assert "builtin_interfaces/Time echo_t1" in echo
    assert "builtin_interfaces/Time echo_t2" in echo
    assert "builtin_interfaces/Time echo_t3" in echo


def test_packaged_example_profiles_parse() -> None:
    # The shipped illustrative profiles must actually load — a malformed example
    # (bad tc/netem combo, unknown key) would be a packaging regression.
    from rosotacom.network_profiles import load_profiles_file

    path = resources.files("rosotacom").joinpath("resources/examples/profiles.yaml")
    profiles = load_profiles_file(Path(str(path)))
    assert {"cellular-typical", "cellular-handover"} <= set(profiles)
    assert profiles["cellular-handover"].is_timeline


def test_cli_resource_constants_point_inside_package_resources() -> None:
    assert cli.RESOURCE_DIR == PACKAGE_ROOT / "src" / "rosotacom" / "resources"
    assert cli.WS_DIR == cli.RESOURCE_DIR / "ws"
    assert cli.DEFAULT_ROS2DOCKER_CONFIG == cli.RESOURCE_DIR / "ros2docker.json.example"
    assert cli.EXAMPLE_PROJECT_DIR == cli.RESOURCE_DIR / "examples"


def test_default_ros2docker_config_pins_supported_kilted_noble_image() -> None:
    default_build_args = cli.load_config(cli.DEFAULT_ROS2DOCKER_CONFIG)["build_args"]
    example_build_args = cli.load_config(cli.EXAMPLE_PROJECT_DIR / "ros2docker.json")["build_args"]

    assert default_build_args["BASE_IMAGE"] == "osrf/ros:kilted-desktop-full-noble"
    assert default_build_args["DIGEST"].startswith("@sha256:")
    assert default_build_args["BASE_IMAGE"] == example_build_args["BASE_IMAGE"]
    assert default_build_args["DIGEST"] == example_build_args["DIGEST"]


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


def test_base_plugin_catmux_commands_are_strings() -> None:
    plugin = yaml.safe_load(
        (cli.WS_DIR / "session" / "content" / "base" / "session_plugin_base.yaml").read_text(encoding="utf-8")
    )

    offenders: list[str] = []
    for window in plugin["windows"]:
        for split_index, split in enumerate(window.get("splits", [])):
            for command_index, command in enumerate(split.get("commands", [])):
                if not isinstance(command, str):
                    offenders.append(
                        f"{window.get('name', '<unnamed>')}.splits[{split_index}].commands[{command_index}]"
                    )

    assert not offenders


def test_domain_bridge_generates_ota_dds_config_before_launch() -> None:
    plugin = yaml.safe_load(
        (cli.WS_DIR / "session" / "content" / "base" / "session_plugin_base.yaml").read_text(encoding="utf-8")
    )
    com_window = next(window for window in plugin["windows"] if window["name"] == "COM")
    commands = com_window["splits"][0]["commands"]

    assert "/ws/ota_configs/get_ota_xml.py" in commands[0]
    assert commands[1].startswith("ros2 run domain_bridge domain_bridge")


def test_native_chatter_waiter_reads_topic_info_without_broken_pipe(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ros2 = fake_bin / "ros2"
    fake_ros2.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                'if [[ "$1 $2" == "topic info" ]]; then',
                '  for i in {1..100}; do echo "detail $i"; done',
                '  echo "Type: std_msgs/msg/String"',
                "  exit 0",
                "fi",
                'if [[ "$1 $2" == "topic echo" ]]; then',
                '  echo "echo started"',
                "  exit 0",
                "fi",
                "exit 2",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_ros2.chmod(0o755)
    script = cli.EXAMPLE_PROJECT_DIR / "scripts" / "2_native_chatter" / "machine_a" / "wait_for_echo_topic.sh"

    result = subprocess.run(
        [str(script)],
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert result.stdout.strip() == "echo started"
    assert "BrokenPipeError" not in result.stderr
