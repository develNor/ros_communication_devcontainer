from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

import rosotacom.cli as rosotacom


@pytest.fixture(autouse=True)
def clear_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "ROSOTACOM_CONFIG",
        "ROSOTACOM_ROS2DOCKER_CONFIG",
        "ROSOTACOM_SESSION_CONFIGS_DIR",
        "ROSOTACOM_SESSION_INSTANCES_DIR",
        "ROSOTACOM_DATA_DICT",
    ):
        monkeypatch.delenv(key, raising=False)
    # Keep the global user config and the built-in example's tmpfs instances dir
    # off the real $HOME / runtime dir so tests stay hermetic.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / ".run"))


def test_cwd_rosotacom_yaml_is_auto_discovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "ros2docker.json").write_text('{"image_name": "cwd"}\n', encoding="utf-8")
    config = tmp_path / "rosotacom.yaml"
    config.write_text("ros2docker_config: ros2docker.json\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    runtime = rosotacom._load_runtime_config(argparse.Namespace())

    assert runtime.rosotacom_config == config
    assert runtime.project_source == "local"
    assert runtime.ros2docker_config == tmp_path / "ros2docker.json"
    assert runtime.session_instances_dir == tmp_path / "session-instances"


def test_global_user_config_project_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "ros2docker.json").write_text('{"image_name": "global"}\n', encoding="utf-8")
    config = proj / "rosotacom.yaml"
    config.write_text("ros2docker_config: ros2docker.json\n", encoding="utf-8")
    rosotacom._write_user_project(config.resolve())

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    runtime = rosotacom._load_runtime_config(argparse.Namespace())

    assert runtime.rosotacom_config == config.resolve()
    assert runtime.project_source == "global"


def test_builtin_example_used_when_nothing_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    runtime = rosotacom._load_runtime_config(argparse.Namespace())

    assert runtime.project_source == "built-in"
    # The packaged example is used in place (read-only), not copied into $HOME.
    assert runtime.rosotacom_config == (rosotacom.EXAMPLE_PROJECT_DIR / "rosotacom.yaml").resolve()
    assert runtime.session_configs_dir is not None  # ships runnable sessions
    # Only the writable runtime output is redirected, to tmpfs (no $HOME writes).
    assert runtime.session_instances_dir == rosotacom._builtin_instances_dir()
    assert str(runtime.session_instances_dir).startswith(str(tmp_path / ".run"))


def test_config_set_project_global_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "rosotacom.yaml"
    config.write_text("ros2docker_config: ros2docker.json\n", encoding="utf-8")

    rc = rosotacom.config_command(
        argparse.Namespace(config_action="set", key="project", value=str(config), scope="global")
    )

    assert rc == 0
    assert rosotacom._user_config_project() == config.resolve()

    rosotacom.config_command(argparse.Namespace(config_action="unset", key="project", scope="global"))
    assert rosotacom._user_config_project() is None


def test_config_set_project_shell_prints_export(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "rosotacom.yaml"
    config.write_text("ros2docker_config: ros2docker.json\n", encoding="utf-8")

    rosotacom.config_command(argparse.Namespace(config_action="set", key="project", value=str(config), scope="shell"))

    assert capsys.readouterr().out.strip() == f"export ROSOTACOM_CONFIG={rosotacom.shlex.quote(str(config))}"


def test_rosotacom_yaml_relative_paths_resolve_from_config_dir(tmp_path: Path) -> None:
    (tmp_path / "ros2docker.json").write_text('{"image_name": "test"}\n', encoding="utf-8")
    (tmp_path / "data_dict.json").write_text('{"machine_a_ip": "127.0.0.1"}\n', encoding="utf-8")
    (tmp_path / "sessions").mkdir()
    config = tmp_path / "rosotacom.yaml"
    config.write_text(
        "\n".join(
            [
                "ros2docker_config: ros2docker.json",
                "session_configs_dir: sessions",
                "session_instances_dir: session-instances",
                "data_dict: data_dict.json",
                "",
            ]
        ),
        encoding="utf-8",
    )

    runtime = rosotacom._load_runtime_config(argparse.Namespace(rosotacom_config=str(config)))

    assert runtime.rosotacom_config == config
    assert runtime.ros2docker_config == tmp_path / "ros2docker.json"
    assert runtime.session_configs_dir == tmp_path / "sessions"
    assert runtime.session_instances_dir == tmp_path / "session-instances"
    assert runtime.data_dict == tmp_path / "data_dict.json"


def test_environment_config_is_used_when_no_config_arg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "ros2docker.json").write_text('{"image_name": "env"}\n', encoding="utf-8")
    (tmp_path / "sessions").mkdir()
    config = tmp_path / "rosotacom.yaml"
    config.write_text(
        "ros2docker_config: ros2docker.json\nsession_configs_dir: sessions\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ROSOTACOM_CONFIG", str(config))

    runtime = rosotacom._load_runtime_config(argparse.Namespace())

    assert runtime.rosotacom_config == config
    assert runtime.ros2docker_config == tmp_path / "ros2docker.json"
    assert runtime.session_configs_dir == tmp_path / "sessions"
    assert runtime.session_instances_dir == tmp_path / "session-instances"


