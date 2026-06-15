from __future__ import annotations

import argparse
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import rosotacom


class RosotacomCliTests(unittest.TestCase):
    def _clear_config_env(self):
        return mock.patch.dict(
            os.environ,
            {
                "ROSOTACOM_CONFIG": "",
                "ROSOTACOM_ROS2DOCKER_CONFIG": "",
                "ROSOTACOM_SESSION_CONFIGS_DIR": "",
                "ROSOTACOM_DATA_DICT": "",
            },
        )

    def test_no_rosotacom_yaml_auto_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self._clear_config_env():
            tmp_path = Path(tmp)
            (tmp_path / "rosotacom.yaml").write_text("ros2docker_config: missing.json\n", encoding="utf-8")
            old_cwd = Path.cwd()
            try:
                os.chdir(tmp_path)
                runtime = rosotacom._load_runtime_config(argparse.Namespace())
            finally:
                os.chdir(old_cwd)

        self.assertIsNone(runtime.rosotacom_config)
        self.assertEqual(runtime.ros2docker_config, rosotacom.DEFAULT_ROS2DOCKER_CONFIG.resolve())

    def test_rosotacom_yaml_relative_paths_resolve_from_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self._clear_config_env():
            project = Path(tmp)
            (project / "ros2docker.json").write_text('{"image_name": "test"}\n', encoding="utf-8")
            (project / "data_dict.json").write_text('{"machine_a_ip": "127.0.0.1"}\n', encoding="utf-8")
            (project / "sessions").mkdir()
            config = project / "rosotacom.yaml"
            config.write_text(
                "\n".join(
                    [
                        "ros2docker_config: ros2docker.json",
                        "session_configs_dir: sessions",
                        "data_dict: data_dict.json",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            runtime = rosotacom._load_runtime_config(argparse.Namespace(rosotacom_config=str(config)))

        self.assertEqual(runtime.rosotacom_config, config)
        self.assertEqual(runtime.ros2docker_config, project / "ros2docker.json")
        self.assertEqual(runtime.session_configs_dir, project / "sessions")
        self.assertEqual(runtime.data_dict, project / "data_dict.json")

    def test_examples_create_copies_project_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "rosotacom_examples"
            args = argparse.Namespace(target=str(target), force=False)

            with contextlib.redirect_stdout(io.StringIO()):
                rosotacom.examples_create_command(args)

            self.assertTrue((target / "rosotacom.yaml").is_file())
            self.assertTrue((target / "ros2docker.json").is_file())
            self.assertTrue((target / "data_dict.json").is_file())
            self.assertTrue((target / "sessions" / "1_heartbeat" / "session-definition.yaml").is_file())
            self.assertFalse((target / "__init__.py").exists())

            with self.assertRaises(RuntimeError):
                rosotacom.examples_create_command(args)

            with contextlib.redirect_stdout(io.StringIO()):
                rosotacom.examples_create_command(argparse.Namespace(target=str(target), force=True))
            self.assertTrue((target / "scripts" / "1_heartbeat" / "run_machine_a.sh").is_file())

    def test_setup_env_prints_absolute_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "project setup" / "rosotacom.yaml"
            config.parent.mkdir()
            config.write_text("ros2docker_config: ros2docker.json\n", encoding="utf-8")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rosotacom.setup_env_command(argparse.Namespace(rosotacom_config=str(config)))

        self.assertEqual(out.getvalue().strip(), f"export ROSOTACOM_CONFIG={rosotacom.shlex.quote(str(config))}")

    def test_session_name_resolves_through_configured_sessions_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            session = project / "sessions" / "1_heartbeat"
            session.mkdir(parents=True)
            (session / "session-definition.yaml").write_text("peers: {}\n", encoding="utf-8")
            runtime = rosotacom.RuntimeConfig(
                rosotacom_config=project / "rosotacom.yaml",
                ros2docker_config=rosotacom.DEFAULT_ROS2DOCKER_CONFIG,
                session_configs_dir=project / "sessions",
                data_dict=None,
                install_id="test",
            )

            resolved = rosotacom._resolve_session("1_heartbeat", runtime)

        self.assertEqual(resolved.host_dir, session.resolve())
        self.assertEqual(resolved.container_dir, "/session/configs/1_heartbeat")
        self.assertEqual(resolved.source, "session_configs")

    def test_example_loopback_data_dict_resolves(self) -> None:
        runtime = rosotacom.RuntimeConfig(
            rosotacom_config=rosotacom.EXAMPLE_PROJECT_DIR / "rosotacom.yaml",
            ros2docker_config=rosotacom.EXAMPLE_PROJECT_DIR / "ros2docker.json",
            session_configs_dir=rosotacom.EXAMPLE_PROJECT_DIR / "sessions",
            data_dict=rosotacom.EXAMPLE_PROJECT_DIR / "data_dict.json",
            install_id="test",
        )

        self.assertEqual(rosotacom._resolved_address_expr_ips("data:machine_a_ip", runtime), {"127.0.0.1"})
        self.assertEqual(rosotacom._resolved_address_expr_ips("data:machine_b_ip", runtime), {"127.0.0.1"})


if __name__ == "__main__":
    unittest.main()
