from __future__ import annotations

import argparse
import os
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
        "ROSOTACOM_SCENARIO_CONFIGS_DIR",
        "ROSOTACOM_SESSION_INSTANCES_DIR",
        "ROSOTACOM_DEPLOYMENT",
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
    (tmp_path / "deployment.yaml").write_text(
        "hosts:\n  local: {address: 127.0.0.1, ssh: null}\n",
        encoding="utf-8",
    )
    (tmp_path / "sessions").mkdir()
    (tmp_path / "scenarios").mkdir()
    config = tmp_path / "rosotacom.yaml"
    config.write_text(
        "\n".join(
            [
                "ros2docker_config: ros2docker.json",
                "session_configs_dir: sessions",
                "scenario_configs_dir: scenarios",
                "session_instances_dir: session-instances",
                "deployment: deployment.yaml",
                "",
            ]
        ),
        encoding="utf-8",
    )

    runtime = rosotacom._load_runtime_config(argparse.Namespace(rosotacom_config=str(config)))

    assert runtime.rosotacom_config == config
    assert runtime.ros2docker_config == tmp_path / "ros2docker.json"
    assert runtime.session_configs_dir == tmp_path / "sessions"
    assert runtime.scenario_configs_dir == tmp_path / "scenarios"
    assert runtime.session_instances_dir == tmp_path / "session-instances"
    assert runtime.deployment == tmp_path / "deployment.yaml"


def test_rosotacom_yaml_rejects_removed_data_dict_key(tmp_path: Path) -> None:
    (tmp_path / "ros2docker.json").write_text('{"image_name": "test"}\n', encoding="utf-8")
    config = tmp_path / "rosotacom.yaml"
    config.write_text(
        "ros2docker_config: ros2docker.json\ndata_dict: data_dict.json\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Unsupported rosotacom.yaml keys.*data_dict"):
        rosotacom._load_runtime_config(argparse.Namespace(rosotacom_config=str(config)))


def test_session_definition_rejects_physical_address_field() -> None:
    with pytest.raises(RuntimeError, match="Unsupported keys in peers.a.*address"):
        rosotacom.session_gen._validate_session_template_cfg({"peers": {"a": {"address": "10.0.0.1"}, "b": {}}})


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
    assert (target / "deployment.example.yaml").is_file()
    assert "session-instances/" in (target / ".gitignore").read_text(encoding="utf-8")
    assert (target / "sessions" / "1_heartbeat" / "session-definition.yaml").is_file()
    assert (target / "scenarios" / "2_native_chatter" / "scenario-definition.yaml").is_file()
    assert not (target / "__init__.py").exists()
    assert "Copied rosotacom examples" in capsys.readouterr().out

    with pytest.raises(RuntimeError, match="Target already exists"):
        rosotacom.examples_create_command(args)

    rosotacom.examples_create_command(argparse.Namespace(target=str(target), force=True))
    assert (target / "scripts" / "1_heartbeat" / "run_machine_a.sh").is_file()


def test_session_name_resolves_through_configured_sessions_dir(tmp_path: Path) -> None:
    session = tmp_path / "sessions" / "1_heartbeat"
    session.mkdir(parents=True)
    (session / "session-definition.yaml").write_text("peers: {}\n", encoding="utf-8")
    runtime = rosotacom.RuntimeConfig(
        rosotacom_config=tmp_path / "rosotacom.yaml",
        ros2docker_config=rosotacom.DEFAULT_ROS2DOCKER_CONFIG,
        session_configs_dir=tmp_path / "sessions",
        deployment=None,
        install_id="test",
    )

    resolved = rosotacom._resolve_session("1_heartbeat", runtime)

    assert resolved.host_dir == session.resolve()
    assert resolved.container_dir == "/session/definitions/1_heartbeat"
    assert resolved.source == "session_configs"