def test_examples_create_copies_project_and_refuses_overwrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "rosotacom_examples"
    args = argparse.Namespace(target=str(target), force=False)

    rosotacom.examples_create_command(args)

    assert (target / "rosotacom.yaml").is_file()
    assert (target / "ros2docker.json").is_file()
    assert (target / "data_dict.json").is_file()
    assert "session-instances/" in (target / ".gitignore").read_text(encoding="utf-8")
    assert (target / "sessions" / "1_heartbeat_fastdds" / "session-definition.yaml").is_file()
    assert not (target / "__init__.py").exists()
    assert "Copied rosotacom examples" in capsys.readouterr().out

    with pytest.raises(RuntimeError, match="Target already exists"):
        rosotacom.examples_create_command(args)

    rosotacom.examples_create_command(argparse.Namespace(target=str(target), force=True))
    assert (target / "scripts" / "1_heartbeat" / "run_machine_a.sh").is_file()


def test_setup_env_prints_absolute_export(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "project setup" / "rosotacom.yaml"
    config.parent.mkdir()
    config.write_text("ros2docker_config: ros2docker.json\n", encoding="utf-8")

    rosotacom.setup_env_command(argparse.Namespace(rosotacom_config=str(config)))

    assert capsys.readouterr().out.strip() == f"export ROSOTACOM_CONFIG={rosotacom.shlex.quote(str(config))}"


def test_session_name_resolves_through_configured_sessions_dir(tmp_path: Path) -> None:
    session = tmp_path / "sessions" / "1_heartbeat"
    session.mkdir(parents=True)
    (session / "session-definition.yaml").write_text("peers: {}\n", encoding="utf-8")
    runtime = rosotacom.RuntimeConfig(
        rosotacom_config=tmp_path / "rosotacom.yaml",
        ros2docker_config=rosotacom.DEFAULT_ROS2DOCKER_CONFIG,
        session_configs_dir=tmp_path / "sessions",
        data_dict=None,
        install_id="test",
    )

    resolved = rosotacom._resolve_session("1_heartbeat", runtime)

    assert resolved.host_dir == session.resolve()
    assert resolved.container_dir == "/session/definitions/1_heartbeat"
    assert resolved.source == "session_configs"


def test_example_loopback_data_dict_resolves() -> None:
    runtime = rosotacom.RuntimeConfig(
        rosotacom_config=rosotacom.EXAMPLE_PROJECT_DIR / "rosotacom.yaml",
        ros2docker_config=rosotacom.EXAMPLE_PROJECT_DIR / "ros2docker.json",
        session_configs_dir=rosotacom.EXAMPLE_PROJECT_DIR / "sessions",
        data_dict=rosotacom.EXAMPLE_PROJECT_DIR / "data_dict.json",
        install_id="test",
    )

    assert rosotacom._resolved_address_expr_ips("data:machine_a_ip", runtime) == {"127.0.0.1"}
    assert rosotacom._resolved_address_expr_ips("data:machine_b_ip", runtime) == {"127.0.0.1"}


def test_version_flag_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        rosotacom.main(["--version"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.startswith("rosotacom ")


def test_positional_session_defaults_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[argparse.Namespace] = []

    def fake_start_session(args: argparse.Namespace) -> str:
        calls.append(args)
        return "container"

    monkeypatch.setattr(rosotacom, "start_session", fake_start_session)

    result = rosotacom.main(["1_heartbeat", "--identity", "a"])

    assert result == 0
    assert calls
    assert calls[0].session_dir == "1_heartbeat"
    assert calls[0].identity == "a"


def test_new_verification_verbs_dispatch_not_wrapped_in_start(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression guard: verify/probe-* must be in main()'s command set, otherwise
    # the "bare positional -> start" shim rewrites them as `start <verb> ...`.
    seen: list[tuple[str, argparse.Namespace]] = []

    def record(name: str) -> Callable[[argparse.Namespace], int]:
        def _cmd(args: argparse.Namespace) -> int:
            seen.append((name, args))
            return 0

        return _cmd

    for name in ("verify_command", "probe_publish_command", "probe_check_command"):
        monkeypatch.setattr(rosotacom, name, record(name))

    assert rosotacom.main(["verify", "1_heartbeat", "--identity", "a"]) == 0
    assert rosotacom.main(["probe-publish", "1_heartbeat", "--identity", "a"]) == 0
    assert rosotacom.main(["probe-check", "1_heartbeat", "--identity", "b", "--expect", "absent"]) == 0

    assert [n for n, _ in seen] == ["verify_command", "probe_publish_command", "probe_check_command"]
    assert all(args.session_dir == "1_heartbeat" for _, args in seen)
    assert seen[2][1].expect == "absent"


def test_start_and_stop_compat_entrypoints_prefix_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_main(argv: list[str] | None = None) -> int:
        calls.append(list(argv or []))
        return 0

    monkeypatch.setattr(rosotacom, "main", fake_main)
    monkeypatch.setattr(sys, "argv", ["start_rosotacom", "1_heartbeat"])
    assert rosotacom.start_compat_main() == 0
    monkeypatch.setattr(sys, "argv", ["stop_rosotacom", "1_heartbeat"])
    assert rosotacom.stop_compat_main() == 0

    assert calls == [["start", "1_heartbeat"], ["stop", "1_heartbeat"]]


def test_path_yaml_and_network_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    invalid_yaml = tmp_path / "list.yaml"
    invalid_yaml.write_text("- not\n- mapping\n", encoding="utf-8")

    assert rosotacom._load_yaml_file(None) == {}
    assert rosotacom._load_yaml_file(tmp_path / "missing.yaml") == {}
    with pytest.raises(RuntimeError, match="must contain a mapping"):
        rosotacom._load_yaml_file(invalid_yaml)
    assert rosotacom._first_value(None, "", "chosen") == "chosen"
    assert rosotacom._resolve_path("~/missing", tmp_path, must_exist=False).is_absolute()
    with pytest.raises(FileNotFoundError):
        rosotacom._resolve_path("missing", tmp_path, must_exist=True)
    assert rosotacom._sanitize_docker_name("a/b c") == "a_b_c"
    assert len(rosotacom._install_id(tmp_path)) == 8

    def fake_check_output(command: list[str], *, text: bool) -> str:
        if command[:4] == ["ip", "-o", "-4", "addr"]:
            return "1: lo inet 127.0.0.1/8\n2: eth0 inet 10.0.0.5/24\n"
        return "1.1.1.1 via 10.0.0.1 dev eth0 src 10.0.0.5 uid 1000"

    monkeypatch.setattr(rosotacom.subprocess, "check_output", fake_check_output)

    assert rosotacom._get_local_ipv4s() == ["127.0.0.1", "10.0.0.5"]
    assert rosotacom._default_local_ip() == "10.0.0.5"


def test_peer_override_identity_and_command_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dict = tmp_path / "data_dict.json"
    data_dict.write_text(
        '{"machines": {"local_box": "10.0.0.1", "remote_box": "10.0.0.2"}}',
        encoding="utf-8",
    )
    runtime = rosotacom.RuntimeConfig(
        rosotacom_config=tmp_path / "rosotacom.yaml",
        ros2docker_config=rosotacom.DEFAULT_ROS2DOCKER_CONFIG,
        session_configs_dir=None,
        data_dict=data_dict,
        install_id="abc",
    )
    cfg = {
        "peers": {
            "a": {"address": "0.0.0.0", "com-name": "local unit"},
            "b": {"address": "0.0.0.0", "com-name": "remote/unit"},
        }
    }

    overridden = rosotacom._apply_remote_peer_override_to_cfg(
        cfg,
        "b=remote_box",
        runtime,
        local_ips={"10.0.0.1"},
    )
    overridden = rosotacom._apply_peer_address_overrides_to_cfg(overridden, {"a": "data:local_box"})

    assert overridden["peers"]["a"]["address"] == "data:local_box"
    assert overridden["peers"]["b"]["address"] == "data:remote_box"
    assert rosotacom._resolved_address_expr_ips("data:local_box + data:remote_box", runtime) == {
        "10.0.0.1",
        "10.0.0.2",
    }
    assert rosotacom._contains_data_ref(overridden)
    monkeypatch.setattr(rosotacom, "_get_local_ipv4s", lambda: ["10.0.0.1"])
    assert rosotacom._auto_identity(tmp_path, runtime, overridden) == "a"
    assert rosotacom._remote_peer_name(overridden, "a") == "remote/unit"
    assert rosotacom._identity_container_names(overridden, runtime, "a") == ["rosotacom_abc_com_to_remote_unit"]

    session = rosotacom.ResolvedSession(tmp_path, "/session/current", "absolute")
    instance = rosotacom.SessionInstance(
        instance_id="run1",
        host_dir=tmp_path / "instances" / "2026-01-01" / "1_heartbeat_run1",
        container_dir="/session/instances/2026-01-01/1_heartbeat_run1",
        config_host_dir=tmp_path / "instances" / "2026-01-01" / "1_heartbeat_run1" / "config",
        config_container_dir="/session/instances/2026-01-01/1_heartbeat_run1/config",
        logs_host_dir=tmp_path / "instances" / "2026-01-01" / "1_heartbeat_run1" / "logs",
        logs_container_dir="/session/instances/2026-01-01/1_heartbeat_run1/logs",
        rosbags_host_dir=tmp_path / "instances" / "2026-01-01" / "1_heartbeat_run1" / "rosbags",
        rosbags_container_dir="/session/instances/2026-01-01/1_heartbeat_run1/rosbags",
    )
    assert rosotacom._session_command(
        session,
        instance,
        "a",
        force=True,
        rewrite_formatting=True,
        overwrite_peers_via_remote_peer="b=remote_box",
        peer_address_overrides={"a": "data:local_box"},
        attach_mode="attach",
    ) == [
        "/ws/session/creation/run_session.py",
        "--session-dir",
        "/session/current",
        "--output-dir",
        "/session/instances/2026-01-01/1_heartbeat_run1/config",
        "--instance-dir",
        "/session/instances/2026-01-01/1_heartbeat_run1",
        "--catmux-log-dir",
        "/session/instances/2026-01-01/1_heartbeat_run1/logs/a/catmux",
        "--rosbag-dir",
        "/session/instances/2026-01-01/1_heartbeat_run1/rosbags/a",
        "--identity",
        "a",
        "--force",
        "--rewrite-formatting",
        "--overwrite-peers-via-remote-peer",
        "b=remote_box",
        "--peer-address",
        "a=data:local_box",
        "--attach",
    ]

    with pytest.raises(RuntimeError, match="Duplicate"):
        rosotacom._parse_peer_address_overrides(["a=1.1.1.1", "a=2.2.2.2"])


def test_resolve_session_and_base_extra_run_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = tmp_path / "sessions" / "1_heartbeat"
    session.mkdir(parents=True)
    (session / "session-definition.yaml").write_text("peers: {}\n", encoding="utf-8")
    data_dict = tmp_path / "data.json"
    data_dict.write_text('{"machine_a_ip": "127.0.0.1"}', encoding="utf-8")
    runtime = rosotacom.RuntimeConfig(
        rosotacom_config=tmp_path / "rosotacom.yaml",
        ros2docker_config=tmp_path / "ros2docker.json",
        session_configs_dir=tmp_path / "sessions",
        data_dict=data_dict,
        install_id="id",
        session_instances_dir=tmp_path / "session-instances",
    )

    resolved = rosotacom._resolve_session("1_heartbeat", runtime)
    instance = rosotacom._resolve_session_instance(runtime, resolved, "test-run")
    monkeypatch.setattr(rosotacom, "load_config", lambda config: {"mount_ws": False}, raising=False)
    args = rosotacom._base_extra_run_args(
        runtime,
        resolved,
        {"peers": {"a": {"address": "data:machine_a_ip"}}},
        instance,
    )

    assert resolved.container_dir == "/session/definitions/1_heartbeat"
    assert f"{rosotacom.WS_DIR.resolve()}:/ws" in args
    assert f"{runtime.session_configs_dir}:/session/definitions:ro" in args
    assert f"{runtime.session_instances_dir}:/session/instances" in args
    assert f"{data_dict}:/data_dict.json:ro" in args
    assert "Configured sessions:" in rosotacom._format_available_sessions(runtime)


def test_container_helpers_use_docker_and_ros2docker_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = rosotacom.RuntimeConfig(None, tmp_path / "ros2docker.json", None, None, "id")
    calls: list[object] = []

    monkeypatch.setattr(rosotacom, "_require_ros2docker", lambda: None)
    monkeypatch.setattr(rosotacom, "_container_exists", lambda name: False)
    assert not rosotacom._stop_container_name("missing", runtime, quiet_missing=True)

    monkeypatch.setattr(rosotacom, "_container_exists", lambda name: True)
    monkeypatch.setattr(rosotacom, "ros2docker_stop", lambda **kwargs: calls.append(kwargs), raising=False)
    assert rosotacom._stop_container_name("running", runtime)
    assert calls == [{"config_file": runtime.ros2docker_config, "override": {"container_name": "running"}}]

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(command, 0, "Sourced ROS 2 workspace overlay", "")
        return subprocess.CompletedProcess(command, 0, "true\n", "")

    monkeypatch.setattr(rosotacom.subprocess, "run", fake_run)
    rosotacom._wait_for_container_ready("ready", timeout_s=1)
    assert ["docker", "logs", "ready"] in calls


def test_start_session_detached_dispatches_ros2docker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = rosotacom.RuntimeConfig(None, tmp_path / "ros2docker.json", None, None, "id", tmp_path / "instances")
    session = rosotacom.ResolvedSession(tmp_path, "/session/current", "absolute")
    cfg = {"peers": {"a": {}, "b": {"com-name": "remote"}}}
    calls: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(rosotacom, "_require_ros2docker", lambda: None)
    monkeypatch.setattr(rosotacom, "_load_runtime_config", lambda args: runtime)
    monkeypatch.setattr(rosotacom, "_resolve_session", lambda session_dir, runtime: session)
    monkeypatch.setattr(rosotacom, "_effective_session_config", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(rosotacom, "_scoped_image_name", lambda runtime: "image:id")
    monkeypatch.setattr(
        rosotacom,
        "_base_extra_run_args",
        lambda runtime, session, cfg, instance: ["--network", "host"],
    )
    monkeypatch.setattr(rosotacom, "_resolve_mode", lambda mode: "detached")
    monkeypatch.setattr(rosotacom, "_stop_container_name", lambda *args, **kwargs: True)
    monkeypatch.setattr(rosotacom, "_wait_for_container_ready", lambda name: None)
    monkeypatch.setattr(rosotacom, "_write_docker_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(rosotacom, "ros2docker_build", lambda **kwargs: calls.append(("build", kwargs)), raising=False)
    monkeypatch.setattr(rosotacom, "ros2docker_run", lambda **kwargs: calls.append(("run", kwargs)), raising=False)
    monkeypatch.setattr(rosotacom, "ros2docker_exec", lambda **kwargs: calls.append(("exec", kwargs)), raising=False)

    container = rosotacom.start_session(
        argparse.Namespace(
            session_dir="1_heartbeat",
            peer_address=["a=127.0.0.1"],
            overwrite_peers_via_remote_peer=None,
            identity="a",
            auto_identity=True,
            mode="detached",
            force=True,
            rewrite_formatting=False,
            instance_id="unit",
        )
    )

    assert container == "rosotacom_id_com_to_remote"
    assert [name for name, _ in calls] == ["build", "run", "exec"]
    assert calls[1][1]["override"]["run_type"] == "up"
    assert calls[2][1]["interactive"] is False
    assert "--output-dir" in calls[2][1]["command"]


def test_start_session_attach_dispatches_command_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = rosotacom.RuntimeConfig(None, tmp_path / "ros2docker.json", None, None, "id", tmp_path / "instances")
    session = rosotacom.ResolvedSession(tmp_path, "/session/current", "absolute")
    cfg = {"peers": {"a": {}, "b": {}}}
    calls: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(rosotacom, "_require_ros2docker", lambda: None)
    monkeypatch.setattr(rosotacom, "_load_runtime_config", lambda args: runtime)
    monkeypatch.setattr(rosotacom, "_resolve_session", lambda session_dir, runtime: session)
    monkeypatch.setattr(rosotacom, "_effective_session_config", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(rosotacom, "_auto_identity", lambda *args: "a")
    monkeypatch.setattr(rosotacom, "_scoped_image_name", lambda runtime: "image:id")
    monkeypatch.setattr(rosotacom, "_base_extra_run_args", lambda runtime, session, cfg, instance: [])
    monkeypatch.setattr(rosotacom, "_resolve_mode", lambda mode: "attach")
    monkeypatch.setattr(rosotacom, "_stop_container_name", lambda *args, **kwargs: True)
    monkeypatch.setattr(rosotacom, "_write_docker_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(rosotacom, "ros2docker_build", lambda **kwargs: calls.append(("build", kwargs)), raising=False)
    monkeypatch.setattr(rosotacom, "ros2docker_run", lambda **kwargs: calls.append(("run", kwargs)), raising=False)

    rosotacom.start_session(
        argparse.Namespace(
            session_dir="1_heartbeat",
            peer_address=[],
            overwrite_peers_via_remote_peer=None,
            identity=None,
            auto_identity=True,
            mode="attach",
            force=True,
            rewrite_formatting=False,
            instance_id="unit",
        )
    )

    assert [name for name, _ in calls] == ["build", "run"]
    assert calls[1][1]["override"]["run_type"] == "command"
    assert calls[1][1]["override"]["tty"] is True


def test_stop_list_doctor_and_smoke_host_flows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_dir = tmp_path / "sessions" / "1_heartbeat"
    (session_dir / "a").mkdir(parents=True)
    (session_dir / "b").mkdir()
    (session_dir / "session-definition.yaml").write_text("peers: {}\n", encoding="utf-8")
    (session_dir / "a" / "plugin.yaml").write_text("127.0.0.1\n", encoding="utf-8")
    (session_dir / "b" / "plugin.yaml").write_text("127.0.0.1\n", encoding="utf-8")
    runtime = rosotacom.RuntimeConfig(
        None,
        tmp_path / "ros2docker.json",
        tmp_path / "sessions",
        None,
        "id",
        tmp_path / "session-instances",
    )
    session = rosotacom.ResolvedSession(session_dir, "/session/definitions/1_heartbeat", "session_configs")
    instance = rosotacom.SessionInstance(
        instance_id="smoke",
        host_dir=tmp_path / "session-instances" / "2026-01-01" / "1_heartbeat_smoke",
        container_dir="/session/instances/2026-01-01/1_heartbeat_smoke",
        config_host_dir=tmp_path / "session-instances" / "2026-01-01" / "1_heartbeat_smoke" / "config",
        config_container_dir="/session/instances/2026-01-01/1_heartbeat_smoke/config",
        logs_host_dir=tmp_path / "session-instances" / "2026-01-01" / "1_heartbeat_smoke" / "logs",
        logs_container_dir="/session/instances/2026-01-01/1_heartbeat_smoke/logs",
        rosbags_host_dir=tmp_path / "session-instances" / "2026-01-01" / "1_heartbeat_smoke" / "rosbags",
        rosbags_container_dir="/session/instances/2026-01-01/1_heartbeat_smoke/rosbags",
    )
    (instance.config_host_dir / "a").mkdir(parents=True)
    (instance.config_host_dir / "b").mkdir()
    (instance.config_host_dir / "a" / "plugin.yaml").write_text("10.137.0.2\n", encoding="utf-8")
    (instance.config_host_dir / "b" / "plugin.yaml").write_text("10.137.0.3\n", encoding="utf-8")
    cfg = {"peers": {"a": {}, "b": {}}}
    stopped: list[str] = []

    monkeypatch.setattr(rosotacom, "_load_runtime_config", lambda args: runtime)
    monkeypatch.setattr(rosotacom, "_resolve_session", lambda session_dir, runtime: session)
    monkeypatch.setattr(rosotacom, "_resolve_session_instance", lambda runtime, session, instance_id=None: instance)
    monkeypatch.setattr(rosotacom, "_effective_session_config", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(rosotacom, "_identity_container_names", lambda cfg, runtime, identity=None: ["c1", "c2"])
    monkeypatch.setattr(rosotacom, "_stop_container_name", lambda name, runtime, **kwargs: stopped.append(name) or True)
    monkeypatch.setattr(rosotacom, "_ROS2DOCKER_IMPORT_ERROR", None)
    fake_ros2docker = type("FakeRos2Docker", (), {"__version__": "test", "__file__": __file__})
    monkeypatch.setattr(rosotacom, "ros2docker", fake_ros2docker)
    monkeypatch.setattr(rosotacom, "load_config", lambda config: {"mount_ws": True}, raising=False)
    monkeypatch.setattr(rosotacom, "_scoped_image_name", lambda runtime: "image:id")

    rosotacom.stop_session(argparse.Namespace(session_dir="1_heartbeat", identity=None, auto_identity=False))
    rosotacom.list_sessions(argparse.Namespace())
    assert stopped == ["c1", "c2"]
    assert "Configured sessions:" in capsys.readouterr().out

    assert rosotacom.doctor(argparse.Namespace()) == 0
    assert "OK: ros2docker validation: config loads" in capsys.readouterr().out

    monkeypatch.setattr(rosotacom, "start_session", lambda args: f"container_{args.identity}")
    monkeypatch.setattr(rosotacom, "_ensure_smoke_network", lambda: None)
    monkeypatch.setattr(rosotacom, "_remove_smoke_network", lambda: None)
    # Smoke's per-container delivery + isolation verification is exercised
    # end-to-end in tests/e2e; this unit test only drives the host flow
    # (start/stop/network), so stub the shared verification helpers as passing.
    monkeypatch.setattr(rosotacom, "_verify_received_topics", lambda *args, **kwargs: [])
    monkeypatch.setattr(rosotacom, "_verify_isolation", lambda *args, **kwargs: [])
    assert (
        rosotacom.smoke(
            argparse.Namespace(
                local=True,
                local_ip="127.0.0.1",
                session_dir="1_heartbeat",
                rosotacom_config=None,
                ros2docker_config=None,
                session_configs_dir=None,
                session_instances_dir=None,
                data_dict=None,
                instance_id="smoke",
                keep_running=False,
            )
        )
        == 0
    )
    assert stopped[-2:] == ["container_a", "container_b"]


def test_smoke_peer_addresses_use_isolated_bridge_ips() -> None:
    # Smoke isolates the two peers in their own network namespaces on a dedicated
    # docker bridge with distinct IPs, instead of sharing the host loopback.
    assert rosotacom._smoke_peer_address_args() == ["a=10.137.0.2", "b=10.137.0.3"]
    assert rosotacom.SMOKE_PEER_IPS == {"a": "10.137.0.2", "b": "10.137.0.3"}


def test_isolated_network_run_args_swaps_host_networking() -> None:
    base = ["-e", "ROS_DOMAIN_ID=48", "--network", "host"]
    swapped = rosotacom._isolated_network_run_args(base, "rosotacom-smoke", "10.137.0.2")
    assert "host" not in swapped
    assert swapped == ["-e", "ROS_DOMAIN_ID=48", "--network", "rosotacom-smoke", "--ip", "10.137.0.2"]
    # A config that already pins --network=... form is also replaced.
    assert rosotacom._isolated_network_run_args(["--network=host"], "net", None) == ["--network", "net"]


def test_smoke_crossed_topics_include_native_chatter_direction() -> None:
    cfg = yaml.safe_load(
        (rosotacom.EXAMPLE_PROJECT_DIR / "sessions" / "2_native_chatter" / "session-definition.yaml").read_text(
            encoding="utf-8"
        )
    )

    specs_a = rosotacom._received_crossed_topics(cfg, "a")
    specs_b = rosotacom._received_crossed_topics(cfg, "b")

    assert [(s.topic, s.label, s.publish_topic, s.publish_type) for s in specs_a] == [
        ("/com/in/b/chatter", "b->a inbound bridge topic", None, None),
        ("/chatter", "b->a final topic", "/chatter", "std_msgs/msg/String"),
    ]
    assert specs_b == []
    assert [(s.source_peer_key, s.receiver_peer_key, s.publish_topic) for s in rosotacom._smoke_publish_specs(cfg)] == [
        ("b", "a", "/chatter")
    ]
    assert "export ROS_DOMAIN_ID=46" in rosotacom._smoke_ros_setup("/config", cfg, "a")
    assert "export ROS_DOMAIN_ID=47" in rosotacom._smoke_ros_setup("/config", cfg, "b")


def test_smoke_crossed_topics_keep_heartbeat_labels() -> None:
    cfg = {"peers": {"a": {}, "b": {}}, "shared": {"use_heartbeat": True}}

    assert [(s.topic, s.label, s.enforce_bounds) for s in rosotacom._received_crossed_topics(cfg, "b")] == [
        ("/com/in/a/heartbeat_a", "a->b inbound bridge heartbeat", False),
        ("/heartbeat_a", "a->b final heartbeat", True),
    ]


def test_smoke_crossed_topics_include_compressed_occupancy_grid_pipeline() -> None:
    cfg = yaml.safe_load(
        (rosotacom.EXAMPLE_PROJECT_DIR / "sessions" / "3_comp_occ_grid" / "session-definition.yaml").read_text(
            encoding="utf-8"
        )
    )

    specs = rosotacom._received_crossed_topics(cfg, "a")
    costmap_specs = [s for s in specs if "costmap" in s.topic]

    assert [(s.topic, s.label) for s in costmap_specs] == [
        ("/com/in/b/costmap/costmap/restamped/bz2", "b->a inbound bridge topic"),
        ("/costmap/costmap/restamped", "b->a final topic"),
    ]
    final = costmap_specs[-1]
    assert final.publish_topic == "/costmap/costmap"
    assert final.publish_type == "nav_msgs/msg/OccupancyGrid"
    assert final.publish_rate == 3.0
    assert (final.hz_min, final.hz_max, final.max_delay_s) == (1.0, 5.0, 0.5)
    assert final.enforce_bounds
    assert "width: 4" in rosotacom._smoke_publish_message(final.publish_type)


def test_run_session_generates_into_instance_config_without_touching_static_source(tmp_path: Path) -> None:
    from session.creation import run_session

    source = tmp_path / "sessions" / "1_heartbeat"
    output = tmp_path / "session-instances" / "2026-01-01" / "1_heartbeat_run" / "config"
    source.mkdir(parents=True)
    (source / "session-definition.yaml").write_text(
        "\n".join(
            [
                "peers:",
                "  a:",
                "    address: 127.0.0.1",
                "  b:",
                "    address: 127.0.0.1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    peer_dir = run_session._resolve_peer_dir(
        str(source),
        str(output),
        "a",
        force=True,
        rewrite_formatting=False,
    )

    assert Path(peer_dir) == output / "a"
    assert (output / "session-definition.yaml").is_file()
    assert (output / "a" / "plugin.yaml").is_file()
    assert not (source / "a").exists()


def test_create_session_yaml_injects_catmux_logging_command(tmp_path: Path) -> None:
    from session.creation import create_session_yaml

    peer_dir = tmp_path / "config" / "a"
    peer_dir.mkdir(parents=True)
    (peer_dir / "plugin.yaml").write_text(
        "\n".join(
            [
                "parameters:",
                "  example: true",
                "common:",
                "  before_commands:",
                "    - echo existing",
                "windows:",
                "  - name: TEST",
                "    splits:",
                "      - commands:",
                "        - echo run",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (peer_dir / "session_specification.yaml").write_text("session_plugins:\n  - ./plugin.yaml\n", encoding="utf-8")

    create_session_yaml.main(
        str(peer_dir),
        instance_dir="/session/instances/run",
        config_dir="/session/instances/run/config",
        catmux_log_dir="/session/instances/run/logs/a/catmux",
        rosbag_dir="/session/instances/run/rosbags/a",
    )

    merged = yaml.safe_load((peer_dir / ".session_readonly.yaml").read_text(encoding="utf-8"))
    before_commands = merged["common"]["before_commands"]
    assert "catmux_log_setup.sh" in before_commands[0]
    assert "ROSOTACOM_CATMUX_LOG_DIR=/session/instances/run/logs/a/catmux" in before_commands[0]
    assert before_commands[1] == "echo existing"