def test_session_name_completion_uses_active_project_and_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    for name in ("1_heartbeat", "1_heartbeat_status", "2_native_chatter"):
        session = sessions / name
        session.mkdir(parents=True)
        (session / "session-definition.yaml").write_text("peers: {}\n", encoding="utf-8")
    (sessions / "not_a_session").mkdir()
    (tmp_path / "ros2docker.json").write_text('{"image_name": "completion"}\n', encoding="utf-8")
    config = tmp_path / "rosotacom.yaml"
    config.write_text(
        "ros2docker_config: ros2docker.json\nsession_configs_dir: sessions\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    completions = rosotacom._session_name_completer("1_", argparse.Namespace())

    assert completions == {
        "1_heartbeat": "configured session",
        "1_heartbeat_status": "configured session",
    }

    external = tmp_path / "external_session"
    external.mkdir()
    path_completions = rosotacom._session_name_completer("./ext", argparse.Namespace())

    assert path_completions["./external_session/"] == "session directory"


def _write_test_scenario_project(tmp_path: Path) -> tuple[rosotacom.RuntimeConfig, rosotacom.ResolvedScenario]:
    sessions = tmp_path / "sessions"
    session = sessions / "demo"
    session.mkdir(parents=True)
    session.joinpath("session-definition.yaml").write_text(
        "\n".join(
            [
                "peers:",
                "  a: {}",
                "  b: {}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    app_dir = tmp_path / "apps"
    app_dir.mkdir()
    for identity in ("a", "b"):
        app_dir.joinpath(f"{identity}.json").write_text(
            "\n".join(
                [
                    "{",
                    f'  "container_name": "{identity}_app",',
                    '  "run_type": "command",',
                    '  "command": ["true"]',
                    "}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    scenario_dir = tmp_path / "scenarios" / "demo"
    scenario_dir.mkdir(parents=True)
    scenario_dir.joinpath("scenario-definition.yaml").write_text(
        "\n".join(
            [
                "schema_version: 1",
                "session: demo",
                "applications:",
                "  a:",
                "    - name: local_app",
                "      ros2docker_config: ../../apps/a.json",
                "  b:",
                "    - name: local_app",
                "      ros2docker_config: ../../apps/b.json",
                "",
            ]
        ),
        encoding="utf-8",
    )
    runtime = rosotacom.RuntimeConfig(
        rosotacom_config=tmp_path / "rosotacom.yaml",
        ros2docker_config=rosotacom.DEFAULT_ROS2DOCKER_CONFIG,
        session_configs_dir=sessions,
        deployment=None,
        install_id="test",
        session_instances_dir=tmp_path / "session-instances",
        scenario_configs_dir=tmp_path / "scenarios",
    )
    return runtime, rosotacom._resolve_scenario("demo", runtime)


def _ota_plan(tmp_path: Path) -> rosotacom.OtaSmokePlan:
    return rosotacom.OtaSmokePlan(
        state_path=tmp_path / "ota-deployment.yaml",
        workdir="/tmp/rosotacom_ota",
        rosotacom="source/.venv/bin/rosotacom",
        project="project/rosotacom.yaml",
        peers={
            "a": rosotacom.OtaSmokePeer("a", None, "10.0.0.10"),
            "b": rosotacom.OtaSmokePeer("b", "robot-b", "10.0.0.11"),
        },
    )


def test_scenario_definition_resolves_and_validates_strictly(tmp_path: Path) -> None:
    runtime, resolved = _write_test_scenario_project(tmp_path)

    definition = rosotacom._load_scenario_definition(resolved)

    assert definition.session == "demo"
    assert definition.applications["a"][0].name == "local_app"
    assert definition.applications["a"][0].ros2docker_config == tmp_path / "apps" / "a.json"
    assert rosotacom._scenario_names(runtime) == ["demo"]
    assert rosotacom._scenario_container_name(runtime, "demo", "a", "local_app") == (
        "rosotacom_test_scenario_demo_a_local_app"
    )
    assert rosotacom._scenario_application_image_name(runtime, definition.applications["a"][0]) == "ros2docker-test"

    resolved.definition_path.write_text(
        "schema_version: 1\nsession: demo\napplications: {}\nunknown: true\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="Unsupported keys"):
        rosotacom._load_scenario_definition(resolved)


def test_scenario_name_completion_uses_active_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)
    (tmp_path / "ros2docker.json").write_text('{"image_name": "completion"}\n', encoding="utf-8")
    runtime.rosotacom_config.write_text(
        "\n".join(
            [
                "ros2docker_config: ros2docker.json",
                "session_configs_dir: sessions",
                "scenario_configs_dir: scenarios",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert rosotacom._scenario_name_completer("de", argparse.Namespace()) == {"demo": "configured scenario"}


def test_active_scenarios_are_discovered_from_tmux_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[:3] == ["tmux", "-L", "rosotacom-test"]
        return subprocess.CompletedProcess(command, 0, "demo-a\tdemo\ta\ndemo-b\tdemo\tb\n", "")

    monkeypatch.setattr(rosotacom.shutil, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(rosotacom.subprocess, "run", fake_run)

    assert rosotacom._active_scenario_runs(runtime) == [
        rosotacom.ActiveScenarioRun("demo", "a", "demo-a"),
        rosotacom.ActiveScenarioRun("demo", "b", "demo-b"),
    ]
    assert rosotacom._format_scenario_listing(runtime) == "\n".join(
        [
            "Configured scenarios:",
            "  - demo (active: a, b)",
            "Active scenarios:",
            "  - demo --identity a",
            "  - demo --identity b",
        ]
    )


def test_scenario_attach_selector_infers_the_only_active_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)
    monkeypatch.setattr(rosotacom, "_load_runtime_config", lambda args: runtime)
    monkeypatch.setattr(
        rosotacom,
        "_active_scenario_runs",
        lambda runtime: [rosotacom.ActiveScenarioRun("demo", "a", "demo-a")],
    )
    args = argparse.Namespace(scenario=None, identity=None)

    rosotacom._infer_active_scenario_selector(args, require_active=True)

    assert (args.scenario, args.identity) == ("demo", "a")
    assert "Auto-selected active scenario: demo" in capsys.readouterr().out


def test_scenario_selector_requires_choice_for_multiple_active_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)
    monkeypatch.setattr(rosotacom, "_load_runtime_config", lambda args: runtime)
    monkeypatch.setattr(
        rosotacom,
        "_active_scenario_runs",
        lambda runtime: [
            rosotacom.ActiveScenarioRun("demo", "a", "demo-a"),
            rosotacom.ActiveScenarioRun("demo", "b", "demo-b"),
        ],
    )

    with pytest.raises(RuntimeError, match="multiple active identities"):
        rosotacom._infer_active_scenario_selector(
            argparse.Namespace(scenario="demo", identity=None),
            require_active=True,
        )


def test_scenario_and_identity_completion_follow_active_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)
    monkeypatch.setattr(rosotacom, "_load_runtime_config", lambda args: runtime)
    monkeypatch.setattr(
        rosotacom,
        "_active_scenario_runs",
        lambda runtime: [rosotacom.ActiveScenarioRun("demo", "a", "demo-a")],
    )

    attach_args = argparse.Namespace(command="scenario", scenario_command="attach", scenario=None)
    assert rosotacom._active_scenario_name_completer("d", attach_args) == {"demo": "active scenario"}
    assert rosotacom._identity_completer("", attach_args) == {"a": "peer identity"}

    start_args = argparse.Namespace(command="scenario", scenario_command="start", scenario="demo")
    assert rosotacom._identity_completer("", start_args) == {
        "a": "peer identity",
        "b": "peer identity",
    }

    session_args = argparse.Namespace(command="start", session_dir_positional="demo", session_dir=None)
    assert rosotacom._identity_completer("", session_args) == {
        "a": "peer identity",
        "b": "peer identity",
    }


def test_scenario_attach_cli_accepts_no_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[argparse.Namespace] = []
    monkeypatch.setattr(rosotacom, "attach_scenario", lambda args: calls.append(args) or 0)

    assert rosotacom.main(["scenario", "attach"]) == 0
    assert calls[0].scenario is None
    assert calls[0].identity is None


def test_scenario_tmux_commands_keep_ctrl_b_and_use_full_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, resolved = _write_test_scenario_project(tmp_path)
    definition = rosotacom._load_scenario_definition(resolved)
    session = rosotacom._resolve_session("demo", runtime)
    instance = rosotacom._resolve_session_instance(runtime, session, "tmux")
    calls: list[list[str]] = []
    pane_number = 0

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal pane_number
        calls.append(command)
        if "new-session" in command or "new-window" in command:
            pane_number += 1
            return subprocess.CompletedProcess(command, 0, f"%{pane_number}\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(rosotacom.subprocess, "run", fake_run)

    tmux_session = rosotacom._create_scenario_tmux(
        runtime,
        resolved,
        definition,
        instance,
        "a",
        definition.applications["a"],
        argparse.Namespace(
            force=True,
            rewrite_formatting=False,
            overwrite_peers_via_remote_peer=None,
            peer_address=[],
        ),
    )

    assert tmux_session == "demo-a"
    assert all(command[:3] == ["tmux", "-L", "rosotacom-test"] for command in calls)
    assert any(command[-2:] == ["prefix", "C-b"] for command in calls if "set-option" in command)
    assert any(command[-3:] == ["prefix", "C-b", "send-prefix"] for command in calls if "bind-key" in command)
    assert any(command[-2:] == ["@rosotacom_scenario", "demo"] for command in calls)
    assert any(command[-2:] == ["@rosotacom_identity", "a"] for command in calls)
    assert any("inner catmux: C-b C-b" in part for command in calls for part in command)
    assert any("rosotacom start demo --identity a --mode attach" in " ".join(command) for command in calls)
    assert any("scenario _run-application demo" in " ".join(command) for command in calls)
    assert sum("new-window" in command for command in calls) == 1
    assert not any("split-window" in command for command in calls)
    assert any(
        command[command.index("-n") : command.index("-n") + 2] == ["-n", "local_app"]
        for command in calls
        if "new-window" in command
    )
    assert any(command[-1] == "demo-a:communication" for command in calls if "select-window" in command)
    assert (instance.logs_host_dir / "a" / "scenario").is_dir()


def test_interactive_smoke_target_resolution_prefers_scenario_and_peer_ips(tmp_path: Path) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)

    target = rosotacom._resolve_interactive_smoke_target("demo", runtime, "auto")
    session_target = rosotacom._resolve_interactive_smoke_target("demo", runtime, "session")

    assert target.target_type == "scenario"
    assert target.name == "demo"
    assert session_target.target_type == "session"
    assert rosotacom._smoke_peer_ips_for_subnet(["robot_b", "robot_a"], "10.137.42.0/24") == {
        "robot_a": "10.137.42.2",
        "robot_b": "10.137.42.3",
    }


def test_interactive_smoke_tmux_uses_full_windows_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)
    target = rosotacom._resolve_interactive_smoke_target("demo", runtime, "scenario")
    instance = rosotacom._resolve_session_instance(runtime, target.session, "interactive")
    calls: list[list[str]] = []
    pane_number = 0

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal pane_number
        calls.append(command)
        if "new-session" in command or "new-window" in command:
            pane_number += 1
            return subprocess.CompletedProcess(command, 0, f"%{pane_number}\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(rosotacom.subprocess, "run", fake_run)

    tmux_session = rosotacom._create_interactive_smoke_tmux(
        runtime,
        target,
        instance,
        {"a": "10.137.42.2", "b": "10.137.42.3"},
        "smoke-net",
    )

    assert tmux_session == "smoke-scenario-demo"
    window_names = [
        command[command.index("-n") + 1]
        for command in calls
        if ("new-session" in command or "new-window" in command) and "-n" in command
    ]
    assert window_names == [
        "a_communication",
        "b_communication",
        "a_local_app",
        "b_local_app",
        "verification",
    ]
    assert any(command[-2:] == ["@rosotacom_smoke_target", "demo"] for command in calls)
    assert any(command[-2:] == ["@rosotacom_smoke_target_type", "scenario"] for command in calls)
    assert any(command[-2:] == ["@rosotacom_smoke_instance", "interactive"] for command in calls)
    assert any("inner catmux: C-b C-b" in part for command in calls for part in command)
    joined = "\n".join(" ".join(command) for command in calls)
    assert "--smoke-managed" in joined
    assert "--network-name smoke-net --network-ip 10.137.42.2" in joined
    assert "--peer-address a=10.137.42.2 --peer-address b=10.137.42.3" in joined
    assert "scenario _run-application demo --identity a --application local_app" in joined
    assert "--network-name container:rosotacom_test_com_to_b" in joined
    assert "waiting for generated config" in joined
    assert "waiting for container" in joined
    assert any(command[-1] == "smoke-scenario-demo:verification" for command in calls if "select-window" in command)


def test_run_scenario_application_can_override_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)
    (tmp_path / "apps" / "a.json").write_text(
        "\n".join(
            [
                "{",
                '  "container_name": "a_app",',
                '  "image_name": "app-image",',
                '  "run_type": "command",',
                '  "command": ["true"],',
                '  "run_args": ["--network", "host", "-e", "ROS_DOMAIN_ID=46"]',
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    runs: list[dict[str, object]] = []
    monkeypatch.setattr(rosotacom, "_require_ros2docker", lambda: None)
    monkeypatch.setattr(rosotacom, "_load_runtime_config", lambda args: runtime)
    monkeypatch.setattr(rosotacom, "_container_exists", lambda name: False)
    monkeypatch.setattr(rosotacom, "ros2docker_build", lambda **kwargs: None)
    monkeypatch.setattr(
        rosotacom,
        "ros2docker_run",
        lambda **kwargs: runs.append(dict(kwargs)),
    )

    assert (
        rosotacom.run_scenario_application(
            argparse.Namespace(
                scenario="demo",
                identity="a",
                application="local_app",
                network_name="smoke-net",
                network_ip=None,
            )
        )
        == 0
    )

    override = runs[0]["override"]
    assert isinstance(override, dict)
    assert override["container_name"] == "rosotacom_test_scenario_demo_a_local_app"
    assert override["image_name"] == "app-image-test"
    assert override["run_args"] == ["-e", "ROS_DOMAIN_ID=46", "--network", "smoke-net"]


def test_active_interactive_smoke_runs_are_listed_from_tmux_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[:3] == ["tmux", "-L", "rosotacom-test"]
        return subprocess.CompletedProcess(command, 0, "smoke-scenario-demo\tdemo\tscenario\trun1\tnet1\n", "")

    monkeypatch.setattr(rosotacom.shutil, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(rosotacom.subprocess, "run", fake_run)

    runs = rosotacom._active_interactive_smoke_runs(runtime)

    assert runs == [rosotacom.ActiveInteractiveSmokeRun("demo", "scenario", "smoke-scenario-demo", "run1", "net1")]
    assert "demo (scenario) instance=run1 network=net1" in rosotacom._format_active_interactive_smoke_runs(runs)


def test_ota_smoke_plan_writes_state_and_manifest(tmp_path: Path) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)
    plan = _ota_plan(tmp_path)
    target = rosotacom._resolve_interactive_smoke_target("demo", runtime, "auto")
    instance = rosotacom._resolve_session_instance(runtime, target.session, "ota")
    plan = rosotacom._ota_write_state(instance, plan)
    rosotacom._ota_write_manifest(
        instance,
        target,
        runtime,
        plan,
        tmux_session="ota-smoke-scenario-demo",
        interactive=True,
        phase="running",
    )
    manifest = yaml.safe_load((instance.host_dir / "manifest.yaml").read_text(encoding="utf-8"))
    run = manifest["ota_smoke_runs"]["scenario:demo"]

    assert target.target_type == "scenario"
    assert plan.peers["b"].ssh == "robot-b"
    assert run["deployment_state"] == str(instance.host_dir / "ota-deployment.yaml")
    assert run["peers"]["b"] == {"ssh_configured": True, "address": "10.0.0.11"}
    assert rosotacom._ota_load_state(str(plan.state_path)) == plan


def test_ota_smoke_command_building_and_remote_wrapping(tmp_path: Path) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)
    plan = _ota_plan(tmp_path)
    target = rosotacom._resolve_interactive_smoke_target("demo", runtime, "scenario")
    peer_args = rosotacom._ota_peer_address_args(plan)

    start = rosotacom._ota_rosotacom_command(
        plan,
        rosotacom._ota_start_parts(target, "a", "run1", peer_args, mode="detached"),
    )
    stop = rosotacom._ota_rosotacom_command(plan, rosotacom._ota_stop_parts(target, "b", "run1", peer_args))

    assert start.startswith("cd /tmp/rosotacom_ota && source/.venv/bin/rosotacom scenario start demo")
    assert "--identity a --mode detached --instance-id run1 --force" in start
    assert "--peer-address a=10.0.0.10 --peer-address b=10.0.0.11" in start
    assert "--rosotacom-config project/rosotacom.yaml" in start
    assert "scenario stop demo --identity b --instance-id run1" in stop
    assert rosotacom._ota_remote_argv(plan.peers["a"], "true") == ["bash", "-lc", "true"]
    assert rosotacom._ota_remote_argv(plan.peers["b"], "true", tty=True, batch=True) == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-t",
        "robot-b",
        "true",
    ]


def test_ota_workdir_validation_refuses_dangerous_paths() -> None:
    with pytest.raises(RuntimeError, match="dangerous OTA prepare workdir"):
        rosotacom._ota_validate_prepare_workdir("/")
    rosotacom._ota_validate_prepare_workdir("/tmp/rosotacom_ota")


def test_active_ota_smoke_runs_are_listed_from_tmux_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[:3] == ["tmux", "-L", "rosotacom-test"]
        return subprocess.CompletedProcess(
            command,
            0,
            "ota-smoke-scenario-demo\tdemo\tscenario\trun1\t/tmp/ota-state.yaml\n",
            "",
        )

    monkeypatch.setattr(rosotacom.shutil, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(rosotacom.subprocess, "run", fake_run)

    runs = rosotacom._active_ota_smoke_runs(runtime)

    assert runs == [
        rosotacom.ActiveOtaSmokeRun("demo", "scenario", "ota-smoke-scenario-demo", "run1", "/tmp/ota-state.yaml")
    ]
    assert "demo (scenario) instance=run1" in rosotacom._format_active_ota_smoke_runs(runs)


def test_interactive_ota_smoke_tmux_uses_control_windows_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)
    plan = _ota_plan(tmp_path)
    target = rosotacom._resolve_interactive_smoke_target("demo", runtime, "scenario")
    instance = rosotacom._resolve_session_instance(runtime, target.session, "ota-interactive")
    calls: list[list[str]] = []
    pane_number = 0

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal pane_number
        calls.append(command)
        if "new-session" in command or "new-window" in command:
            pane_number += 1
            return subprocess.CompletedProcess(command, 0, f"%{pane_number}\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(rosotacom.shutil, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(rosotacom.subprocess, "run", fake_run)

    tmux_session = rosotacom._ota_create_tmux(runtime, target, plan, instance)

    assert tmux_session == "ota-smoke-scenario-demo"
    window_names = [
        command[command.index("-n") + 1]
        for command in calls
        if ("new-session" in command or "new-window" in command) and "-n" in command
    ]
    assert window_names == ["a_remote", "b_remote", "a_shell", "b_shell", "verification"]
    assert any(command[-2:] == ["@rosotacom_ota_smoke_target", "demo"] for command in calls)
    assert any(command[-2:] == ["@rosotacom_ota_smoke_target_type", "scenario"] for command in calls)
    assert any(command[-2:] == ["@rosotacom_ota_smoke_instance", "ota-interactive"] for command in calls)
    assert any(command[-2:] == ["@rosotacom_ota_smoke_state", str(plan.state_path)] for command in calls)
    joined = "\n".join(" ".join(command) for command in calls)
    assert "scenario start demo --identity a --mode detached --instance-id ota-interactive" in joined
    assert "scenario start demo --identity b --mode detached --instance-id ota-interactive" in joined
    assert "status demo --identity a --instance-id ota-interactive --watch" in joined
    assert "status demo --identity b --instance-id ota-interactive --watch" in joined
    assert "ota-smoke demo --state-file" in joined
    assert "--verify-only" in joined
    assert "scenario attach" not in joined


def test_ota_smoke_parser_accepts_stop_without_peer_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[argparse.Namespace] = []
    monkeypatch.setattr(rosotacom, "ota_smoke", lambda args: calls.append(args) or 0)

    assert rosotacom.main(["ota-smoke", "--stop"]) == 0

    assert calls[0].target is None
    assert calls[0].stop is True
    assert calls[0].state_file is None


def test_ota_smoke_stop_dry_run_does_not_kill_local_tmux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)
    plan = _ota_plan(tmp_path)
    target = rosotacom._resolve_interactive_smoke_target("demo", runtime, "scenario")
    calls: list[str] = []
    monkeypatch.setattr(rosotacom, "_load_runtime_config", lambda args: runtime)
    monkeypatch.setattr(rosotacom, "_resolve_ota_smoke_context", lambda args: (runtime, plan, target))
    monkeypatch.setattr(rosotacom, "_ota_stop_peers", lambda *args, **kwargs: calls.append("remote-dry-run"))
    monkeypatch.setattr(rosotacom, "_kill_scenario_tmux", lambda *args, **kwargs: calls.append("tmux") or True)

    assert (
        rosotacom._stop_ota_smoke(
            argparse.Namespace(
                target="demo",
                target_type="scenario",
                state_file=str(plan.state_path),
                instance_id="run1",
                dry_run=True,
                keep_workdir=True,
            )
        )
        == 0
    )

    assert calls == ["remote-dry-run"]
    assert "Would stop OTA smoke tmux session: ota-smoke-scenario-demo" in capsys.readouterr().out


def test_noninteractive_ota_smoke_lifecycle_uses_generic_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)
    plan = _ota_plan(tmp_path)
    target = rosotacom._resolve_interactive_smoke_target("demo", runtime, "scenario")
    instance = rosotacom._resolve_session_instance(runtime, target.session, "ota-lifecycle")
    calls: list[str] = []

    monkeypatch.setattr(rosotacom, "_resolve_ota_smoke_context", lambda args: (runtime, plan, target))
    monkeypatch.setattr(rosotacom, "_resolve_session_instance", lambda *args, **kwargs: instance)
    monkeypatch.setattr(rosotacom, "_ota_preflight", lambda *args, **kwargs: calls.append("preflight"))
    monkeypatch.setattr(rosotacom, "_ota_prepare_hosts", lambda *args, **kwargs: calls.append("prepare"))
    monkeypatch.setattr(rosotacom, "_ota_start_peers", lambda *args, **kwargs: calls.append("start"))
    monkeypatch.setattr(rosotacom, "_ota_start_session_publishers", lambda *args, **kwargs: calls.append("publishers"))
    monkeypatch.setattr(rosotacom, "_ota_verify_delivery", lambda *args, **kwargs: calls.append("test") or [])
    monkeypatch.setattr(rosotacom, "_ota_verify_isolation", lambda *args, **kwargs: calls.append("isolation") or [])
    monkeypatch.setattr(rosotacom, "_ota_collect_logs", lambda *args, **kwargs: calls.append("collect"))
    monkeypatch.setattr(rosotacom, "_ota_stop_peers", lambda *args, **kwargs: calls.append("stop"))
    monkeypatch.setattr(rosotacom, "_ota_cleanup_hosts", lambda *args, **kwargs: calls.append("cleanup"))
    monkeypatch.setattr(rosotacom, "_ota_write_state", lambda instance, plan: plan)
    monkeypatch.setattr(rosotacom.time, "sleep", lambda seconds: calls.append(f"sleep:{seconds}"))

    assert (
        rosotacom._start_noninteractive_ota_smoke(
            argparse.Namespace(
                skip_preflight=False,
                check_peer_reachability=False,
                dry_run=False,
                instance_id="ota-lifecycle",
                keep_running=False,
                keep_workdir=False,
            )
        )
        == 0
    )

    assert calls == [
        "preflight",
        "prepare",
        "start",
        "sleep:12",
        "publishers",
        "test",
        "isolation",
        "collect",
        "stop",
        "cleanup",
    ]


def test_ota_smoke_dry_run_exercises_generic_remote_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)
    monkeypatch.setattr(rosotacom, "_load_runtime_config", lambda args: runtime)
    monkeypatch.setattr(rosotacom, "_ota_source_checkout", lambda: tmp_path)

    assert (
        rosotacom.ota_smoke(
            argparse.Namespace(
                target="demo",
                target_type="scenario",
                peer=[],
                peer_address=["a=10.0.0.10", "b=10.0.0.11"],
                peer_ssh=["b=robot-b"],
                deployment=None,
                interactive=False,
                stop=False,
                list=False,
                verify_only=False,
                reuse=False,
                workdir="/tmp/rosotacom_ota",
                keep_workdir=False,
                skip_preflight=False,
                check_peer_reachability=True,
                dry_run=True,
                instance_id="ota-dry",
                keep_running=False,
                mode="detached",
                state_file=None,
            )
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "stage rosotacom source" in out
    assert "install rosotacom" in out
    assert "required command tmux" in out
    assert "Python venv support" in out
    assert 'grep -Eq "[1-9][0-9]* received"' in out
    assert "ssh -o BatchMode=yes robot-b true" in out
    assert "scenario start demo --identity a --mode detached --instance-id ota-dry" in out
    assert "scenario start demo --identity b --mode detached --instance-id ota-dry" in out
    assert "test demo --instance-id ota-dry" in out
    assert "probe-publish demo --identity a --topic /local_only" in out
    assert "probe-check demo --identity b --topic /local_only --expect absent" in out
    assert "scenario stop demo --identity a --instance-id ota-dry" in out
    assert "*_ota-dry" in out
    assert "OTA SMOKE OK" in out


def test_interactive_smoke_stop_infers_active_run_and_cleans_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)
    target = rosotacom._resolve_interactive_smoke_target("demo", runtime, "scenario")
    calls: list[str] = []

    monkeypatch.setattr(rosotacom, "_require_ros2docker", lambda: None)
    monkeypatch.setattr(rosotacom, "_load_runtime_config", lambda args: runtime)
    monkeypatch.setattr(
        rosotacom,
        "_active_interactive_smoke_runs",
        lambda runtime: [
            rosotacom.ActiveInteractiveSmokeRun("demo", "scenario", "smoke-scenario-demo", "run1", "net1")
        ],
    )
    monkeypatch.setattr(rosotacom, "_resolve_interactive_smoke_target", lambda *args, **kwargs: target)
    monkeypatch.setattr(
        rosotacom,
        "_stop_scenario_application",
        lambda *args, **kwargs: calls.append("app") or True,
    )
    monkeypatch.setattr(
        rosotacom,
        "_stop_container_name",
        lambda *args, **kwargs: calls.append("communication") or True,
    )
    monkeypatch.setattr(rosotacom, "_kill_scenario_tmux", lambda *args, **kwargs: calls.append("tmux") or True)
    monkeypatch.setattr(rosotacom, "_remove_smoke_network", lambda *args, **kwargs: calls.append("network"))
    monkeypatch.setattr(rosotacom, "_find_latest_interactive_smoke_instance", lambda *args, **kwargs: None)

    assert (
        rosotacom._stop_interactive_smoke(argparse.Namespace(session_dir=None, target_type="auto", instance_id=None))
        == 0
    )

    assert calls == ["app", "app", "communication", "communication", "tmux", "network"]


def test_start_and_stop_scenario_manage_manifest_and_component_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, resolved = _write_test_scenario_project(tmp_path)
    definition = rosotacom._load_scenario_definition(resolved)
    session = rosotacom._resolve_session("demo", runtime)
    cfg = {"peers": {"a": {}, "b": {}}}
    instance = rosotacom._resolve_session_instance(runtime, session, "managed")
    calls: list[str] = []

    context = (runtime, resolved, definition, session, cfg, "a", definition.applications["a"])
    monkeypatch.setattr(rosotacom, "_require_ros2docker", lambda: None)
    monkeypatch.setattr(rosotacom, "_require_tmux", lambda: None)
    monkeypatch.setattr(rosotacom, "_resolve_scenario_context", lambda args: context)
    monkeypatch.setattr(rosotacom, "_tmux_session_exists", lambda runtime, name: False)
    monkeypatch.setattr(rosotacom, "_resolve_session_instance", lambda *args, **kwargs: instance)
    monkeypatch.setattr(rosotacom, "_remote_peer_name", lambda cfg, identity: "b")
    monkeypatch.setattr(
        rosotacom, "_create_scenario_tmux", lambda *args, **kwargs: calls.append("tmux-start") or "demo-a"
    )
    monkeypatch.setattr(rosotacom, "_resolve_mode", lambda mode: "detached")

    args = argparse.Namespace(
        scenario="demo",
        identity="a",
        force=True,
        mode="detached",
        instance_id="managed",
    )
    assert rosotacom.start_scenario(args) == 0
    manifest = yaml.safe_load((instance.host_dir / "manifest.yaml").read_text(encoding="utf-8"))
    run = manifest["scenario_runs"]["demo:a"]
    assert run["tmux_session"] == "demo-a"
    assert run["communication_container"] == "rosotacom_test_com_to_b"
    assert run["applications"][0]["container_name"] == "rosotacom_test_scenario_demo_a_local_app"
    assert run["applications"][0]["image_name"] == "ros2docker-test"
    assert calls == ["tmux-start"]

    monkeypatch.setattr(
        rosotacom,
        "_stop_scenario_application",
        lambda *args, **kwargs: calls.append("application-stop") or True,
    )
    monkeypatch.setattr(
        rosotacom,
        "_stop_container_name",
        lambda *args, **kwargs: calls.append("communication-stop") or True,
    )
    monkeypatch.setattr(
        rosotacom,
        "_kill_scenario_tmux",
        lambda *args, **kwargs: calls.append("tmux-stop") or True,
    )
    monkeypatch.setattr(rosotacom, "_find_latest_scenario_instance", lambda *args, **kwargs: instance.host_dir)

    assert rosotacom.stop_scenario(args) == 0
    assert calls[-3:] == ["application-stop", "communication-stop", "tmux-stop"]
    stopped_manifest = yaml.safe_load((instance.host_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert stopped_manifest["scenario_runs"]["demo:a"]["stopped_at"]


def test_completion_command_emits_shell_registration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    monkeypatch.setattr(sys, "argv", ["/tmp/bin/rosotacom@2.2.0"])

    assert rosotacom.completion_command(argparse.Namespace(shell=None)) == 0

    output = capsys.readouterr().out
    assert "_python_argcomplete_global" in output
    assert "compdef _python_argcomplete_global rosotacom@2.2.0" in output


def test_module_completion_registers_the_public_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["/tmp/site-packages/rosotacom/__main__.py"])

    assert rosotacom.completion_command(argparse.Namespace(shell="bash")) == 0

    assert "_python_argcomplete_global rosotacom" in capsys.readouterr().out


def test_argcomplete_protocol_returns_session_prefix_matches() -> None:
    line = "rosotacom smoke 1_"
    env = {
        **os.environ,
        "_ARGCOMPLETE": "1",
        "_ARGCOMPLETE_IFS": "\v",
        "_ARGCOMPLETE_SUPPRESS_SPACE": "1",
        "COMP_LINE": line,
        "COMP_POINT": str(len(line)),
    }

    result = subprocess.run(
        ["bash", "-c", f"{rosotacom.shlex.quote(sys.executable)} -m rosotacom 8>&1"],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.stdout.split("\v") == ["1_heartbeat", "1_heartbeat_status"]


def test_argcomplete_protocol_returns_identity_matches() -> None:
    line = "rosotacom start 1_heartbeat --identity "
    env = {
        **os.environ,
        "_ARGCOMPLETE": "1",
        "_ARGCOMPLETE_IFS": "\v",
        "_ARGCOMPLETE_SUPPRESS_SPACE": "1",
        "COMP_LINE": line,
        "COMP_POINT": str(len(line)),
    }

    result = subprocess.run(
        ["bash", "-c", f"{rosotacom.shlex.quote(sys.executable)} -m rosotacom 8>&1"],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.stdout.split("\v") == ["a", "b"]


def test_peer_completion_returns_logical_peers_hosts_and_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _resolved = _write_test_scenario_project(tmp_path)
    deployment = tmp_path / "deployment.yaml"
    deployment.write_text(
        "\n".join(
            [
                "hosts:",
                "  workstation: {address: 10.0.0.1, ssh: null}",
                "  robot: {address: 10.0.0.2, ssh: robot-b}",
                "values:",
                "  vpn:",
                "    gateway: 10.0.0.254",
                "",
            ]
        ),
        encoding="utf-8",
    )
    runtime = rosotacom.RuntimeConfig(
        rosotacom_config=runtime.rosotacom_config,
        ros2docker_config=runtime.ros2docker_config,
        session_configs_dir=runtime.session_configs_dir,
        deployment=deployment,
        install_id=runtime.install_id,
        session_instances_dir=runtime.session_instances_dir,
        scenario_configs_dir=runtime.scenario_configs_dir,
    )
    monkeypatch.setattr(rosotacom, "_load_runtime_config", lambda args: runtime)
    parsed = argparse.Namespace(command="start", session_dir_positional="demo", session_dir=None)

    assert rosotacom._peer_address_completer("", parsed) == {
        "a=": "peer address override",
        "b=": "peer address override",
    }
    assert rosotacom._peer_address_completer("a=value:vpn.", parsed) == {
        "a=value:vpn.gateway": "deployment value",
    }
    assert rosotacom._peer_host_completer("a=", parsed) == {
        "a=robot": "deployment host",
        "a=workstation": "deployment host",
    }


def test_smoke_interactive_parser_accepts_stop_without_target(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[argparse.Namespace] = []
    monkeypatch.setattr(rosotacom, "smoke", lambda args: calls.append(args) or 0)

    assert rosotacom.main(["smoke", "--interactive", "--stop"]) == 0

    assert calls[0].session_dir is None
    assert calls[0].interactive is True
    assert calls[0].interactive_stop is True


def test_local_check_derives_from_domains_and_allows_opt_out(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    derived = sessions / "derived"
    derived.mkdir(parents=True)
    derived.joinpath("session-definition.yaml").write_text(
        "\n".join(
            [
                "peers:",
                "  a: {}",
                "  b: {}",
                "peer_settings:",
                "  a: { domain_id: 46 }",
                "  b: { domain_id: 47 }",
                "",
            ]
        ),
        encoding="utf-8",
    )
    opted_out = sessions / "opted_out"
    opted_out.mkdir()
    opted_out.joinpath("session-definition.yaml").write_text(
        "\n".join(
            [
                "local_check: false",
                "peers:",
                "  a: {}",
                "  b: {}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    shared_domain = sessions / "shared_domain"
    shared_domain.mkdir()
    shared_domain.joinpath("session-definition.yaml").write_text(
        "\n".join(
            [
                "peers:",
                "  a: {}",
                "  b: {}",
                "shared:",
                "  local_domain_id: 46",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert rosotacom.session_local_checks(sessions) == {
        "derived": True,
        "opted_out": False,
        "shared_domain": False,
    }
    assert rosotacom.local_check_sessions(sessions) == ["derived"]
    assert set(rosotacom.ota_suite_sessions(sessions)) == {"derived", "opted_out", "shared_domain"}


def test_examples_have_logical_peers_without_default_deployment() -> None:
    runtime = rosotacom.RuntimeConfig(
        rosotacom_config=rosotacom.EXAMPLE_PROJECT_DIR / "rosotacom.yaml",
        ros2docker_config=rosotacom.EXAMPLE_PROJECT_DIR / "ros2docker.json",
        session_configs_dir=rosotacom.EXAMPLE_PROJECT_DIR / "sessions",
        deployment=None,
        install_id="test",
    )
    cfg = rosotacom._effective_session_config(
        rosotacom.EXAMPLE_PROJECT_DIR / "sessions" / "1_heartbeat",
        runtime,
    )
    assert cfg["peers"] == {"a": {}, "b": {}}
    with pytest.raises(RuntimeError, match="Missing deployment address"):
        rosotacom._resolve_bindings(cfg, runtime)


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


def test_probe_verbs_dispatch_not_wrapped_in_start(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression guard: probe-* must be in main()'s command set, otherwise
    # the "bare positional -> start" shim rewrites them as `start <verb> ...`.
    seen: list[tuple[str, argparse.Namespace]] = []

    def record(name: str) -> Callable[[argparse.Namespace], int]:
        def _cmd(args: argparse.Namespace) -> int:
            seen.append((name, args))
            return 0

        return _cmd

    for name in ("probe_publish_command", "probe_check_command", "publish_test_topics_command"):
        monkeypatch.setattr(rosotacom, name, record(name))

    assert rosotacom.main(["probe-publish", "1_heartbeat", "--identity", "a"]) == 0
    assert rosotacom.main(["probe-check", "1_heartbeat", "--identity", "b", "--expect", "absent"]) == 0
    assert rosotacom.main(["publish-test-topics", "1_heartbeat", "--identity", "b"]) == 0

    assert [n for n, _ in seen] == [
        "probe_publish_command",
        "probe_check_command",
        "publish_test_topics_command",
    ]
    assert all(args.session_dir == "1_heartbeat" for _, args in seen)
    assert seen[1][1].expect == "absent"


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


def test_peer_binding_identity_and_command_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deployment = tmp_path / "deployment.yaml"
    deployment.write_text(
        "\n".join(
            [
                "hosts:",
                "  local_box: {address: 10.0.0.1, ssh: null}",
                "  remote_box: {address: 10.0.0.2, ssh: remote}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    runtime = rosotacom.RuntimeConfig(
        rosotacom_config=tmp_path / "rosotacom.yaml",
        ros2docker_config=rosotacom.DEFAULT_ROS2DOCKER_CONFIG,
        session_configs_dir=None,
        deployment=deployment,
        install_id="abc",
    )
    cfg = {
        "peers": {
            "a": {"com-name": "local unit"},
            "b": {"com-name": "remote/unit"},
        }
    }

    bindings = rosotacom._resolve_bindings(
        cfg,
        runtime,
        peer=["a=local_box", "b=remote_box"],
    )
    assert bindings["a"] == rosotacom.PeerBinding("a", "10.0.0.1", None, "local_box")
    assert bindings["b"] == rosotacom.PeerBinding("b", "10.0.0.2", "remote", "remote_box")
    monkeypatch.setattr(rosotacom, "_get_local_ipv4s", lambda: ["10.0.0.1"])
    assert rosotacom._auto_identity(bindings) == "a"
    assert rosotacom._remote_peer_name(cfg, "a") == "remote/unit"
    assert rosotacom._identity_container_names(cfg, runtime, "a") == ["rosotacom_abc_com_to_remote_unit"]

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
        peer_address_overrides={"a": "10.0.0.1", "b": "10.0.0.2"},
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
        "--peer-address",
        "a=10.0.0.1",
        "--peer-address",
        "b=10.0.0.2",
        "--attach",
    ]

    with pytest.raises(RuntimeError, match="Duplicate"):
        rosotacom._parse_peer_address_overrides(["a=1.1.1.1", "a=2.2.2.2"])


def test_resolve_session_and_base_extra_run_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = tmp_path / "sessions" / "1_heartbeat"
    session.mkdir(parents=True)
    (session / "session-definition.yaml").write_text("peers: {}\n", encoding="utf-8")
    runtime = rosotacom.RuntimeConfig(
        rosotacom_config=tmp_path / "rosotacom.yaml",
        ros2docker_config=tmp_path / "ros2docker.json",
        session_configs_dir=tmp_path / "sessions",
        deployment=None,
        install_id="id",
        session_instances_dir=tmp_path / "session-instances",
    )

    resolved = rosotacom._resolve_session("1_heartbeat", runtime)
    instance = rosotacom._resolve_session_instance(runtime, resolved, "test-run")
    monkeypatch.setattr(rosotacom, "load_config", lambda config: {"mount_ws": False}, raising=False)
    args = rosotacom._base_extra_run_args(
        runtime,
        resolved,
        {"peers": {"a": {}, "b": {}}},
        instance,
    )

    assert resolved.container_dir == "/session/definitions/1_heartbeat"
    assert f"{rosotacom.WS_DIR.resolve()}:/ws" in args
    assert f"{runtime.session_configs_dir}:/session/definitions:ro" in args
    assert f"{runtime.session_instances_dir}:/session/instances" in args
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
            peer=[],
            peer_address=["a=127.0.0.1", "b=127.0.0.2"],
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
            peer=[],
            peer_address=["a=127.0.0.1", "b=127.0.0.2"],
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
    monkeypatch.setattr(rosotacom, "test_command", lambda args: 0)
    publisher_durations: list[float] = []

    def fake_start_publishers(*args: object, **kwargs: object) -> list[rosotacom.SmokeTopicSpec]:
        publisher_durations.append(float(kwargs["duration"]))
        return []

    monkeypatch.setattr(rosotacom, "_start_smoke_topic_publishers", fake_start_publishers)
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
                deployment=None,
                instance_id="smoke",
                keep_running=False,
            )
        )
        == 0
    )
    assert publisher_durations == [rosotacom.SMOKE_PUBLISHER_DURATION_S]
    assert rosotacom.SMOKE_PUBLISHER_DURATION_S == 900.0
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
    assert [
        (s.source_peer_key, s.receiver_peer_key, s.publish_topic)
        for s in rosotacom._smoke_publish_specs(cfg, source_peer_key="b")
    ] == [("b", "a", "/chatter")]
    assert rosotacom._smoke_publish_specs(cfg, source_peer_key="a") == []
    assert "export ROS_DOMAIN_ID=46" in rosotacom._smoke_ros_setup("/config", cfg, "a")
    assert "export ROS_DOMAIN_ID=47" in rosotacom._smoke_ros_setup("/config", cfg, "b")


def test_publish_test_topics_command_starts_and_stops_identity_publishers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = yaml.safe_load(
        (rosotacom.EXAMPLE_PROJECT_DIR / "sessions" / "2_native_chatter" / "session-definition.yaml").read_text(
            encoding="utf-8"
        )
    )
    started: list[tuple[str, float | None]] = []
    stopped: list[str] = []

    monkeypatch.setattr(rosotacom, "_resolve_running_peer", lambda args, identity: ("container_b", "source ros", cfg))

    def fake_start(
        containers: dict[str, str],
        ros_setups: dict[str, str],
        cfg: dict[str, object],
        **kwargs: object,
    ) -> list[rosotacom.SmokeTopicSpec]:
        started.append((containers["b"], kwargs.get("duration") if isinstance(kwargs.get("duration"), float) else None))
        return rosotacom._smoke_publish_specs(cfg, source_peer_key="b")

    def fake_stop(containers: dict[str, str], specs: list[rosotacom.SmokeTopicSpec]) -> None:
        stopped.extend([containers[s.source_peer_key] for s in specs])

    monkeypatch.setattr(rosotacom, "_start_smoke_topic_publishers", fake_start)
    monkeypatch.setattr(rosotacom, "_stop_smoke_topic_publishers", fake_stop)

    assert (
        rosotacom.publish_test_topics_command(
            argparse.Namespace(identity="b", duration=42.0, stop=False, session_dir="2_native_chatter")
        )
        == 0
    )
    assert started == [("container_b", 42.0)]
    assert "Started 1 test topic publisher" in capsys.readouterr().out

    assert (
        rosotacom.publish_test_topics_command(
            argparse.Namespace(identity="b", duration=42.0, stop=True, session_dir="2_native_chatter")
        )
        == 0
    )
    assert stopped == ["container_b"]


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


def test_smoke_crossed_topics_include_zenoh_compressed_occupancy_grid_pipeline() -> None:
    cfg = yaml.safe_load(
        (rosotacom.EXAMPLE_PROJECT_DIR / "sessions" / "4_comp_occ_grid_zen" / "session-definition.yaml").read_text(
            encoding="utf-8"
        )
    )

    specs = [s for s in rosotacom._received_crossed_topics(cfg, "a") if "costmap" in s.topic]

    assert [(s.topic, s.label) for s in specs] == [
        ("/com/in/b/costmap/costmap/restamped/bz2", "b->a inbound bridge topic"),
        ("/costmap/costmap/restamped", "b->a final topic"),
    ]
    assert specs[-1].publish_type == "nav_msgs/msg/OccupancyGrid"


@pytest.mark.parametrize(
    ("session_name", "receiver", "expected_topics"),
    [
        (
            "5_sized_payload",
            "b",
            ["/com/in/a/size_test_a/ota_stamped", "/size_test_a"],
        ),
        (
            "6_sized_payload_zen",
            "b",
            ["/com/in/a/size_test_a", "/size_test_a"],
        ),
    ],
)
def test_smoke_crossed_topics_include_sized_payload_pipelines(
    session_name: str,
    receiver: str,
    expected_topics: list[str],
) -> None:
    cfg = yaml.safe_load(
        (rosotacom.EXAMPLE_PROJECT_DIR / "sessions" / session_name / "session-definition.yaml").read_text(
            encoding="utf-8"
        )
    )

    specs = [s for s in rosotacom._received_crossed_topics(cfg, receiver) if "size_test" in s.topic]

    assert [s.topic for s in specs] == expected_topics
    final = specs[-1]
    assert final.publish_topic == "/size_test_a"
    assert final.publish_type == "com_msgs/msg/SizedPayload"
    assert final.expected_size == 66000
    command = rosotacom._smoke_publisher_command(final, "source ros", 180.0)
    assert "ros2 run com_py sized_publisher" in command
    assert "-p topic:=/size_test_a" in command
    assert "-p size:=66000" in command


def test_run_session_generates_into_instance_config_without_touching_static_source(tmp_path: Path) -> None:
    from session.creation import run_session

    source = tmp_path / "sessions" / "1_heartbeat"
    output = tmp_path / "session-instances" / "2026-01-01" / "1_heartbeat_run" / "config"
    source.mkdir(parents=True)
    (source / "session-definition.yaml").write_text(
        "\n".join(
            [
                "peers:",
                "  a: {}",
                "  b: {}",
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
        peer_address=["a=127.0.0.1", "b=127.0.0.2"],
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
