#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""rosotacom host CLI.

This module owns rosotacom-specific concepts such as session config
directories, data_dict wiring, multi-checkout names, session instance
lifecycle, and local smoke tests.
ros2docker remains the generic Docker runner underneath.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.resources as importlib_resources
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import argcomplete
import yaml
from argcomplete.completers import DirectoriesCompleter

from . import __version__

ros2docker: ModuleType | None
_ROS2DOCKER_IMPORT_ERROR: Exception | None

try:
    import ros2docker as _ros2docker
    from ros2docker.api import build as ros2docker_build
    from ros2docker.api import exec_shell as ros2docker_exec
    from ros2docker.api import run as ros2docker_run
    from ros2docker.api import stop as ros2docker_stop
    from ros2docker.config import load_config
except Exception as exc:  # noqa: BLE001 - reported by doctor/start with context.
    ros2docker = None
    _ROS2DOCKER_IMPORT_ERROR = exc
else:
    ros2docker = _ros2docker
    _ROS2DOCKER_IMPORT_ERROR = None


PACKAGE_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = PACKAGE_DIR / "resources"
PROJECT_DIR = RESOURCE_DIR
WS_DIR = RESOURCE_DIR / "ws"
DEFAULT_ROS2DOCKER_CONFIG = RESOURCE_DIR / "ros2docker.json.example"
EXAMPLE_PROJECT_DIR = RESOURCE_DIR / "examples"
SESSION_CONFIG_CONTAINER_DIR = "/session/configs"
SESSION_DEFINITION_CONTAINER_DIR = "/session/definitions"
SESSION_INSTANCE_CONTAINER_DIR = "/session/instances"
EXTERNAL_SESSION_CONTAINER_DIR = "/session/current"
CONTAINER_DATA_DICT_PATH = "/data_dict.json"
RUN_SESSION_CONTAINER_PATH = "/ws/session/creation/run_session.py"
DEFAULT_SMOKE_SESSION = "1_heartbeat"

ws_creation_dir = WS_DIR / "session" / "creation"
session_gen_path = ws_creation_dir / "generate_session_files.py"
spec = importlib.util.spec_from_file_location("session_gen", session_gen_path)
assert spec is not None and spec.loader is not None
session_gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session_gen)

sys.path.append(str(WS_DIR))
from session.content.address_resolution import (  # noqa: E402,I001
    find_data_dict_leaf,
    format_data_reference,
    load_data_dict,
    parse_data_reference,
    resolve_address_expression,
)


_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_DATA_REF_RE = re.compile(r"(^|\+)\s*data:")


@dataclass(frozen=True)
class RuntimeConfig:
    rosotacom_config: Path | None
    ros2docker_config: Path
    session_configs_dir: Path | None
    data_dict: Path | None
    install_id: str
    session_instances_dir: Path | None = None
    project_source: str | None = None
    scenario_configs_dir: Path | None = None


@dataclass(frozen=True)
class ResolvedSession:
    host_dir: Path
    container_dir: str
    source: str


@dataclass(frozen=True)
class ResolvedScenario:
    name: str
    host_dir: Path
    definition_path: Path
    source: str


@dataclass(frozen=True)
class ScenarioApplication:
    name: str
    ros2docker_config: Path


@dataclass(frozen=True)
class ScenarioDefinition:
    session: str
    applications: dict[str, tuple[ScenarioApplication, ...]]


@dataclass(frozen=True)
class ActiveScenarioRun:
    scenario: str
    identity: str
    tmux_session: str


@dataclass(frozen=True)
class SessionInstance:
    instance_id: str
    host_dir: Path
    container_dir: str
    config_host_dir: Path
    config_container_dir: str
    logs_host_dir: Path
    logs_container_dir: str
    rosbags_host_dir: Path
    rosbags_container_dir: str


@dataclass(frozen=True)
class SmokeTopicSpec:
    source_peer_key: str
    receiver_peer_key: str
    topic: str
    label: str
    enforce_bounds: bool
    use_default_bounds: bool = False
    publish_topic: str | None = None
    publish_type: str | None = None
    publish_rate: float = 5.0
    hz_min: float | None = None
    hz_max: float | None = None
    max_delay_s: float | None = None
    expected_size: int | None = None


def _load_yaml_file(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"Config file must contain a mapping: {path}")
    return loaded


def _first_value(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _resolve_path(raw: str | os.PathLike[str] | None, base_dir: Path, *, must_exist: bool = True) -> Path | None:
    if raw is None or str(raw).strip() == "":
        return None
    path = Path(os.path.expandvars(os.path.expanduser(str(raw))))
    if not path.is_absolute():
        candidate = (base_dir / path).resolve()
        if candidate.exists() or base_dir != PROJECT_DIR:
            path = candidate
        else:
            path = (PROJECT_DIR / path).resolve()
    else:
        path = path.resolve()
    if must_exist and not path.exists():
        raise FileNotFoundError(f"Path does not exist: {raw!r} (resolved to {path})")
    return path


def _install_id(scope_path: Path | None = None) -> str:
    scope = scope_path.resolve() if scope_path else PROJECT_DIR.resolve()
    return hashlib.sha1(str(scope).encode("utf-8")).hexdigest()[:8]


def _xdg_dir(env_var: str, default_suffix: str) -> Path:
    base = os.environ.get(env_var)
    return Path(base) if base else Path.home() / default_suffix


def _user_config_file() -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", ".config") / "rosotacom" / "config.yaml"


def _runtime_dir() -> Path:
    """A per-user, RAM-backed scratch dir for ephemeral runtime artifacts.

    ``XDG_RUNTIME_DIR`` is tmpfs on Linux and auto-cleaned on logout, so it is the
    correct home for generated session files (which must be real paths because
    they are bind-mounted into the container) without touching ``$HOME``.
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / "rosotacom"
    uid = getattr(os, "getuid", lambda: os.getpid())()
    return Path(tempfile.gettempdir()) / f"rosotacom-{uid}"


def _builtin_instances_dir() -> Path:
    return _runtime_dir() / "example" / "session-instances"


def _discover_project_config(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default cwd) for the nearest ``rosotacom.yaml``."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / "rosotacom.yaml"
        if candidate.is_file():
            return candidate
    return None


def _user_config_project() -> Path | None:
    """Return the machine-wide default project from the user config file, if set."""
    raw = _load_yaml_file(_user_config_file()).get("project")
    if not raw:
        return None
    path = Path(os.path.expandvars(os.path.expanduser(str(raw))))
    if not path.is_absolute():
        path = _user_config_file().parent / path
    path = path.resolve()
    return path if path.is_file() else None


def _builtin_example_config() -> Path | None:
    """Return the packaged example project's rosotacom.yaml, used in place.

    This is the zero-config fallback: a fresh install can run sessions with no
    setup. Nothing is copied — the config, ``sessions/`` and ``data_dict.json``
    are read-only (and mounted read-only). Only the writable runtime output is
    redirected, to tmpfs; see ``_builtin_instances_dir`` and where
    ``project_source == "built-in"`` overrides ``session_instances_dir``.
    """
    if not EXAMPLE_PROJECT_DIR.is_dir():
        return None
    config = EXAMPLE_PROJECT_DIR / "rosotacom.yaml"
    return config if config.is_file() else None


def _resolve_project_config_source(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Resolve which rosotacom.yaml is active and which scope it came from.

    Precedence (highest first), in the shared shell/local/global vocabulary:
    explicit ``--project`` flag, ``shell`` (ROSOTACOM_CONFIG env), ``local`` (a
    rosotacom.yaml discovered upward from the cwd), ``global`` (machine-wide user
    default), then the packaged ``built-in`` example.
    """
    flag = getattr(args, "rosotacom_config", None)
    if flag:
        return str(flag), "flag"
    env = os.environ.get("ROSOTACOM_CONFIG")
    if env:
        return env, "shell"
    discovered = _discover_project_config()
    if discovered:
        return str(discovered), "local"
    user = _user_config_project()
    if user:
        return str(user), "global"
    builtin = _builtin_example_config()
    if builtin:
        return str(builtin), "built-in"
    return None, None


def _load_runtime_config(args: argparse.Namespace | None = None) -> RuntimeConfig:
    args = args or argparse.Namespace()
    rosotacom_config_raw, project_source = _resolve_project_config_source(args)
    rosotacom_config = _resolve_path(rosotacom_config_raw, Path.cwd(), must_exist=True)
    config_base = rosotacom_config.parent if rosotacom_config else Path.cwd()
    cfg = _load_yaml_file(rosotacom_config)

    ros2docker_config_raw = _first_value(
        getattr(args, "ros2docker_config", None),
        os.environ.get("ROSOTACOM_ROS2DOCKER_CONFIG"),
        cfg.get("ros2docker_config"),
        str(DEFAULT_ROS2DOCKER_CONFIG),
    )
    session_configs_dir_raw = _first_value(
        getattr(args, "session_configs_dir", None),
        os.environ.get("ROSOTACOM_SESSION_CONFIGS_DIR"),
        cfg.get("session_configs_dir"),
    )
    scenario_configs_dir_raw = _first_value(
        getattr(args, "scenario_configs_dir", None),
        os.environ.get("ROSOTACOM_SCENARIO_CONFIGS_DIR"),
        cfg.get("scenario_configs_dir"),
    )
    session_instances_dir_raw = _first_value(
        getattr(args, "session_instances_dir", None),
        os.environ.get("ROSOTACOM_SESSION_INSTANCES_DIR"),
        cfg.get("session_instances_dir"),
        "session-instances",
    )
    data_dict_raw = _first_value(
        getattr(args, "data_dict", None),
        os.environ.get("ROSOTACOM_DATA_DICT"),
        cfg.get("data_dict"),
    )

    ros2docker_config = _resolve_path(ros2docker_config_raw, config_base, must_exist=True)
    if ros2docker_config is None:
        raise RuntimeError("ros2docker config could not be resolved.")

    session_instances_dir = _resolve_path(session_instances_dir_raw, config_base, must_exist=False)
    if project_source == "built-in":
        # The packaged example is read-only; redirect only its writable runtime
        # output to tmpfs so the built-in fallback never writes into $HOME.
        session_instances_dir = _builtin_instances_dir()

    return RuntimeConfig(
        rosotacom_config=rosotacom_config,
        ros2docker_config=ros2docker_config,
        session_configs_dir=_resolve_path(session_configs_dir_raw, config_base, must_exist=True),
        data_dict=_resolve_path(data_dict_raw, config_base, must_exist=True),
        install_id=_install_id(rosotacom_config),
        session_instances_dir=session_instances_dir,
        project_source=project_source,
        scenario_configs_dir=_resolve_path(scenario_configs_dir_raw, config_base, must_exist=True),
    )


def _require_ros2docker() -> None:
    if _ROS2DOCKER_IMPORT_ERROR is not None:
        raise RuntimeError(
            "Could not import ros2docker from this Python environment. "
            "Run `./install.sh` and `source .venv/bin/activate` from this checkout."
        ) from _ROS2DOCKER_IMPORT_ERROR


def _sanitize_docker_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", name)


def _scoped_image_name(runtime: RuntimeConfig) -> str:
    _require_ros2docker()
    config = load_config(runtime.ros2docker_config)
    return _scoped_image_name_from_base(str(config.get("image_name") or "ros-communication"), runtime.install_id)


def _scoped_image_name_from_base(base: str, suffix: str) -> str:
    if base.endswith(f"-{suffix}"):
        return base
    last = base.rsplit("/", 1)[-1]
    if ":" in last:
        prefix, tag = base.rsplit(":", 1)
        return f"{prefix}-{suffix}:{tag}"
    return f"{base}-{suffix}"


def _container_name(remote_peer_name: str, runtime: RuntimeConfig) -> str:
    return _sanitize_docker_name(f"rosotacom_{runtime.install_id}_com_to_{remote_peer_name}")


def _safe_path_token(value: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip())
    token = token.strip("._-")
    return token or "run"


def _new_instance_id() -> str:
    seed = f"{time.time_ns()}:{os.getpid()}:{Path.cwd()}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]


def _session_instances_root(runtime: RuntimeConfig) -> Path:
    return (runtime.session_instances_dir or (Path.cwd() / "session-instances")).resolve()


def _session_instance_slug(session: ResolvedSession, runtime: RuntimeConfig) -> str:
    if runtime.session_configs_dir:
        rel = _relative_to(session.host_dir, runtime.session_configs_dir)
        if rel is not None:
            return _safe_path_token(rel.as_posix().replace("/", "_"))
    return _safe_path_token(session.host_dir.name)


def _resolve_session_instance(
    runtime: RuntimeConfig,
    session: ResolvedSession,
    instance_id: str | None = None,
) -> SessionInstance:
    root = _session_instances_root(runtime)
    root.mkdir(parents=True, exist_ok=True)
    session_slug = _session_instance_slug(session, runtime)
    resolved_instance_id = _safe_path_token(instance_id or _new_instance_id())

    if instance_id:
        matches = sorted(root.glob(f"*/{session_slug}_*_{resolved_instance_id}"))
        if matches:
            host_dir = matches[-1].resolve()
        else:
            now = datetime.now()
            host_dir = (
                root
                / now.strftime("%Y-%m-%d")
                / (f"{session_slug}_{now.strftime('%Y-%m-%d_%H-%M-%S')}_{resolved_instance_id}")
            )
    else:
        now = datetime.now()
        host_dir = (
            root
            / now.strftime("%Y-%m-%d")
            / (f"{session_slug}_{now.strftime('%Y-%m-%d_%H-%M-%S')}_{resolved_instance_id}")
        )

    host_dir.mkdir(parents=True, exist_ok=True)
    config_host_dir = host_dir / "config"
    logs_host_dir = host_dir / "logs"
    rosbags_host_dir = host_dir / "rosbags"
    for path in (config_host_dir, logs_host_dir, rosbags_host_dir):
        path.mkdir(parents=True, exist_ok=True)

    rel = host_dir.resolve().relative_to(root.resolve())
    container_dir = f"{SESSION_INSTANCE_CONTAINER_DIR}/{rel.as_posix()}"
    return SessionInstance(
        instance_id=resolved_instance_id,
        host_dir=host_dir,
        container_dir=container_dir,
        config_host_dir=config_host_dir,
        config_container_dir=f"{container_dir}/config",
        logs_host_dir=logs_host_dir,
        logs_container_dir=f"{container_dir}/logs",
        rosbags_host_dir=rosbags_host_dir,
        rosbags_container_dir=f"{container_dir}/rosbags",
    )


def _peer_catmux_log_container_dir(instance: SessionInstance, identity: str) -> str:
    return f"{instance.logs_container_dir}/{_safe_path_token(identity)}/catmux"


def _peer_rosbag_container_dir(instance: SessionInstance, identity: str) -> str:
    return f"{instance.rosbags_container_dir}/{_safe_path_token(identity)}"


def _peer_launcher_log(instance: SessionInstance, identity: str) -> Path:
    return instance.logs_host_dir / _safe_path_token(identity) / "launcher.log"


def _peer_docker_log(instance: SessionInstance, identity: str) -> Path:
    return instance.logs_host_dir / _safe_path_token(identity) / "docker.log"


def _append_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(text)
        if not text.endswith("\n"):
            fp.write("\n")


def _effective_config_sha256(cfg: dict[str, Any]) -> str:
    text = yaml.safe_dump(cfg, sort_keys=True, default_flow_style=False, allow_unicode=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_instance_manifest(
    instance: SessionInstance,
    session: ResolvedSession,
    runtime: RuntimeConfig,
    cfg: dict[str, Any],
    *,
    identity: str,
    container_name: str,
    mode: str,
    peer_address: list[str],
    overwrite_peers_via_remote_peer: str | None,
) -> None:
    manifest_path = instance.host_dir / "manifest.yaml"
    manifest = _load_yaml_file(manifest_path)
    now = datetime.now().isoformat(timespec="seconds")
    if not manifest:
        manifest = {
            "schema_version": 1,
            "created_at": now,
            "instance_id": instance.instance_id,
            "rosotacom_version": __version__,
            "source_session_host_dir": str(session.host_dir),
            "source_session_container_dir": session.container_dir,
            "source": session.source,
            "config_dir": str(instance.config_host_dir),
            "logs_dir": str(instance.logs_host_dir),
            "rosbags_dir": str(instance.rosbags_host_dir),
            "rollout": None,
            "starts": [],
        }
    manifest["updated_at"] = now
    manifest["effective_config_sha256"] = _effective_config_sha256(cfg)
    manifest.setdefault("starts", []).append(
        {
            "started_at": now,
            "identity": identity,
            "container_name": container_name,
            "mode": mode,
            "peer_address": list(peer_address),
            "overwrite_peers_via_remote_peer": overwrite_peers_via_remote_peer,
        }
    )
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=True, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


def _write_scenario_manifest(
    instance: SessionInstance,
    session: ResolvedSession,
    runtime: RuntimeConfig,
    cfg: dict[str, Any],
    resolved: ResolvedScenario,
    *,
    identity: str,
    communication_container: str,
    applications: tuple[ScenarioApplication, ...],
    tmux_session: str,
) -> None:
    manifest_path = instance.host_dir / "manifest.yaml"
    manifest = _load_yaml_file(manifest_path)
    now = datetime.now().isoformat(timespec="seconds")
    if not manifest:
        manifest = {
            "schema_version": 1,
            "created_at": now,
            "instance_id": instance.instance_id,
            "rosotacom_version": __version__,
            "source_session_host_dir": str(session.host_dir),
            "source_session_container_dir": session.container_dir,
            "source": session.source,
            "config_dir": str(instance.config_host_dir),
            "logs_dir": str(instance.logs_host_dir),
            "rosbags_dir": str(instance.rosbags_host_dir),
            "rollout": None,
            "starts": [],
        }
    run_key = f"{resolved.name}:{identity}"
    manifest["updated_at"] = now
    manifest["effective_config_sha256"] = _effective_config_sha256(cfg)
    scenario_runs = manifest.setdefault("scenario_runs", {})
    scenario_runs[run_key] = {
        "started_at": now,
        "stopped_at": None,
        "scenario": resolved.name,
        "identity": identity,
        "source_definition": str(resolved.definition_path),
        "tmux_socket": _scenario_tmux_socket(runtime),
        "tmux_session": tmux_session,
        "communication_container": communication_container,
        "applications": [
            {
                "name": application.name,
                "ros2docker_config": str(application.ros2docker_config),
                "container_name": _scenario_container_name(runtime, resolved.name, identity, application.name),
                "image_name": _scenario_application_image_name(runtime, application),
            }
            for application in applications
        ],
    }
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=True, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


def _mark_scenario_stopped(instance_dir: Path, scenario_name: str, identity: str) -> None:
    manifest_path = instance_dir / "manifest.yaml"
    manifest = _load_yaml_file(manifest_path)
    run = (manifest.get("scenario_runs") or {}).get(f"{scenario_name}:{identity}")
    if not isinstance(run, dict):
        return
    now = datetime.now().isoformat(timespec="seconds")
    run["stopped_at"] = now
    manifest["updated_at"] = now
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=True, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


def _write_docker_log(container_name: str, instance: SessionInstance, identity: str) -> None:
    try:
        result = subprocess.run(["docker", "logs", container_name], text=True, capture_output=True, check=False)
    except Exception:
        return
    logs = (result.stdout or "") + (result.stderr or "")
    if logs:
        _append_log(_peer_docker_log(instance, identity), logs)


def _get_local_ipv4s() -> list[str]:
    try:
        out = subprocess.check_output(["ip", "-o", "-4", "addr", "show"], text=True)
    except Exception:
        return []
    return _IPV4_RE.findall(out)


def _default_local_ip() -> str:
    try:
        out = subprocess.check_output(["ip", "-4", "route", "get", "1.1.1.1"], text=True)
    except Exception:
        return "127.0.0.1"
    match = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", out)
    return match.group(1) if match else "127.0.0.1"


def _load_session_config_from_host(session_host_dir: Path) -> dict[str, Any]:
    candidates = ["session-parametrization.yaml", "session-definition.yaml"]
    for name in candidates:
        fp = session_host_dir / name
        if not fp.exists():
            continue
        param = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        if isinstance(param, dict) and "load_template" in param:
            session_template_fs, provided_params = session_gen._parse_session_config_template_spec(
                param,
                str(fp.parent),
            )
            cfg_raw = session_gen._load_yaml(session_template_fs)
            vars_map = session_gen._build_vars_map_from_template(cfg_raw, provided_params)
            return session_gen._substitute(cfg_raw, vars_map) or {}
        if not isinstance(param, dict):
            raise RuntimeError(f"Session config input YAML must be a mapping: {fp}")
        return param
    raise RuntimeError(
        f"Missing session config input file in session dir. Expected one of: {candidates}. Got dir: {session_host_dir}"
    )


def _parse_peer_address_overrides(overrides: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for override in overrides or []:
        peer_key, sep, address_value = (override or "").partition("=")
        peer_key = peer_key.strip()
        address_value = address_value.strip()
        if sep != "=" or not peer_key or not address_value:
            raise RuntimeError(
                "--peer-address must use '<peer_key>=<address_expr>', "
                "for example 'a=192.168.1.10' or 'a=data:machine_a_ip'."
            )
        if peer_key in result:
            raise RuntimeError(f"Duplicate --peer-address override for peer '{peer_key}'.")
        result[peer_key] = address_value
    return result


def _parse_remote_peer_override(override: str) -> tuple[str, str]:
    peer_key, sep, remote_address_ref = (override or "").partition("=")
    peer_key = peer_key.strip()
    remote_address_ref = remote_address_ref.strip()
    if sep != "=" or not peer_key or not remote_address_ref:
        raise RuntimeError(
            "--overwrite-peers-via-remote-peer must use '<peer_key>=<data_dict_key>', "
            "for example 'b=machine_b_ip' or 'b=data:machine_b_ip'."
        )
    remote_data_key = parse_data_reference(remote_address_ref) or remote_address_ref
    return peer_key, remote_data_key


def _load_host_data_dict(runtime: RuntimeConfig) -> dict[str, Any]:
    if not runtime.data_dict:
        raise RuntimeError(
            "This session still contains data:<key> address expressions, but no data_dict.json was configured. "
            "Use --data-dict, ROSOTACOM_DATA_DICT, or an explicit rosotacom.yaml via "
            "--rosotacom-config/ROSOTACOM_CONFIG."
        )
    loaded = load_data_dict([str(runtime.data_dict)])
    if not isinstance(loaded, dict):
        raise RuntimeError(f"data_dict must contain a JSON object: {runtime.data_dict}")
    return loaded


def _apply_remote_peer_override_to_cfg(
    cfg: dict[str, Any],
    override: str | None,
    runtime: RuntimeConfig,
    local_ips: set[str] | None = None,
) -> dict[str, Any]:
    if not override:
        return cfg
    peers = (cfg or {}).get("peers")
    if not isinstance(peers, dict) or len(peers) != 2:
        raise RuntimeError("Peer override currently requires exactly 2 peers in the session config.")

    remote_peer_key, remote_data_key = _parse_remote_peer_override(override)
    if remote_peer_key not in peers:
        raise RuntimeError(
            f"Peer override references unknown session peer '{remote_peer_key}'. Known peers: {sorted(peers.keys())}"
        )

    data_dict = _load_host_data_dict(runtime)
    group_name, remote_ip = find_data_dict_leaf(data_dict, remote_data_key)
    if not group_name:
        raise RuntimeError(
            f"Peer override key '{remote_data_key}' is not inside a data_dict group. "
            "Automatic local peer inference requires grouped entries."
        )

    group = data_dict.get(group_name)
    if not isinstance(group, dict):
        raise RuntimeError(f"data_dict group '{group_name}' must be a mapping.")

    local_ips = set(local_ips or _get_local_ipv4s())
    if not local_ips:
        raise RuntimeError("Peer override failed: could not determine local IPv4 addresses.")

    local_peer_key = next(k for k in peers.keys() if k != remote_peer_key)
    local_candidates = []
    for candidate_key, candidate_value in group.items():
        candidate_ip = str(candidate_value)
        if candidate_key == remote_data_key:
            continue
        if candidate_ip in local_ips:
            local_candidates.append((candidate_key, candidate_ip))

    if len(local_candidates) != 1:
        raise RuntimeError(
            "Peer override could not infer a unique local peer from the remote peer group. "
            f"remote={remote_data_key} group={group_name} remote_ip={remote_ip} "
            f"local_ipv4s={sorted(local_ips)} candidates={local_candidates}"
        )

    local_data_key, _local_ip = local_candidates[0]
    cfg = dict(cfg)
    cfg["peers"] = dict(peers)
    cfg["peers"][remote_peer_key] = dict(cfg["peers"][remote_peer_key])
    cfg["peers"][local_peer_key] = dict(cfg["peers"][local_peer_key])
    cfg["peers"][remote_peer_key]["address"] = format_data_reference(remote_data_key)
    cfg["peers"][local_peer_key]["address"] = format_data_reference(local_data_key)
    return cfg


def _apply_peer_address_overrides_to_cfg(cfg: dict[str, Any], peer_address_overrides: dict[str, str]) -> dict[str, Any]:
    if not peer_address_overrides:
        return cfg
    peers = (cfg or {}).get("peers")
    if not isinstance(peers, dict):
        raise RuntimeError("Peer address override requires a session config with a 'peers' mapping.")
    unknown = sorted([peer for peer in peer_address_overrides if peer not in peers])
    if unknown:
        raise RuntimeError(f"--peer-address references unknown peer(s) {unknown}. Known peers: {sorted(peers.keys())}")

    cfg = dict(cfg)
    cfg["peers"] = dict(peers)
    for peer_key, address_value in peer_address_overrides.items():
        cfg["peers"][peer_key] = dict(cfg["peers"][peer_key])
        cfg["peers"][peer_key]["address"] = address_value
    return cfg


def _effective_session_config(
    session_host_dir: Path,
    runtime: RuntimeConfig,
    *,
    overwrite_peers_via_remote_peer: str | None = None,
    peer_address_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    cfg = _load_session_config_from_host(session_host_dir)
    cfg = _apply_remote_peer_override_to_cfg(cfg, overwrite_peers_via_remote_peer, runtime)
    cfg = _apply_peer_address_overrides_to_cfg(cfg, peer_address_overrides or {})
    session_gen._validate_session_template_cfg(cfg)
    return cfg


def _contains_data_ref(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_DATA_REF_RE.search(value.strip()))
    if isinstance(value, dict):
        return any(_contains_data_ref(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_data_ref(v) for v in value)
    return False


def _resolved_address_expr_ips(address_expr: str, runtime: RuntimeConfig) -> set[str]:
    data_dict = _load_host_data_dict(runtime) if "data:" in address_expr else None
    resolved = [
        resolve_address_expression(part.strip(), data_dict=data_dict)
        for part in str(address_expr).split("+")
        if part.strip()
    ]
    ips: set[str] = set()
    for value in resolved:
        ips.update(_IPV4_RE.findall(str(value)))
    return ips


def _auto_identity(session_host_dir: Path, runtime: RuntimeConfig, cfg: dict[str, Any]) -> str:
    local_ips = set(_get_local_ipv4s())
    if not local_ips:
        raise RuntimeError("Auto identity failed: could not determine local IPv4 addresses. Use --identity.")
    peers = (cfg or {}).get("peers")
    if not isinstance(peers, dict):
        raise RuntimeError("session-config must define a mapping 'peers: { <peer_key>: { ... } }'.")

    matches = []
    for peer_key, peer_cfg in peers.items():
        address = (peer_cfg or {}).get("address")
        if address and local_ips.intersection(_resolved_address_expr_ips(str(address), runtime)):
            matches.append(peer_key)
    if len(matches) == 1:
        return str(matches[0])
    if not matches:
        raise RuntimeError(
            f"Auto identity failed: no peer address matched local IPv4s={sorted(local_ips)}. Use --identity."
        )
    raise RuntimeError(f"Auto identity ambiguous: matched peers={matches} for local IPv4s={sorted(local_ips)}.")


def _peer_com_name(peers: dict[str, Any], peer_key: str) -> str:
    value = (peers.get(peer_key) or {}).get("com-name")
    if value is None:
        return peer_key
    if isinstance(value, str):
        return value.strip() or peer_key
    return str(value) if value else peer_key


def _remote_peer_name(cfg: dict[str, Any], identity: str) -> str:
    peers = (cfg or {}).get("peers")
    if not isinstance(peers, dict):
        raise RuntimeError("session-config must define a mapping 'peers: { <peer_key>: { ... } }'.")
    peer_keys = list(peers.keys())
    if len(peer_keys) != 2:
        raise RuntimeError(f"Expected exactly 2 peers, got peers={peer_keys}")
    if identity not in peer_keys:
        raise RuntimeError(f"--identity must be one of peers={peer_keys}")
    remote_peer_key = next(k for k in peer_keys if k != identity)
    return _peer_com_name(peers, str(remote_peer_key))


def _identity_container_names(cfg: dict[str, Any], runtime: RuntimeConfig, identity: str | None = None) -> list[str]:
    peers = (cfg or {}).get("peers")
    if not isinstance(peers, dict):
        raise RuntimeError("session-config must define a mapping 'peers: { <peer_key>: { ... } }'.")
    identities = [identity] if identity else list(peers.keys())
    names = []
    for peer_key in identities:
        if peer_key not in peers:
            raise RuntimeError(f"--identity must be one of peers={list(peers.keys())}")
        names.append(_container_name(_remote_peer_name(cfg, str(peer_key)), runtime))
    return names


def _relative_to(path: Path, base: Path) -> Path | None:
    try:
        return path.resolve().relative_to(base.resolve())
    except ValueError:
        return None


def _is_session_dir(path: Path) -> bool:
    return (path / "session-parametrization.yaml").exists() or (path / "session-definition.yaml").exists()


def _resolve_session(session_dir: str, runtime: RuntimeConfig) -> ResolvedSession:
    raw = Path(os.path.expandvars(os.path.expanduser(session_dir)))

    candidates: list[tuple[Path, str]] = []
    if raw.is_absolute():
        if str(raw).startswith("/ws/"):
            candidates.append((PROJECT_DIR / str(raw).lstrip("/"), "workspace"))
        if str(raw).startswith(f"{SESSION_DEFINITION_CONTAINER_DIR}/") and runtime.session_configs_dir:
            rel = Path(str(raw)[len(SESSION_DEFINITION_CONTAINER_DIR) :].lstrip("/"))
            candidates.append((runtime.session_configs_dir / rel, "session_configs"))
        if str(raw).startswith(f"{SESSION_CONFIG_CONTAINER_DIR}/") and runtime.session_configs_dir:
            rel = Path(str(raw)[len(SESSION_CONFIG_CONTAINER_DIR) :].lstrip("/"))
            candidates.append((runtime.session_configs_dir / rel, "session_configs"))
        candidates.append((raw, "absolute"))
    else:
        candidates.append((Path.cwd() / raw, "cwd"))
        if runtime.session_configs_dir:
            candidates.append((runtime.session_configs_dir / raw, "session_configs"))

    for candidate, source in candidates:
        if candidate.is_dir() and _is_session_dir(candidate):
            host_dir = candidate.resolve()
            ws_rel = _relative_to(host_dir, WS_DIR)
            if ws_rel is not None:
                return ResolvedSession(host_dir, f"/ws/{ws_rel.as_posix()}", "workspace")
            if runtime.session_configs_dir:
                cfg_rel = _relative_to(host_dir, runtime.session_configs_dir)
                if cfg_rel is not None:
                    return ResolvedSession(
                        host_dir,
                        f"{SESSION_DEFINITION_CONTAINER_DIR}/{cfg_rel.as_posix()}",
                        "session_configs",
                    )
            return ResolvedSession(host_dir, EXTERNAL_SESSION_CONTAINER_DIR, source)

    available = _format_available_sessions(runtime)
    raise RuntimeError(f"--session-dir must be a directory, got: {session_dir}\n{available}")


def _format_available_sessions(runtime: RuntimeConfig) -> str:
    parts: list[str] = []
    if runtime.session_configs_dir and runtime.session_configs_dir.is_dir():
        sessions = [p.name for p in sorted(runtime.session_configs_dir.iterdir()) if p.is_dir()]
        if sessions:
            parts.append("Configured sessions:\n  - " + "\n  - ".join(sessions))
    if parts:
        return "\n".join(parts)
    return (
        "No configured session directories found. Create examples with "
        "`rosotacom examples create ./rosotacom_examples` and wire them with "
        '`eval "$(rosotacom setup-env ./rosotacom_examples/rosotacom.yaml)"`.'
    )


def _session_names(runtime: RuntimeConfig) -> list[str]:
    if not runtime.session_configs_dir or not runtime.session_configs_dir.is_dir():
        return []
    return [
        path.name for path in sorted(runtime.session_configs_dir.iterdir()) if path.is_dir() and _is_session_dir(path)
    ]


def _session_name_completer(
    prefix: str,
    parsed_args: argparse.Namespace,
    **_: Any,
) -> dict[str, str]:
    """Complete session names from the project selected by the arguments parsed so far."""
    completions: dict[str, str] = {}
    if prefix.startswith((".", "~", os.sep)) or os.sep in prefix:
        completions.update({path: "session directory" for path in DirectoriesCompleter()(prefix)})
    try:
        runtime = _load_runtime_config(parsed_args)
    except (FileNotFoundError, RuntimeError, OSError, yaml.YAMLError):
        return completions
    completions.update({name: "configured session" for name in _session_names(runtime) if name.startswith(prefix)})
    return completions


def _session_identities(runtime: RuntimeConfig, session_name: str) -> list[str]:
    try:
        session = _resolve_session(session_name, runtime)
        cfg = _effective_session_config(session.host_dir, runtime)
    except (FileNotFoundError, RuntimeError, OSError, yaml.YAMLError):
        return []
    peers = cfg.get("peers")
    if not isinstance(peers, dict):
        return []
    return [str(identity) for identity in peers]


def _is_scenario_dir(path: Path) -> bool:
    return (path / "scenario-definition.yaml").is_file()


def _resolve_scenario(scenario_name: str, runtime: RuntimeConfig) -> ResolvedScenario:
    raw = Path(os.path.expandvars(os.path.expanduser(scenario_name)))
    candidates: list[tuple[Path, str]] = []
    if raw.is_absolute():
        candidates.append((raw, "absolute"))
    else:
        candidates.append((Path.cwd() / raw, "cwd"))
        if runtime.scenario_configs_dir:
            candidates.append((runtime.scenario_configs_dir / raw, "scenario_configs"))

    for candidate, source in candidates:
        if candidate.is_file() and candidate.name == "scenario-definition.yaml":
            host_dir = candidate.parent.resolve()
            return ResolvedScenario(host_dir.name, host_dir, candidate.resolve(), source)
        if _is_scenario_dir(candidate):
            host_dir = candidate.resolve()
            return ResolvedScenario(host_dir.name, host_dir, host_dir / "scenario-definition.yaml", source)

    available = _format_available_scenarios(runtime)
    raise RuntimeError(
        f"scenario must be a directory containing scenario-definition.yaml, got: {scenario_name}\n{available}"
    )


def _scenario_names(runtime: RuntimeConfig) -> list[str]:
    if not runtime.scenario_configs_dir or not runtime.scenario_configs_dir.is_dir():
        return []
    return [
        path.name for path in sorted(runtime.scenario_configs_dir.iterdir()) if path.is_dir() and _is_scenario_dir(path)
    ]


def _format_available_scenarios(runtime: RuntimeConfig) -> str:
    names = _scenario_names(runtime)
    if names:
        return "Configured scenarios:\n  - " + "\n  - ".join(names)
    return "No configured scenarios found. Set scenario_configs_dir in rosotacom.yaml."


def _scenario_identities(runtime: RuntimeConfig, scenario_name: str) -> list[str]:
    try:
        resolved = _resolve_scenario(scenario_name, runtime)
        definition = _load_scenario_definition(resolved)
    except (FileNotFoundError, RuntimeError, OSError, yaml.YAMLError):
        return []
    return list(definition.applications)


def _active_scenario_runs(runtime: RuntimeConfig) -> list[ActiveScenarioRun]:
    if not shutil.which("tmux"):
        return []
    result = subprocess.run(
        _tmux_command(
            runtime,
            "list-sessions",
            "-F",
            "#{session_name}\t#{@rosotacom_scenario}\t#{@rosotacom_identity}",
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []

    configured_by_tmux_name: dict[str, tuple[str, str]] = {}
    for scenario_name in _scenario_names(runtime):
        for identity in _scenario_identities(runtime, scenario_name):
            configured_by_tmux_name[_scenario_tmux_session(scenario_name, identity)] = (scenario_name, identity)

    runs: list[ActiveScenarioRun] = []
    for line in result.stdout.splitlines():
        tmux_session, _, metadata = line.partition("\t")
        scenario, _, identity = metadata.partition("\t")
        if not scenario or not identity:
            fallback = configured_by_tmux_name.get(tmux_session)
            if fallback:
                scenario, identity = fallback
        if scenario and identity:
            runs.append(ActiveScenarioRun(scenario=scenario, identity=identity, tmux_session=tmux_session))
    return sorted(runs, key=lambda run: (run.scenario, run.identity))


def _format_active_scenario_runs(runs: list[ActiveScenarioRun]) -> str:
    if not runs:
        return "  (none)"
    return "\n".join(f"  - {run.scenario} --identity {run.identity}" for run in runs)


def _format_scenario_listing(runtime: RuntimeConfig) -> str:
    configured = _scenario_names(runtime)
    active = _active_scenario_runs(runtime)
    active_by_scenario: dict[str, list[str]] = {}
    for run in active:
        active_by_scenario.setdefault(run.scenario, []).append(run.identity)

    if configured:
        configured_lines = []
        for name in configured:
            identities = active_by_scenario.get(name, [])
            state = f"active: {', '.join(identities)}" if identities else "inactive"
            configured_lines.append(f"  - {name} ({state})")
        configured_text = "\n".join(configured_lines)
    else:
        configured_text = "  (none)"
    return f"Configured scenarios:\n{configured_text}\nActive scenarios:\n{_format_active_scenario_runs(active)}"


def _scenario_name_completer(
    prefix: str,
    parsed_args: argparse.Namespace,
    **_: Any,
) -> dict[str, str]:
    completions: dict[str, str] = {}
    if prefix.startswith((".", "~", os.sep)) or os.sep in prefix:
        completions.update({path: "scenario directory" for path in DirectoriesCompleter()(prefix)})
    try:
        runtime = _load_runtime_config(parsed_args)
    except (FileNotFoundError, RuntimeError, OSError, yaml.YAMLError):
        return completions
    completions.update({name: "configured scenario" for name in _scenario_names(runtime) if name.startswith(prefix)})
    return completions


def _active_scenario_name_completer(
    prefix: str,
    parsed_args: argparse.Namespace,
    **_: Any,
) -> dict[str, str]:
    try:
        runtime = _load_runtime_config(parsed_args)
    except (FileNotFoundError, RuntimeError, OSError, yaml.YAMLError):
        return {}
    names = sorted({run.scenario for run in _active_scenario_runs(runtime)})
    return {name: "active scenario" for name in names if name.startswith(prefix)}


def _identity_completer(
    prefix: str,
    parsed_args: argparse.Namespace,
    **_: Any,
) -> dict[str, str]:
    try:
        runtime = _load_runtime_config(parsed_args)
    except (FileNotFoundError, RuntimeError, OSError, yaml.YAMLError):
        return {}

    if getattr(parsed_args, "command", None) == "scenario":
        scenario_name = getattr(parsed_args, "scenario", None)
        if getattr(parsed_args, "scenario_command", None) in {"attach", "stop"}:
            identities = sorted(
                {
                    run.identity
                    for run in _active_scenario_runs(runtime)
                    if not scenario_name or run.scenario == scenario_name
                }
            )
        elif scenario_name:
            identities = _scenario_identities(runtime, scenario_name)
        else:
            identities = []
    else:
        session_name = getattr(parsed_args, "session_dir", None) or getattr(parsed_args, "session_dir_positional", None)
        identities = _session_identities(runtime, session_name) if session_name else []
    return {identity: "peer identity" for identity in identities if identity.startswith(prefix)}


def _load_scenario_definition(resolved: ResolvedScenario) -> ScenarioDefinition:
    raw = _load_yaml_file(resolved.definition_path)
    allowed_root = {"schema_version", "session", "applications"}
    extra_root = sorted(set(raw) - allowed_root)
    if extra_root:
        raise RuntimeError(
            f"Unsupported keys in scenario-definition root: {extra_root}. Allowed keys: {sorted(allowed_root)}"
        )
    if raw.get("schema_version") != 1:
        raise RuntimeError("scenario-definition.yaml requires schema_version: 1.")

    session = raw.get("session")
    if not isinstance(session, str) or not session.strip():
        raise RuntimeError("scenario-definition.yaml requires a non-empty string 'session'.")

    raw_applications = raw.get("applications")
    if not isinstance(raw_applications, dict) or not raw_applications:
        raise RuntimeError("scenario-definition.yaml requires a non-empty 'applications' mapping.")

    applications: dict[str, tuple[ScenarioApplication, ...]] = {}
    for identity, entries in raw_applications.items():
        if not isinstance(identity, str) or not identity.strip():
            raise RuntimeError("scenario application identity keys must be non-empty strings.")
        if not isinstance(entries, list):
            raise RuntimeError(f"scenario applications.{identity} must be a list.")
        seen_names: set[str] = set()
        parsed_entries: list[ScenarioApplication] = []
        for index, entry in enumerate(entries):
            context = f"scenario applications.{identity}[{index}]"
            if not isinstance(entry, dict):
                raise RuntimeError(f"{context} must be a mapping.")
            allowed_entry = {"name", "ros2docker_config"}
            extra_entry = sorted(set(entry) - allowed_entry)
            if extra_entry:
                raise RuntimeError(
                    f"Unsupported keys in {context}: {extra_entry}. Allowed keys: {sorted(allowed_entry)}"
                )
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                raise RuntimeError(f"{context}.name must be a non-empty string.")
            name = name.strip()
            if name in seen_names:
                raise RuntimeError(f"Duplicate application name '{name}' for identity '{identity}'.")
            config_raw = entry.get("ros2docker_config")
            if not isinstance(config_raw, str) or not config_raw.strip():
                raise RuntimeError(f"{context}.ros2docker_config must be a non-empty string.")
            config_path = _resolve_path(config_raw, resolved.host_dir, must_exist=True)
            assert config_path is not None
            if not config_path.is_file():
                raise RuntimeError(f"{context}.ros2docker_config must resolve to a file: {config_path}")
            load_config(config_path)
            seen_names.add(name)
            parsed_entries.append(ScenarioApplication(name=name, ros2docker_config=config_path))
        applications[identity] = tuple(parsed_entries)

    return ScenarioDefinition(session=session.strip(), applications=applications)


def _scenario_application(
    definition: ScenarioDefinition,
    identity: str,
    application_name: str,
) -> ScenarioApplication:
    for application in definition.applications.get(identity, ()):
        if application.name == application_name:
            return application
    raise RuntimeError(f"Unknown scenario application '{application_name}' for identity '{identity}'.")


def _scenario_container_name(
    runtime: RuntimeConfig,
    scenario_name: str,
    identity: str,
    application_name: str,
) -> str:
    base = _sanitize_docker_name(
        f"rosotacom_{runtime.install_id}_scenario_{scenario_name}_{identity}_{application_name}"
    )
    if len(base) <= 120:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    return f"{base[:109]}_{digest}"


def _scenario_application_image_name(runtime: RuntimeConfig, application: ScenarioApplication) -> str:
    config = load_config(application.ros2docker_config, resolve_run_args=False)
    base = str(config.get("image_name") or "ros2docker")
    return _scoped_image_name_from_base(base, runtime.install_id)


def _scenario_tmux_socket(runtime: RuntimeConfig) -> str:
    return f"rosotacom-{runtime.install_id}"


def _scenario_tmux_session(scenario_name: str, identity: str) -> str:
    return _safe_path_token(f"{scenario_name}-{identity}")


def _tmux_command(runtime: RuntimeConfig, *args: str) -> list[str]:
    return ["tmux", "-L", _scenario_tmux_socket(runtime), *args]


def _require_tmux() -> None:
    if not shutil.which("tmux"):
        raise RuntimeError("Scenario orchestration requires host tmux. Install tmux and retry.")


def _tmux_session_exists(runtime: RuntimeConfig, session_name: str) -> bool:
    if not shutil.which("tmux"):
        return False
    result = subprocess.run(
        _tmux_command(runtime, "has-session", "-t", session_name),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _runtime_cli_args(runtime: RuntimeConfig) -> list[str]:
    args: list[str] = []
    if runtime.rosotacom_config:
        args.extend(["--rosotacom-config", str(runtime.rosotacom_config)])
    args.extend(["--ros2docker-config", str(runtime.ros2docker_config)])
    if runtime.session_configs_dir:
        args.extend(["--session-configs-dir", str(runtime.session_configs_dir)])
    if runtime.scenario_configs_dir:
        args.extend(["--scenario-configs-dir", str(runtime.scenario_configs_dir)])
    if runtime.session_instances_dir:
        args.extend(["--session-instances-dir", str(runtime.session_instances_dir)])
    if runtime.data_dict:
        args.extend(["--data-dict", str(runtime.data_dict)])
    return args


def _scenario_communication_command(
    runtime: RuntimeConfig,
    definition: ScenarioDefinition,
    identity: str,
    instance_id: str,
    args: argparse.Namespace,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "rosotacom",
        "start",
        definition.session,
        "--identity",
        identity,
        "--mode",
        "attach",
        "--instance-id",
        instance_id,
        "--scenario-managed",
        *_runtime_cli_args(runtime),
    ]
    command.append("--force" if getattr(args, "force", True) else "--no-force")
    if getattr(args, "rewrite_formatting", False):
        command.append("--rewrite-formatting")
    remote_override = getattr(args, "overwrite_peers_via_remote_peer", None)
    if remote_override:
        command.extend(["--overwrite-peers-via-remote-peer", remote_override])
    for override in getattr(args, "peer_address", []) or []:
        command.extend(["--peer-address", override])
    return command


def _scenario_application_command(
    runtime: RuntimeConfig,
    resolved: ResolvedScenario,
    identity: str,
    application: ScenarioApplication,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "rosotacom",
        "scenario",
        "_run-application",
        resolved.name,
        "--identity",
        identity,
        "--application",
        application.name,
        *_runtime_cli_args(runtime),
    ]


def _scenario_log_path(instance: SessionInstance, identity: str, component: str) -> Path:
    return instance.logs_host_dir / _safe_path_token(identity) / "scenario" / f"{_safe_path_token(component)}.log"


def _attach_tmux_pipe(runtime: RuntimeConfig, pane_id: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        _tmux_command(runtime, "pipe-pane", "-o", "-t", pane_id, f"cat >> {shlex.quote(str(log_path))}"),
        check=True,
    )


def _create_scenario_tmux(
    runtime: RuntimeConfig,
    resolved: ResolvedScenario,
    definition: ScenarioDefinition,
    instance: SessionInstance,
    identity: str,
    applications: tuple[ScenarioApplication, ...],
    args: argparse.Namespace,
) -> str:
    session_name = _scenario_tmux_session(resolved.name, identity)
    communication_command = shlex.join(
        _scenario_communication_command(runtime, definition, identity, instance.instance_id, args)
    )
    created = subprocess.run(
        _tmux_command(
            runtime,
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-s",
            session_name,
            "-n",
            "communication",
            communication_command,
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    communication_pane = created.stdout.strip()
    subprocess.run(
        _tmux_command(runtime, "set-window-option", "-g", "-t", session_name, "remain-on-exit", "on"),
        check=True,
    )
    subprocess.run(_tmux_command(runtime, "set-option", "-t", session_name, "prefix", "C-b"), check=True)
    subprocess.run(
        _tmux_command(runtime, "set-option", "-t", session_name, "@rosotacom_scenario", resolved.name),
        check=True,
    )
    subprocess.run(
        _tmux_command(runtime, "set-option", "-t", session_name, "@rosotacom_identity", identity),
        check=True,
    )
    subprocess.run(_tmux_command(runtime, "bind-key", "-T", "prefix", "C-b", "send-prefix"), check=True)
    subprocess.run(
        _tmux_command(runtime, "set-option", "-t", session_name, "pane-border-status", "top"),
        check=True,
    )
    subprocess.run(
        _tmux_command(
            runtime,
            "set-option",
            "-t",
            session_name,
            "pane-border-format",
            " #{pane_title} ",
        ),
        check=True,
    )
    subprocess.run(
        _tmux_command(
            runtime,
            "set-option",
            "-t",
            session_name,
            "status-right",
            " windows: C-b n/p | inner catmux: C-b C-b ",
        ),
        check=True,
    )
    subprocess.run(
        _tmux_command(runtime, "select-pane", "-t", communication_pane, "-T", "communication"),
        check=True,
    )
    _attach_tmux_pipe(
        runtime,
        communication_pane,
        _scenario_log_path(instance, identity, "communication"),
    )

    for application in applications:
        created_window = subprocess.run(
            _tmux_command(
                runtime,
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                session_name,
                "-n",
                _safe_path_token(application.name),
                shlex.join(_scenario_application_command(runtime, resolved, identity, application)),
            ),
            text=True,
            capture_output=True,
            check=True,
        )
        pane_id = created_window.stdout.strip()
        subprocess.run(
            _tmux_command(runtime, "select-pane", "-t", pane_id, "-T", f"application:{application.name}"),
            check=True,
        )
        _attach_tmux_pipe(
            runtime,
            pane_id,
            _scenario_log_path(instance, identity, f"application-{application.name}"),
        )

    subprocess.run(_tmux_command(runtime, "select-window", "-t", f"{session_name}:communication"), check=True)
    return session_name


def _base_extra_run_args(
    runtime: RuntimeConfig,
    session: ResolvedSession,
    cfg: dict[str, Any],
    instance: SessionInstance,
) -> list[str]:
    args: list[str] = []
    ros2docker_cfg = load_config(runtime.ros2docker_config)

    if not ros2docker_cfg.get("mount_ws"):
        args.extend(["-v", f"{WS_DIR.resolve()}:/ws", "-w", "/ws"])
    if runtime.session_configs_dir:
        args.extend(
            [
                "-v",
                f"{runtime.session_configs_dir}:{SESSION_DEFINITION_CONTAINER_DIR}:ro",
                "-e",
                f"SESSION_DEFINITIONS_DIR={SESSION_DEFINITION_CONTAINER_DIR}",
                "-e",
                f"SESSION_CONFIGS_DIR={SESSION_DEFINITION_CONTAINER_DIR}",
            ]
        )
    if session.container_dir == EXTERNAL_SESSION_CONTAINER_DIR:
        args.extend(["-v", f"{session.host_dir}:{EXTERNAL_SESSION_CONTAINER_DIR}:ro"])
    instances_root = _session_instances_root(runtime)
    instances_root.mkdir(parents=True, exist_ok=True)
    args.extend(
        [
            "-v",
            f"{instances_root}:{SESSION_INSTANCE_CONTAINER_DIR}",
            "-e",
            f"ROSOTACOM_SESSION_INSTANCES_DIR={SESSION_INSTANCE_CONTAINER_DIR}",
            "-e",
            f"ROSOTACOM_INSTANCE_DIR={instance.container_dir}",
            "-e",
            f"ROSOTACOM_CONFIG_DIR={instance.config_container_dir}",
            "-e",
            f"ROSOTACOM_LOGS_DIR={instance.logs_container_dir}",
            "-e",
            f"ROSOTACOM_ROSBAGS_DIR={instance.rosbags_container_dir}",
        ]
    )
    if _contains_data_ref(cfg):
        data_dict = _load_host_data_dict(runtime)
        if not isinstance(data_dict, dict):
            raise RuntimeError("data_dict.json must contain a JSON object.")
        args.extend(["-v", f"{runtime.data_dict}:{CONTAINER_DATA_DICT_PATH}:ro"])
    return args


def _session_command(
    session: ResolvedSession,
    instance: SessionInstance,
    identity: str,
    *,
    force: bool,
    rewrite_formatting: bool,
    overwrite_peers_via_remote_peer: str | None,
    peer_address_overrides: dict[str, str],
    attach_mode: str,
) -> list[str]:
    parts = [
        RUN_SESSION_CONTAINER_PATH,
        "--session-dir",
        session.container_dir,
        "--output-dir",
        instance.config_container_dir,
        "--instance-dir",
        instance.container_dir,
        "--catmux-log-dir",
        _peer_catmux_log_container_dir(instance, identity),
        "--rosbag-dir",
        _peer_rosbag_container_dir(instance, identity),
        "--identity",
        identity,
    ]
    if force:
        parts.append("--force")
    if rewrite_formatting:
        parts.append("--rewrite-formatting")
    if overwrite_peers_via_remote_peer:
        parts.extend(["--overwrite-peers-via-remote-peer", overwrite_peers_via_remote_peer])
    for peer_key, address_value in peer_address_overrides.items():
        parts.extend(["--peer-address", f"{peer_key}={address_value}"])
    if attach_mode == "attach":
        parts.append("--attach")
    elif attach_mode == "detached":
        parts.append("--detach")
    return parts


def _resolve_mode(mode: str) -> str:
    if mode != "auto":
        return mode
    return "attach" if sys.stdin.isatty() and sys.stdout.isatty() else "detached"


def _container_exists(container_name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "--type", "container", container_name],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _stop_container_name(container_name: str, runtime: RuntimeConfig, *, quiet_missing: bool = False) -> bool:
    _require_ros2docker()
    if not _container_exists(container_name):
        if not quiet_missing:
            print(f"Container not found: {container_name}")
        return False
    ros2docker_stop(config_file=runtime.ros2docker_config, override={"container_name": container_name})
    return True


def _wait_for_container_ready(container_name: str, timeout_s: int = 240) -> None:
    deadline = time.time() + timeout_s
    marker = "Sourced ROS 2 workspace overlay"
    while time.time() < deadline:
        result = subprocess.run(["docker", "logs", container_name], text=True, capture_output=True, check=False)
        logs = (result.stdout or "") + (result.stderr or "")
        if marker in logs:
            return
        inspect = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            text=True,
            capture_output=True,
            check=False,
        )
        if inspect.returncode == 0 and inspect.stdout.strip() != "true":
            raise RuntimeError(f"Container exited before it became ready: {container_name}")
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for container readiness: {container_name}")


def start_session(args: argparse.Namespace) -> str:
    _require_ros2docker()
    runtime = _load_runtime_config(args)
    session = _resolve_session(args.session_dir, runtime)
    peer_overrides = _parse_peer_address_overrides(args.peer_address)
    cfg = _effective_session_config(
        session.host_dir,
        runtime,
        overwrite_peers_via_remote_peer=args.overwrite_peers_via_remote_peer,
        peer_address_overrides=peer_overrides,
    )
    if not args.identity and not getattr(args, "auto_identity", True):
        raise RuntimeError("Missing --identity. Provide --identity <peer> or allow auto identity.")
    identity = args.identity or _auto_identity(session.host_dir, runtime, cfg)
    if not args.identity:
        print(f"Auto-selected identity: {identity}")
    remote_name = _remote_peer_name(cfg, identity)
    container_name = _container_name(remote_name, runtime)
    instance = _resolve_session_instance(runtime, session, getattr(args, "instance_id", None))
    image_name = _scoped_image_name(runtime)
    extra_run_args = _base_extra_run_args(runtime, session, cfg, instance)
    run_override: dict[str, object] = {}
    network_name = getattr(args, "network_name", None)
    if network_name:
        base_run_args = load_config(runtime.ros2docker_config).get("run_args", []) or []
        run_override["run_args"] = _isolated_network_run_args(
            [str(arg) for arg in base_run_args],
            network_name,
            getattr(args, "network_ip", None),
        )
    mode = _resolve_mode(args.mode)
    command = _session_command(
        session,
        instance,
        identity,
        force=args.force,
        rewrite_formatting=args.rewrite_formatting,
        overwrite_peers_via_remote_peer=args.overwrite_peers_via_remote_peer,
        peer_address_overrides=peer_overrides,
        attach_mode=mode,
    )

    print("Command which will be run in container: " + shlex.join(command))
    print(f"rosotacom session instance: {instance.host_dir}")
    _append_log(
        _peer_launcher_log(instance, identity),
        "\n".join(
            [
                f"started_at: {datetime.now().isoformat(timespec='seconds')}",
                f"container_name: {container_name}",
                f"mode: {mode}",
                "command: " + shlex.join(command),
                "",
            ]
        ),
    )
    if not getattr(args, "scenario_managed", False):
        _write_instance_manifest(
            instance,
            session,
            runtime,
            cfg,
            identity=identity,
            container_name=container_name,
            mode=mode,
            peer_address=list(args.peer_address or []),
            overwrite_peers_via_remote_peer=args.overwrite_peers_via_remote_peer,
        )
    if args.force:
        _stop_container_name(container_name, runtime, quiet_missing=True)

    common_override = {"container_name": container_name, "image_name": image_name}
    if mode == "detached":
        ros2docker_build(config_file=runtime.ros2docker_config, override=common_override)
        ros2docker_run(
            config_file=runtime.ros2docker_config,
            override={**common_override, "run_type": "up", **run_override},
            extra_run_args=extra_run_args,
        )
        _wait_for_container_ready(container_name)
        ros2docker_exec(
            config_file=runtime.ros2docker_config,
            override=common_override,
            command=command,
            interactive=False,
        )
        _write_docker_log(container_name, instance, identity)
    else:
        ros2docker_build(config_file=runtime.ros2docker_config, override=common_override)
        ros2docker_run(
            config_file=runtime.ros2docker_config,
            override={
                **common_override,
                "run_type": "command",
                "command": command,
                "tty": True,
                "stdin_open": True,
                **run_override,
            },
            extra_run_args=extra_run_args,
        )
        _write_docker_log(container_name, instance, identity)

    print(f"rosotacom session started in container: {container_name}")
    return container_name


def stop_session(args: argparse.Namespace) -> None:
    runtime = _load_runtime_config(args)
    session = _resolve_session(args.session_dir, runtime)
    peer_overrides = _parse_peer_address_overrides(getattr(args, "peer_address", None))
    cfg = _effective_session_config(
        session.host_dir,
        runtime,
        overwrite_peers_via_remote_peer=getattr(args, "overwrite_peers_via_remote_peer", None),
        peer_address_overrides=peer_overrides,
    )
    identity = args.identity
    if not identity and getattr(args, "auto_identity", False):
        identity = _auto_identity(session.host_dir, runtime, cfg)
        print(f"Auto-selected identity: {identity}")
    for container_name in _identity_container_names(cfg, runtime, identity):
        _stop_container_name(container_name, runtime)


def _resolve_scenario_context(
    args: argparse.Namespace,
) -> tuple[
    RuntimeConfig,
    ResolvedScenario,
    ScenarioDefinition,
    ResolvedSession,
    dict[str, Any],
    str,
    tuple[ScenarioApplication, ...],
]:
    runtime = _load_runtime_config(args)
    resolved = _resolve_scenario(args.scenario, runtime)
    definition = _load_scenario_definition(resolved)
    session = _resolve_session(definition.session, runtime)
    peer_overrides = _parse_peer_address_overrides(getattr(args, "peer_address", None))
    cfg = _effective_session_config(
        session.host_dir,
        runtime,
        overwrite_peers_via_remote_peer=getattr(args, "overwrite_peers_via_remote_peer", None),
        peer_address_overrides=peer_overrides,
    )
    peers = cfg.get("peers")
    if not isinstance(peers, dict):
        raise RuntimeError("Scenario session must define a peers mapping.")
    unknown_identities = sorted(set(definition.applications) - set(peers))
    if unknown_identities:
        raise RuntimeError(
            f"Scenario defines applications for unknown session identities {unknown_identities}. "
            f"Known identities: {sorted(peers)}"
        )
    identity = getattr(args, "identity", None)
    if not identity and getattr(args, "auto_identity", True):
        identity = _auto_identity(session.host_dir, runtime, cfg)
        print(f"Auto-selected identity: {identity}")
    if not identity:
        raise RuntimeError("Missing --identity. Provide --identity <peer> or allow auto identity.")
    if identity not in peers:
        raise RuntimeError(f"--identity must be one of peers={list(peers.keys())}")
    applications = definition.applications.get(identity)
    if applications is None:
        raise RuntimeError(f"Scenario '{resolved.name}' has no applications entry for identity '{identity}'.")
    return runtime, resolved, definition, session, cfg, identity, applications


def _infer_active_scenario_selector(args: argparse.Namespace, *, require_active: bool) -> None:
    runtime = _load_runtime_config(args)
    runs = _active_scenario_runs(runtime)
    scenario_name = getattr(args, "scenario", None)
    identity = getattr(args, "identity", None)

    if not scenario_name:
        eligible_runs = [run for run in runs if not identity or run.identity == identity]
        scenario_names = sorted({run.scenario for run in eligible_runs})
        if len(scenario_names) == 1:
            scenario_name = scenario_names[0]
            args.scenario = scenario_name
            print(f"Auto-selected active scenario: {scenario_name}")
        elif not scenario_names:
            raise RuntimeError("No active scenarios found. Start one with `rosotacom scenario start <name>`.")
        else:
            raise RuntimeError(
                "Multiple active scenarios found; specify one:\n" + _format_active_scenario_runs(eligible_runs)
            )

    matching = [run for run in runs if run.scenario == scenario_name]
    if not identity:
        identities = sorted({run.identity for run in matching})
        if len(identities) == 1:
            identity = identities[0]
            args.identity = identity
            print(f"Auto-selected active identity: {identity}")
        elif not identities:
            raise RuntimeError(f"No active run found for scenario '{scenario_name}'.")
        else:
            raise RuntimeError(
                f"Scenario '{scenario_name}' has multiple active identities; specify --identity:\n"
                + _format_active_scenario_runs(matching)
            )

    if require_active and not any(run.scenario == scenario_name and run.identity == identity for run in matching):
        raise RuntimeError(f"Scenario '{scenario_name}' is not active for identity '{identity}'.")


def _stop_scenario_application(
    runtime: RuntimeConfig,
    resolved: ResolvedScenario,
    identity: str,
    application: ScenarioApplication,
) -> bool:
    container_name = _scenario_container_name(runtime, resolved.name, identity, application.name)
    if not _container_exists(container_name):
        return False
    ros2docker_stop(
        config_file=application.ros2docker_config,
        override={"container_name": container_name},
    )
    return True


def _kill_scenario_tmux(runtime: RuntimeConfig, session_name: str) -> bool:
    if not _tmux_session_exists(runtime, session_name):
        return False
    subprocess.run(_tmux_command(runtime, "kill-session", "-t", session_name), check=True)
    return True


def _find_latest_scenario_instance(
    runtime: RuntimeConfig,
    scenario_name: str,
    identity: str,
    instance_id: str | None = None,
) -> Path | None:
    root = _session_instances_root(runtime)
    candidates = sorted(root.glob("*/*/manifest.yaml"), reverse=True)
    for manifest_path in candidates:
        manifest = _load_yaml_file(manifest_path)
        if instance_id and manifest.get("instance_id") != _safe_path_token(instance_id):
            continue
        scenario_runs = manifest.get("scenario_runs") or {}
        if f"{scenario_name}:{identity}" in scenario_runs:
            return manifest_path.parent.resolve()
    return None


def _stop_scenario_components(
    runtime: RuntimeConfig,
    resolved: ResolvedScenario,
    cfg: dict[str, Any],
    identity: str,
    applications: tuple[ScenarioApplication, ...],
    *,
    quiet_missing: bool,
) -> None:
    for application in applications:
        stopped = _stop_scenario_application(runtime, resolved, identity, application)
        if stopped:
            print(f"Stopped scenario application: {application.name}")
        elif not quiet_missing:
            print(
                "Application container not found: "
                + _scenario_container_name(runtime, resolved.name, identity, application.name)
            )
    communication_container = _container_name(_remote_peer_name(cfg, identity), runtime)
    _stop_container_name(communication_container, runtime, quiet_missing=quiet_missing)
    if _kill_scenario_tmux(runtime, _scenario_tmux_session(resolved.name, identity)):
        print(f"Stopped scenario tmux session: {_scenario_tmux_session(resolved.name, identity)}")


def start_scenario(args: argparse.Namespace) -> int:
    _require_ros2docker()
    _require_tmux()
    runtime, resolved, definition, session, cfg, identity, applications = _resolve_scenario_context(args)
    tmux_session = _scenario_tmux_session(resolved.name, identity)
    if _tmux_session_exists(runtime, tmux_session):
        if not getattr(args, "force", True):
            raise RuntimeError(f"Scenario tmux session already exists: {tmux_session}. Use --force or attach to it.")
        _stop_scenario_components(
            runtime,
            resolved,
            cfg,
            identity,
            applications,
            quiet_missing=True,
        )

    instance = _resolve_session_instance(runtime, session, getattr(args, "instance_id", None))
    communication_container = _container_name(_remote_peer_name(cfg, identity), runtime)
    _write_scenario_manifest(
        instance,
        session,
        runtime,
        cfg,
        resolved,
        identity=identity,
        communication_container=communication_container,
        applications=applications,
        tmux_session=tmux_session,
    )
    created_session = _create_scenario_tmux(
        runtime,
        resolved,
        definition,
        instance,
        identity,
        applications,
        args,
    )
    print(f"rosotacom scenario instance: {instance.host_dir}")
    print(f"rosotacom scenario started: {resolved.name} ({identity})")
    print("Outer tmux prefix: Ctrl-b; send the inner catmux prefix with Ctrl-b Ctrl-b.")
    mode = _resolve_mode(getattr(args, "mode", "auto"))
    if mode == "attach":
        subprocess.run(_tmux_command(runtime, "attach-session", "-t", created_session), check=True)
    else:
        print("Attach with: rosotacom scenario attach")
    return 0


def attach_scenario(args: argparse.Namespace) -> int:
    _require_tmux()
    _infer_active_scenario_selector(args, require_active=True)
    runtime, resolved, _definition, _session, _cfg, identity, _applications = _resolve_scenario_context(args)
    tmux_session = _scenario_tmux_session(resolved.name, identity)
    if not _tmux_session_exists(runtime, tmux_session):
        raise RuntimeError(f"Scenario tmux session is not running: {tmux_session}")
    subprocess.run(_tmux_command(runtime, "attach-session", "-t", tmux_session), check=True)
    return 0


def stop_scenario(args: argparse.Namespace) -> int:
    _require_ros2docker()
    if not getattr(args, "scenario", None) or not getattr(args, "identity", None):
        _infer_active_scenario_selector(args, require_active=False)
    runtime, resolved, _definition, _session, cfg, identity, applications = _resolve_scenario_context(args)
    _stop_scenario_components(
        runtime,
        resolved,
        cfg,
        identity,
        applications,
        quiet_missing=False,
    )
    instance_dir = _find_latest_scenario_instance(
        runtime,
        resolved.name,
        identity,
        getattr(args, "instance_id", None),
    )
    if instance_dir:
        _mark_scenario_stopped(instance_dir, resolved.name, identity)
    return 0


def run_scenario_application(args: argparse.Namespace) -> int:
    _require_ros2docker()
    runtime = _load_runtime_config(args)
    resolved = _resolve_scenario(args.scenario, runtime)
    definition = _load_scenario_definition(resolved)
    application = _scenario_application(definition, args.identity, args.application)
    container_name = _scenario_container_name(runtime, resolved.name, args.identity, application.name)
    if _container_exists(container_name):
        ros2docker_stop(
            config_file=application.ros2docker_config,
            override={"container_name": container_name},
        )
    override = {
        "container_name": container_name,
        "image_name": _scenario_application_image_name(runtime, application),
    }
    ros2docker_build(config_file=application.ros2docker_config, override=override)
    ros2docker_run(config_file=application.ros2docker_config, override=override)
    return 0


def list_scenarios(args: argparse.Namespace) -> int:
    runtime = _load_runtime_config(args)
    print(_format_scenario_listing(runtime))
    return 0


def list_sessions(args: argparse.Namespace) -> None:
    runtime = _load_runtime_config(args)
    print(_format_available_sessions(runtime))


def _example_project_resource() -> Any:
    if EXAMPLE_PROJECT_DIR.is_dir():
        return EXAMPLE_PROJECT_DIR
    try:
        return importlib_resources.files("rosotacom").joinpath("resources").joinpath("examples")
    except ModuleNotFoundError as exc:
        raise RuntimeError("Packaged rosotacom examples are not available in this installation.") from exc


def _skip_example_resource(name: str) -> bool:
    return name == "__pycache__" or name == "__init__.py" or name.endswith(".pyc")


def _copy_example_resource_tree(source: Any, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        if _skip_example_resource(child.name):
            continue
        destination = target / child.name
        if child.is_dir():
            _copy_example_resource_tree(child, destination)
            continue

        if isinstance(child, Path):
            shutil.copy2(child, destination)
        else:
            destination.write_bytes(child.read_bytes())
        if destination.suffix in {".sh", ".py"}:
            destination.chmod(destination.stat().st_mode | 0o111)


def _ensure_example_gitignore(target: Path) -> None:
    gitignore = target / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    lines = existing.splitlines()
    if "session-instances/" not in lines:
        lines.append("session-instances/")
    gitignore.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def examples_create_command(args: argparse.Namespace) -> int:
    target = Path(os.path.expandvars(os.path.expanduser(args.target))).resolve()
    if target.exists():
        if not args.force:
            raise RuntimeError(f"Target already exists: {target}. Use --force to replace it.")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    _copy_example_resource_tree(_example_project_resource(), target)
    _ensure_example_gitignore(target)
    print(f"Copied rosotacom examples to: {target}")
    print(f"Use it by entering the directory (auto-discovered):  cd {shlex.quote(str(target))}")
    print(f"Or pin it everywhere:  rosotacom config set project {shlex.quote(str(target / 'rosotacom.yaml'))} --global")
    return 0


def _require_project_file(raw: str | None) -> Path:
    config = _resolve_path(raw, Path.cwd(), must_exist=True)
    if not config or not config.is_file():
        raise RuntimeError(f"rosotacom config must be a file: {raw}")
    return config


def _print_project_export(config: Path) -> None:
    print(f"export ROSOTACOM_CONFIG={shlex.quote(str(config))}")


def setup_env_command(args: argparse.Namespace) -> int:
    _print_project_export(_require_project_file(args.rosotacom_config))
    return 0


def _write_user_project(config: Path) -> Path:
    """Persist the machine-wide default project to the user config file."""
    path = _user_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = _load_yaml_file(path)
    cfg["project"] = str(config)
    path.write_text(yaml.safe_dump(cfg, default_flow_style=False, sort_keys=True), encoding="utf-8")
    return path


def config_command(args: argparse.Namespace) -> int:
    action = args.config_action
    if action == "set":
        if args.key != "project":
            raise RuntimeError(f"unknown config key: {args.key}")
        config = _require_project_file(args.value)
        # --global persists machine-wide; --shell (default) emits an export to eval
        # in the current terminal. A child process cannot mutate the parent shell,
        # so the per-terminal path stays an `eval "$(...)"` escape hatch. The third
        # scope, "local", needs no command: just keep a rosotacom.yaml in the dir.
        if args.scope == "global":
            path = _write_user_project(config)
            print(f"Set global default project: {config}")
            print(f"  written to {path}")
        else:
            _print_project_export(config)
        return 0

    if action == "unset":
        path = _user_config_file()
        cfg = _load_yaml_file(path)
        if cfg.pop("project", None) is None:
            print("No global default project was set.")
            return 0
        if cfg:
            path.write_text(yaml.safe_dump(cfg, default_flow_style=False, sort_keys=True), encoding="utf-8")
        else:
            path.unlink()
        print("Cleared global default project.")
        return 0

    # action in {"get", "show"}
    raw, source = _resolve_project_config_source(args)
    if action == "get":
        print(str(_resolve_path(raw, Path.cwd(), must_exist=True)) if raw else "")
        return 0

    def line(label: str, value: str) -> None:
        print(f"{label:>9}: {value}")

    line("flag", getattr(args, "rosotacom_config", None) or "-")
    line("shell", os.environ.get("ROSOTACOM_CONFIG") or "-")
    line("local", str(_discover_project_config() or "-"))
    line("global", str(_user_config_project() or "-"))
    line("built-in", str(_builtin_example_config() or "-"))
    if raw:
        active = _resolve_path(raw, Path.cwd(), must_exist=True)
        line("active", f"{active}  (scope: {source})")
    else:
        line("active", "none configured")
    return 0


def doctor(args: argparse.Namespace) -> int:
    runtime = None
    failures = 0

    def line(status: str, name: str, detail: str) -> None:
        nonlocal failures
        if status == "ERROR":
            failures += 1
        print(f"{status}: {name}: {detail}")

    line("OK", "checkout", str(PROJECT_DIR))
    line("OK", "python", sys.executable)
    if _ROS2DOCKER_IMPORT_ERROR:
        line("ERROR", "ros2docker import", str(_ROS2DOCKER_IMPORT_ERROR))
    else:
        assert ros2docker is not None
        ros2docker_file = ros2docker.__file__ or "<unknown>"
        line(
            "OK",
            "ros2docker",
            f"{getattr(ros2docker, '__version__', 'unknown')} from {Path(ros2docker_file).resolve()}",
        )

    try:
        runtime = _load_runtime_config(args)
        if runtime.rosotacom_config:
            line("OK", "rosotacom config", f"{runtime.rosotacom_config} (source: {runtime.project_source})")
        else:
            line("INFO", "rosotacom config", "not configured; use --project or `rosotacom config set`")
        line("OK", "ros2docker config", str(runtime.ros2docker_config))
        line("OK", "install id", runtime.install_id)
        line("OK", "workspace mount", f"{WS_DIR} -> /ws")
        if runtime.session_configs_dir:
            line("OK", "session definitions", f"{runtime.session_configs_dir} -> {SESSION_DEFINITION_CONTAINER_DIR}")
        else:
            line("INFO", "session definitions", "not configured")
        if runtime.scenario_configs_dir:
            line("OK", "scenario definitions", str(runtime.scenario_configs_dir))
        else:
            line("INFO", "scenario definitions", "not configured")
        line("OK", "session instances", f"{_session_instances_root(runtime)} -> {SESSION_INSTANCE_CONTAINER_DIR}")
        if runtime.data_dict:
            line("OK", "data dict", str(runtime.data_dict))
        else:
            line("INFO", "data dict", "not configured; literal peer addresses do not need one")
    except Exception as exc:  # noqa: BLE001 - doctor reports all setup failures.
        line("ERROR", "config", str(exc))

    stale = Path.home() / ".local" / "bin" / "start_rosotacom"
    if stale.exists() or stale.is_symlink():
        try:
            target = stale.resolve(strict=False)
        except OSError:
            target = Path(os.readlink(stale))
        if PROJECT_DIR not in [target, *target.parents]:
            line("WARN", "legacy start_rosotacom", f"{stale} points to {target}, outside this checkout")
        else:
            line("OK", "legacy start_rosotacom", f"{stale} points to this checkout")
    else:
        line("INFO", "legacy start_rosotacom", "no global legacy symlink found")

    tmux = shutil.which("tmux")
    if tmux:
        line("OK", "tmux", tmux)
    else:
        line("WARN", "tmux", "not installed; required only for `rosotacom scenario`")

    if runtime and _ROS2DOCKER_IMPORT_ERROR is None:
        try:
            config = load_config(runtime.ros2docker_config)
            line("OK", "ros2docker validation", "config loads")
            line("OK", "image", _scoped_image_name(runtime))
            if not config.get("mount_ws"):
                line("WARN", "mount_ws", "false; rosotacom will add its packaged /ws mount dynamically")
        except Exception as exc:  # noqa: BLE001
            line("ERROR", "ros2docker validation", str(exc))
    return 1 if failures else 0


# `ros2 topic hz`/`delay` never exit on their own, so we sample them for a fixed
# window via `timeout` and then SIGKILL, otherwise a slow rmw shutdown can keep
# the probe alive. The outer docker-exec timeout must stay well above the sample
# window plus the kill grace; on a loaded CI runner the original 12s outer budget
# could expire on an otherwise-healthy probe whose rmw teardown was slow, which
# aborted the whole smoke run instead of letting the caller retry.
_TOPIC_PROBE_WINDOW_S = 8
_TOPIC_PROBE_KILL_GRACE_S = 2
_TOPIC_PROBE_EXEC_TIMEOUT_S = 25


def _topic_probe_command(ros_setup: str, ros2_command: str) -> str:
    sampler = f"timeout -k {_TOPIC_PROBE_KILL_GRACE_S} {_TOPIC_PROBE_WINDOW_S} {ros2_command}"
    return f"{ros_setup} && {sampler} || true"


def _run_container_shell(container_name: str, command: str, timeout_s: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", "exec", container_name, "bash", "-lc", command],
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # A docker-exec timeout must not abort the whole smoke run: callers poll
        # and retry, so surface it as a non-fatal result with any partial output.
        def _as_text(value: str | bytes | None) -> str:
            if value is None:
                return ""
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value

        return subprocess.CompletedProcess(exc.cmd, 124, _as_text(exc.stdout), _as_text(exc.stderr))


def _wait_for_topic_hz(container_name: str, ros_setup: str, topic: str, *, timeout_s: int = 90) -> str:
    deadline = time.time() + timeout_s
    last_output = ""
    while time.time() < deadline:
        result = _run_container_shell(
            container_name,
            _topic_probe_command(ros_setup, f"ros2 topic hz {shlex.quote(topic)}"),
            timeout_s=_TOPIC_PROBE_EXEC_TIMEOUT_S,
        )
        last_output = (result.stdout or "") + (result.stderr or "")
        if "average rate" in last_output:
            return last_output
        time.sleep(2)
    return last_output


def _measure_topic_delay(
    container_name: str, ros_setup: str, topic: str, *, timeout_s: int = _TOPIC_PROBE_EXEC_TIMEOUT_S
) -> str:
    result = _run_container_shell(
        container_name,
        _topic_probe_command(ros_setup, f"ros2 topic delay {shlex.quote(topic)}"),
        timeout_s=timeout_s,
    )
    return (result.stdout or "") + (result.stderr or "")


def _parse_topic_hz_rate(output: str) -> float | None:
    match = re.search(r"average rate:\s*([0-9]+(?:\.[0-9]+)?)", output)
    return float(match.group(1)) if match else None


def _parse_topic_delay_seconds(output: str) -> float | None:
    match = re.search(r"average delay:\s*([0-9]+(?:\.[0-9]+)?)", output)
    return float(match.group(1)) if match else None


def _format_metric_value(value: float | None) -> str:
    return "nan" if value is None else f"{value:.6g}"


def _smoke_metric_line(*, label: str, topic: str, container_name: str, hz: float | None, delay_s: float | None) -> str:
    return (
        f"SMOKE_METRIC topic={topic} container={container_name} "
        f"hz={_format_metric_value(hz)} delay_s={_format_metric_value(delay_s)} label={label!r}"
    )


# Smoke runs both peers on a single host. Sharing the host loopback
# (`--network host`) lets the two peers' local- and OTA-domain DDS participants
# cross-match over `lo`, which intermittently produces duplicate delivery
# (inflated heartbeat rates) or no delivery at all. Instead we give each peer its
# own network namespace on a dedicated docker bridge with distinct IPs, mirroring
# a real two-machine deployment so the transports stay isolated.
SMOKE_NETWORK_NAME = "rosotacom-smoke"
SMOKE_NETWORK_SUBNET = "10.137.0.0/24"
SMOKE_PEER_IPS: dict[str, str] = {"a": "10.137.0.2", "b": "10.137.0.3"}


def _smoke_peer_address_args() -> list[str]:
    return [f"{peer}={ip}" for peer, ip in SMOKE_PEER_IPS.items()]


def _ensure_smoke_network() -> None:
    # Recreate from a clean slate so a leftover network from a crashed run cannot
    # cause a subnet-overlap failure on create.
    _remove_smoke_network()
    result = subprocess.run(
        ["docker", "network", "create", "--subnet", SMOKE_NETWORK_SUBNET, SMOKE_NETWORK_NAME],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to create smoke network {SMOKE_NETWORK_NAME} ({SMOKE_NETWORK_SUBNET}): "
            f"{(result.stderr or result.stdout).strip()}"
        )


def _remove_smoke_network() -> None:
    subprocess.run(
        ["docker", "network", "rm", SMOKE_NETWORK_NAME],
        text=True,
        capture_output=True,
        check=False,
    )


def _isolated_network_run_args(run_args: list[str], network_name: str, ip: str | None) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(run_args):
        token = run_args[i]
        if token in ("--network", "--net"):
            i += 2
            continue
        if token.startswith("--network=") or token.startswith("--net="):
            i += 1
            continue
        out.append(token)
        i += 1
    out.extend(["--network", network_name])
    if ip:
        out.extend(["--ip", ip])
    return out


def _smoke_heartbeat_topic(cfg: dict[str, Any], peer_key: str) -> str:
    peer_settings = cfg.get("peer_settings", {}) or {}
    if isinstance(peer_settings, dict):
        settings = peer_settings.get(peer_key, {}) or {}
        if isinstance(settings, dict) and settings.get("heartbeat_topic"):
            return str(settings["heartbeat_topic"])
    peers = cfg.get("peers", {}) or {}
    if not isinstance(peers, dict):
        raise RuntimeError("Smoke verification requires a session config with peers.")
    return f"/heartbeat_{_peer_com_name(peers, peer_key)}"


def _smoke_inbound_bridge_topic(cfg: dict[str, Any], source_peer_key: str) -> str:
    peers = cfg.get("peers", {}) or {}
    if not isinstance(peers, dict):
        raise RuntimeError("Smoke verification requires a session config with peers.")
    source_name = _peer_com_name(peers, source_peer_key)
    heartbeat_topic = _smoke_heartbeat_topic(cfg, source_peer_key).lstrip("/")
    return f"/com/in/{source_name}/{heartbeat_topic}"


def _smoke_forward_topic_for_inbound(
    cfg: dict[str, Any], source_peer_key: str, receiver_peer_key: str, topic: str
) -> str:
    peer_settings = cfg.get("peer_settings", {}) or {}
    peer_settings = peer_settings if isinstance(peer_settings, dict) else {}
    source_settings = peer_settings.get(source_peer_key, {}) or {}
    source_settings = source_settings if isinstance(source_settings, dict) else {}
    outbound = source_settings.get("outbound", {}) or {}
    outbound = outbound if isinstance(outbound, dict) else {}
    target_prefix = outbound.get("target_prefix", {}) or {}
    target_prefix = target_prefix if isinstance(target_prefix, dict) else {}
    if bool(target_prefix.get("use_target_prefix", False)):
        peers = cfg.get("peers", {}) or {}
        if not isinstance(peers, dict):
            raise RuntimeError("Smoke verification requires a session config with peers.")
        return f"/to_{_peer_com_name(peers, receiver_peer_key)}{topic}"
    return topic


def _smoke_inbound_forward_topic(cfg: dict[str, Any], source_peer_key: str, receiver_peer_key: str, topic: str) -> str:
    peers = cfg.get("peers", {}) or {}
    if not isinstance(peers, dict):
        raise RuntimeError("Smoke verification requires a session config with peers.")
    source_name = _peer_com_name(peers, source_peer_key)
    forward_topic = _smoke_forward_topic_for_inbound(cfg, source_peer_key, receiver_peer_key, topic).lstrip("/")
    return f"/com/in/{source_name}/{forward_topic}"


def _smoke_receiver_final_topic(cfg: dict[str, Any], source_peer_key: str, receiver_peer_key: str, topic: str) -> str:
    peer_settings = cfg.get("peer_settings", {}) or {}
    peer_settings = peer_settings if isinstance(peer_settings, dict) else {}
    receiver_settings = peer_settings.get(receiver_peer_key, {}) or {}
    receiver_settings = receiver_settings if isinstance(receiver_settings, dict) else {}
    inbound = receiver_settings.get("inbound", {}) or {}
    inbound = inbound if isinstance(inbound, dict) else {}
    if bool(inbound.get("keep_source_prefix", False)):
        peers = cfg.get("peers", {}) or {}
        if not isinstance(peers, dict):
            raise RuntimeError("Smoke verification requires a session config with peers.")
        return f"/{_peer_com_name(peers, source_peer_key)}{topic}"
    return topic


def _topic_direction_peers(direction: str, cfg: dict[str, Any]) -> tuple[str, str]:
    parts = direction.split("_to_")
    if len(parts) != 2:
        raise RuntimeError(f"topics key '{direction}' must match '<src>_to_<dst>'.")
    src, dst = parts
    peers = cfg.get("peers") or {}
    if src not in peers or dst not in peers:
        raise RuntimeError(f"topics key '{direction}' refers to unknown peer(s) '{src}'/'{dst}'.")
    return src, dst


def _smoke_topic_pipeline(cfg: dict[str, Any], entry: Any) -> dict[str, Any]:
    shared = cfg.get("shared", {}) or {}
    shared = shared if isinstance(shared, dict) else {}
    suffixes = shared.get("processing_suffixes", {}) or {}
    suffixes = suffixes if isinstance(suffixes, dict) else {}
    restamped_suffix = str(suffixes.get("restamped", "/restamped"))
    latched_suffix = str(suffixes.get("latched", "/latched"))
    globalframe_suffix = str(suffixes.get("framebridge_global", "/globalframe"))
    ota_suffix = str(suffixes.get("ota_stamped", "/ota_stamped"))
    compression = shared.get("compression", {}) or {}
    compression = compression if isinstance(compression, dict) else {}
    comp_alg_suffix = "/" + str(compression.get("algorithm", "bz2") or "bz2").strip()
    return cast(
        dict[str, Any],
        session_gen._compute_pipeline(
            entry,
            {},
            restamped_suffix,
            latched_suffix,
            globalframe_suffix,
            comp_alg_suffix,
            ota_suffix,
        ),
    )


def _smoke_postprocessed_topic(entry: Any, pipe: dict[str, Any]) -> str:
    if pipe.get("compress"):
        return str(pipe["comp_in"])
    if pipe.get("ota_wrap"):
        return str(pipe["ota_in"])
    return str(pipe["final"])


def _smoke_expect_bounds(expect: Any) -> tuple[float | None, float | None, float | None]:
    if not isinstance(expect, dict):
        return None, None, None
    hz = expect.get("hz") or {}
    latency = expect.get("latency_ms") or {}
    hz_min = float(hz["min"]) if isinstance(hz, dict) and hz.get("min") is not None else None
    hz_max = float(hz["max"]) if isinstance(hz, dict) and hz.get("max") is not None else None
    max_delay_s = (
        float(latency["max"]) / 1000.0 if isinstance(latency, dict) and latency.get("max") is not None else None
    )
    return hz_min, hz_max, max_delay_s


def _smoke_publish_rate(expect: Any) -> float:
    hz_min, hz_max, _ = _smoke_expect_bounds(expect)
    if hz_min is not None and hz_max is not None:
        return (hz_min + hz_max) / 2.0
    if hz_min is not None:
        return max(1.0, hz_min * 1.5)
    if hz_max is not None:
        return max(0.5, hz_max / 2.0)
    return 5.0


def _smoke_rmw_spec(cfg: dict[str, Any]) -> Any:
    peers = cfg.get("peers", {}) or {}
    if not isinstance(peers, dict):
        raise RuntimeError("Smoke verification requires a session config with peers.")
    shared = cfg.get("shared", {}) or {}
    if not isinstance(shared, dict):
        shared = {}
    return session_gen._parse_rmw_block(shared.get("rmw"), list(peers.keys()))


def _smoke_rmw_env_value(cfg: dict[str, Any]) -> str | None:
    rmw_spec = _smoke_rmw_spec(cfg)
    local_impl = rmw_spec.local.impl
    if local_impl is None:
        return None
    return str(session_gen.RMW_ALIASES.get(local_impl, local_impl))


def _smoke_local_domain_id(cfg: dict[str, Any], receiver_peer_key: str) -> str | None:
    shared = cfg.get("shared", {}) or {}
    shared = shared if isinstance(shared, dict) else {}
    peer_settings = cfg.get("peer_settings", {}) or {}
    peer_settings = peer_settings if isinstance(peer_settings, dict) else {}
    settings = peer_settings.get(receiver_peer_key, {}) or {}
    settings = settings if isinstance(settings, dict) else {}
    domain_id = settings.get("domain_id")
    if domain_id is None:
        domain_id = shared.get("local_domain_id")
    if domain_id is None or domain_id == "":
        return None
    return str(session_gen._parse_optional_domain_id(domain_id, f"peer_settings.{receiver_peer_key}.domain_id"))


def _smoke_local_config_commands(config_container_dir: str, cfg: dict[str, Any], receiver_peer_key: str) -> list[str]:
    local = _smoke_rmw_spec(cfg).local
    if not local.dds_config:
        return []
    config_file = f"{config_container_dir}/{receiver_peer_key}/local_dds.xml"
    if local.impl == "cyclone":
        return [f"export CYCLONEDDS_URI={shlex.quote(f'file://{config_file}')}"]
    if local.impl == "fastdds":
        return [
            f"export FASTDDS_DEFAULT_PROFILES_FILE={shlex.quote(config_file)}",
            "export RMW_FASTRTPS_USE_QOS_FROM_XML=1",
        ]
    return []


def _smoke_ros_setup(config_container_dir: str, cfg: dict[str, Any], receiver_peer_key: str) -> str:
    commands = [
        'ros_distro="${ROS_DISTRO:-kilted}"',
        'source "/opt/ros/${ros_distro}/setup.bash"',
        "source /opt/ros_venv/bin/activate",
        "{ [ ! -f /opt/custom_ws/install/setup.bash ] || source /opt/custom_ws/install/setup.bash; }",
        "{ [ ! -f /ros2ws/install/setup.bash ] || source /ros2ws/install/setup.bash; }",
    ]
    rmw_env = _smoke_rmw_env_value(cfg)
    if rmw_env:
        commands.append(f"export RMW_IMPLEMENTATION={shlex.quote(rmw_env)}")
        if rmw_env == "rmw_fastrtps_cpp":
            commands.append("export FASTDDS_BUILTIN_TRANSPORTS=${FASTDDS_BUILTIN_TRANSPORTS:-UDPv4}")
    local_domain_id = _smoke_local_domain_id(cfg, receiver_peer_key)
    if local_domain_id:
        commands.append(f"export ROS_DOMAIN_ID={shlex.quote(local_domain_id)}")
    commands.extend(_smoke_local_config_commands(config_container_dir, cfg, receiver_peer_key))
    return " && ".join(commands)


# --- Shared smoke/probe primitives -------------------------------------------
# Single source of truth for the heartbeat delivery bounds and the isolation
# probe topic used by local smoke and by external OTA harnesses.
SMOKE_HZ_MIN = 5.0
SMOKE_HZ_MAX = 20.0
SMOKE_MAX_DELAY_S = 1.0
SMOKE_PUBLISHER_DURATION_S = 900.0
ISOLATION_PROBE_TOPIC = "/local_only"


def _other_peer_key(cfg: dict[str, Any], identity: str) -> str:
    peers = cfg.get("peers") or {}
    if identity not in peers:
        raise RuntimeError(f"--identity must be one of peers={sorted(peers)}")
    return str(next(k for k in peers if k != identity))


def _received_crossed_topics(cfg: dict[str, Any], receiver_peer_key: str) -> list[SmokeTopicSpec]:
    """Topics the receiver must get from crossed session traffic.

    Heartbeat sessions self-publish from their plugin; plain topic examples need
    smoke to synthesize a local app publisher on the source peer.
    """
    source = _other_peer_key(cfg, receiver_peer_key)
    specs: list[SmokeTopicSpec] = []
    shared = cfg.get("shared", {}) or {}
    shared = shared if isinstance(shared, dict) else {}

    if bool(shared.get("use_heartbeat", False)):
        specs.extend(
            [
                SmokeTopicSpec(
                    source_peer_key=source,
                    receiver_peer_key=receiver_peer_key,
                    topic=_smoke_inbound_bridge_topic(cfg, source),
                    label=f"{source}->{receiver_peer_key} inbound bridge heartbeat",
                    enforce_bounds=False,
                ),
                SmokeTopicSpec(
                    source_peer_key=source,
                    receiver_peer_key=receiver_peer_key,
                    topic=_smoke_heartbeat_topic(cfg, source),
                    label=f"{source}->{receiver_peer_key} final heartbeat",
                    enforce_bounds=True,
                    use_default_bounds=True,
                ),
            ]
        )

    topics = cfg.get("topics", {}) or {}
    if not isinstance(topics, dict):
        raise RuntimeError("Smoke verification requires 'topics' to be a mapping when provided.")

    for direction in topics:
        src, dst = _topic_direction_peers(str(direction), cfg)
        if src != source or dst != receiver_peer_key:
            continue
        for entry in session_gen._topic_entries(cfg, str(direction)):
            pipe = _smoke_topic_pipeline(cfg, entry)
            final_topic = str(pipe["final"])
            if bool(shared.get("use_heartbeat", False)) and final_topic == _smoke_heartbeat_topic(cfg, src):
                continue
            postprocessed_topic = _smoke_postprocessed_topic(entry, pipe)
            received_topic = _smoke_receiver_final_topic(cfg, src, dst, postprocessed_topic)
            inbound_topic = _smoke_inbound_forward_topic(cfg, src, dst, final_topic)
            expect_hz_min, expect_hz_max, expect_max_delay_s = _smoke_expect_bounds(entry.expect)
            specs.extend(
                [
                    SmokeTopicSpec(
                        source_peer_key=src,
                        receiver_peer_key=dst,
                        topic=inbound_topic,
                        label=f"{src}->{dst} inbound bridge topic",
                        enforce_bounds=False,
                    ),
                    SmokeTopicSpec(
                        source_peer_key=src,
                        receiver_peer_key=dst,
                        topic=received_topic,
                        label=f"{src}->{dst} final topic",
                        enforce_bounds=any(
                            value is not None for value in (expect_hz_min, expect_hz_max, expect_max_delay_s)
                        ),
                        publish_topic=entry.base,
                        publish_type=entry.msg_type,
                        publish_rate=_smoke_publish_rate(entry.expect),
                        hz_min=expect_hz_min,
                        hz_max=expect_hz_max,
                        max_delay_s=expect_max_delay_s,
                        expected_size=66000 if entry.msg_type == "com_msgs/msg/SizedPayload" else None,
                    ),
                ]
            )

    return specs


def _verify_received_topics(
    container_name: str,
    ros_setup: str,
    cfg: dict[str, Any],
    receiver_peer_key: str,
    *,
    hz_min: float = SMOKE_HZ_MIN,
    hz_max: float = SMOKE_HZ_MAX,
    max_delay_s: float = SMOKE_MAX_DELAY_S,
    log_line: Callable[[str], None] = print,
    detail_log: Callable[[str], None] | None = None,
) -> list[str]:
    """Assert the receiver's crossed topics publish (and the final heartbeat does
    so within rate/latency bounds). Emits the OK/SMOKE_METRIC lines smoke has
    always produced; returns a list of failures (empty == all good)."""
    errors: list[str] = []
    for spec in _received_crossed_topics(cfg, receiver_peer_key):
        topic = spec.topic
        label = spec.label
        output = _wait_for_topic_hz(container_name, ros_setup, topic)
        if detail_log:
            detail_log(f"\n--- {label} ({topic}) in {container_name} ---\n{output}")
        if "average rate" not in output:
            errors.append(f"{label} ({topic}) not publishing in {container_name}")
            continue
        log_line(f"OK: {label} ({topic}) is publishing in {container_name}")
        hz = _parse_topic_hz_rate(output)
        delay_output = _measure_topic_delay(container_name, ros_setup, topic)
        if detail_log:
            detail_log(f"\n--- delay {label} ({topic}) in {container_name} ---\n{delay_output}")
        delay_s = _parse_topic_delay_seconds(delay_output)
        log_line(_smoke_metric_line(label=label, topic=topic, container_name=container_name, hz=hz, delay_s=delay_s))
        if spec.expected_size is not None:
            received_size = _received_sized_payload_size(container_name, ros_setup, topic)
            if received_size != spec.expected_size:
                errors.append(
                    f"{label} ({topic}) payload size {received_size} != {spec.expected_size} in {container_name}"
                )
            else:
                log_line(f"OK: {label} ({topic}) preserves SizedPayload size {received_size} in {container_name}")
        if spec.enforce_bounds:
            effective_hz_min = hz_min if spec.use_default_bounds else spec.hz_min
            effective_hz_max = hz_max if spec.use_default_bounds else spec.hz_max
            effective_max_delay_s = max_delay_s if spec.use_default_bounds else spec.max_delay_s
            if effective_hz_min is not None and (hz is None or hz < effective_hz_min):
                errors.append(f"{label} ({topic}) rate {hz} Hz below {effective_hz_min} in {container_name}")
            if effective_hz_max is not None and (hz is None or hz > effective_hz_max):
                errors.append(f"{label} ({topic}) rate {hz} Hz above {effective_hz_max} in {container_name}")
            if effective_max_delay_s is not None and (delay_s is None or delay_s >= effective_max_delay_s):
                errors.append(f"{label} ({topic}) latency {delay_s}s >= {effective_max_delay_s}s in {container_name}")
    return errors


def _smoke_publish_message(msg_type: str) -> str:
    normalized = msg_type.strip()
    if normalized in {"std_msgs/msg/String", "std_msgs/String"}:
        return "{data: 'rosotacom smoke'}"
    if normalized == "nav_msgs/msg/OccupancyGrid":
        return (
            "{header: {frame_id: map}, "
            "info: {resolution: 0.5, width: 4, height: 4, origin: {orientation: {w: 1.0}}}, "
            "data: [0, 0, 0, 0, 0, 25, 50, 0, 0, 50, 100, 0, -1, -1, -1, -1]}"
        )
    raise RuntimeError(f"Smoke cannot synthesize a publisher for message type {msg_type!r}.")


def _smoke_publisher_command(spec: SmokeTopicSpec, ros_setup: str, duration: float) -> str:
    assert spec.publish_topic is not None and spec.publish_type is not None
    if spec.publish_type == "com_msgs/msg/SizedPayload":
        size = spec.expected_size or 66000
        return (
            f"{ros_setup} && timeout {duration} ros2 run com_py sized_publisher --ros-args "
            f"-p topic:={shlex.quote(spec.publish_topic)} -p size:={size} -p rate:={spec.publish_rate}"
        )
    message = _smoke_publish_message(spec.publish_type)
    return (
        f"{ros_setup} && timeout {duration} ros2 topic pub -r {spec.publish_rate} "
        f"{shlex.quote(spec.publish_topic)} {shlex.quote(spec.publish_type)} "
        f"{shlex.quote(message)}"
    )


def _received_sized_payload_size(container_name: str, ros_setup: str, topic: str) -> int | None:
    command = (
        f"{ros_setup} && timeout -k 2 15 ros2 topic echo "
        f"{shlex.quote(topic)} com_msgs/msg/SizedPayload --once --field size"
    )
    result = _run_container_shell(container_name, command, timeout_s=_TOPIC_PROBE_EXEC_TIMEOUT_S)
    match = re.search(r"(?m)^\s*(\d+)\s*$", (result.stdout or "") + (result.stderr or ""))
    return int(match.group(1)) if match else None


def _smoke_publish_specs(cfg: dict[str, Any], source_peer_key: str | None = None) -> list[SmokeTopicSpec]:
    peers = cfg.get("peers") or {}
    specs: list[SmokeTopicSpec] = []
    seen: set[tuple[str, str, str]] = set()
    for receiver in peers:
        for spec in _received_crossed_topics(cfg, str(receiver)):
            if not spec.publish_topic:
                continue
            if source_peer_key is not None and spec.source_peer_key != source_peer_key:
                continue
            if not spec.publish_type:
                raise RuntimeError(f"Smoke topic {spec.publish_topic!r} requires a message type.")
            key = (spec.source_peer_key, spec.publish_topic, spec.publish_type)
            if key in seen:
                continue
            seen.add(key)
            specs.append(spec)
    return specs


def _start_smoke_topic_publishers(
    containers: dict[str, str],
    ros_setups: dict[str, str],
    cfg: dict[str, Any],
    *,
    log_line: Callable[[str], None] = print,
    duration: float = 180.0,
    source_peer_key: str | None = None,
) -> list[SmokeTopicSpec]:
    started: list[SmokeTopicSpec] = []
    for spec in _smoke_publish_specs(cfg, source_peer_key=source_peer_key):
        assert spec.publish_topic is not None and spec.publish_type is not None
        container = containers[spec.source_peer_key]
        ros_setup = ros_setups[spec.source_peer_key]
        cmd = _smoke_publisher_command(spec, ros_setup, duration)
        subprocess.run(
            ["docker", "exec", "-d", container, "bash", "-lc", cmd],
            capture_output=True,
            text=True,
            check=False,
        )
        for _ in range(10):
            if _topic_present(container, ros_setup, spec.publish_topic):
                log_line(
                    f"OK: smoke publisher {spec.source_peer_key}->{spec.receiver_peer_key} "
                    f"{spec.publish_topic} ({spec.publish_type}) is advertising in {container} "
                    f"at {spec.publish_rate:g} Hz"
                )
                started.append(spec)
                break
            time.sleep(1)
        else:
            raise RuntimeError(
                f"Smoke publisher {spec.source_peer_key}->{spec.receiver_peer_key} "
                f"{spec.publish_topic} ({spec.publish_type}) did not advertise in {container}."
            )
    return started


def _stop_smoke_topic_publishers(containers: dict[str, str], specs: list[SmokeTopicSpec]) -> None:
    for spec in specs:
        if not spec.publish_topic:
            continue
        container = containers.get(spec.source_peer_key)
        if not container:
            continue
        if spec.publish_type == "com_msgs/msg/SizedPayload":
            pattern = f"sized_publisher.*topic:={spec.publish_topic}"
        else:
            pattern = f"ros2 topic pub.*{spec.publish_topic}"
        subprocess.run(
            ["docker", "exec", container, "pkill", "-f", pattern],
            capture_output=True,
            text=True,
            check=False,
        )


def _publish_isolation_probe(
    container_name: str, ros_setup: str, topic: str, *, rate: float = 5.0, duration: float = 30.0
) -> None:
    """Publish a local-only topic in the container's local application domain,
    detached, for `duration` seconds (self-stops via `timeout`)."""
    cmd = f"{ros_setup} && timeout {duration} ros2 topic pub {shlex.quote(topic)} std_msgs/msg/Empty '{{}}' -r {rate}"
    subprocess.run(
        ["docker", "exec", "-d", container_name, "bash", "-lc", cmd], capture_output=True, text=True, check=False
    )


def _stop_isolation_probe(container_name: str, topic: str) -> None:
    """Stop any probe publisher for `topic` (so it cannot linger into a later
    check or session teardown)."""
    subprocess.run(
        ["docker", "exec", container_name, "pkill", "-f", f"ros2 topic pub {topic}"],
        capture_output=True,
        text=True,
        check=False,
    )


def _topic_present(container_name: str, ros_setup: str, topic: str) -> bool:
    result = _run_container_shell(
        container_name, f"{ros_setup} && timeout -k 2 8 ros2 topic list", timeout_s=_TOPIC_PROBE_EXEC_TIMEOUT_S
    )
    names = {ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip().startswith("/")}
    return topic in names


def _verify_isolation(
    pub_container: str,
    pub_setup: str,
    check_container: str,
    check_setup: str,
    topic: str,
    *,
    log_line: Callable[[str], None] = print,
) -> list[str]:
    """Publish a local-only topic in pub_container's local domain and assert it
    never appears in check_container's local domain."""
    _publish_isolation_probe(pub_container, pub_setup, topic)
    try:
        live = False
        for _ in range(8):
            if _topic_present(pub_container, pub_setup, topic):
                live = True
                break
            time.sleep(1)
        if not live:
            return [f"isolation check inconclusive: {topic} never advertised in {pub_container}"]
        if _topic_present(check_container, check_setup, topic):
            return [f"isolation breach: {topic} from {pub_container} leaked to {check_container}"]
        log_line(f"OK: isolation holds ({topic} from {pub_container} not visible in {check_container})")
        return []
    finally:
        _stop_isolation_probe(pub_container, topic)


# --- Local-check derivation ---------------------------------------------------


def _session_local_check(session_host_dir: Path) -> bool:
    """Whether a session is eligible for one-host smoke.

    OTA is the default suite membership. The only per-session axis left here is
    whether local smoke can fake the two peers with distinct local ROS domains.
    A config may explicitly opt out with ``local_check: false``; otherwise it is
    derived from per-peer domain IDs.
    """
    cfg = _load_session_config_from_host(session_host_dir)
    name = session_host_dir.name
    if "test_tiers" in cfg:
        raise RuntimeError(f"{name}: test_tiers is retired; remove it or use local_check: false for opt-outs.")

    explicit = cfg.get("local_check")
    if explicit is not None:
        if not isinstance(explicit, bool):
            raise RuntimeError(f"{name}: local_check must be a boolean when provided.")
        return explicit

    peers = cfg.get("peers")
    if not isinstance(peers, dict) or not peers:
        raise RuntimeError(f"{name}: peers must be a non-empty mapping.")
    shared = cfg.get("shared", {}) or {}
    if not isinstance(shared, dict):
        raise RuntimeError(f"{name}: shared must be a mapping when provided.")
    peer_settings = cfg.get("peer_settings", {}) or {}
    if not isinstance(peer_settings, dict):
        raise RuntimeError(f"{name}: peer_settings must be a mapping when provided.")

    shared_domain = session_gen._parse_optional_domain_id(shared.get("local_domain_id"), "shared.local_domain_id")
    domains: list[int] = []
    for peer_key in peers:
        settings = peer_settings.get(peer_key, {}) or {}
        if not isinstance(settings, dict):
            raise RuntimeError(f"{name}: peer_settings.{peer_key} must be a mapping when provided.")
        per_peer = session_gen._parse_optional_domain_id(
            settings.get("domain_id"),
            f"peer_settings.{peer_key}.domain_id",
        )
        domain = per_peer if per_peer is not None else shared_domain
        if domain is None:
            return False
        domains.append(domain)
    return len(set(domains)) == len(domains)


def session_local_checks(sessions_dir: Path | None = None) -> dict[str, bool]:
    """Map session name -> local smoke eligibility for every session."""
    base = sessions_dir or (EXAMPLE_PROJECT_DIR / "sessions")
    out: dict[str, bool] = {}
    for child in sorted(base.iterdir()):
        if child.is_dir() and _is_session_dir(child):
            out[child.name] = _session_local_check(child)
    return out


def local_check_sessions(sessions_dir: Path | None = None) -> list[str]:
    return [name for name, enabled in session_local_checks(sessions_dir).items() if enabled]


def ota_suite_sessions(sessions_dir: Path | None = None) -> list[str]:
    """All non-experimental sessions in a directory.

    Test-configs are OTA candidates by default; local smoke eligibility is only
    the fast-check lens on top.
    """
    base = sessions_dir or (EXAMPLE_PROJECT_DIR / "sessions")
    out: list[str] = []
    for child in sorted(base.iterdir()):
        if not (child.is_dir() and _is_session_dir(child)):
            continue
        cfg = _load_session_config_from_host(child)
        if not bool(cfg.get("experimental", False)):
            out.append(child.name)
    return out


def _running_instance_config_container_dir(
    runtime: RuntimeConfig, session: ResolvedSession, instance_id: str | None
) -> str:
    """Container-side config dir of the session instance currently running on this
    host (the instances root is bind-mounted at SESSION_INSTANCE_CONTAINER_DIR)."""
    instance_dir = _find_latest_instance_dir(runtime, session, instance_id)
    rel = instance_dir.relative_to(_session_instances_root(runtime))
    return f"{SESSION_INSTANCE_CONTAINER_DIR}/{rel}/config"


def _resolve_running_peer(args: argparse.Namespace, identity: str) -> tuple[str, str, dict[str, Any]]:
    """Resolve (container, ros_setup, cfg) for `identity`'s already-running peer."""
    runtime = _load_runtime_config(args)
    session = _resolve_session(getattr(args, "session_dir", None) or DEFAULT_SMOKE_SESSION, runtime)
    peer_overrides = _parse_peer_address_overrides(getattr(args, "peer_address", None))
    cfg = _effective_session_config(session.host_dir, runtime, peer_address_overrides=peer_overrides)
    containers = _identity_container_names(cfg, runtime, identity)
    if not containers:
        raise RuntimeError(f"No container resolved for identity {identity!r}")
    config_container_dir = _running_instance_config_container_dir(runtime, session, getattr(args, "instance_id", None))
    ros_setup = _smoke_ros_setup(config_container_dir, cfg, identity)
    return containers[0], ros_setup, cfg


def probe_publish_command(args: argparse.Namespace) -> int:
    """Publish (or --stop) a local-only probe topic in a running peer's local domain."""
    container, ros_setup, _ = _resolve_running_peer(args, args.identity)
    if args.stop:
        _stop_isolation_probe(container, args.topic)
        print(f"Stopped probe publisher for {args.topic} in {container} (identity {args.identity})")
        return 0
    _publish_isolation_probe(container, ros_setup, args.topic, rate=args.rate, duration=args.duration)
    # Block until the topic is actually advertised so a caller's subsequent
    # absence check on the other peer is meaningful (not a false pass on a
    # publisher that never came up).
    for _ in range(10):
        if _topic_present(container, ros_setup, args.topic):
            print(f"Publishing {args.topic} in {container} (identity {args.identity}); advertised.")
            return 0
        time.sleep(1)
    print(f"ERROR: {args.topic} did not advertise in {container} (identity {args.identity})", file=sys.stderr)
    return 1


def probe_check_command(args: argparse.Namespace) -> int:
    """Assert a topic is present/absent in a running peer's local domain (isolation)."""
    container, ros_setup, _ = _resolve_running_peer(args, args.identity)
    present = _topic_present(container, ros_setup, args.topic)
    state = "present" if present else "absent"
    print(f"{args.topic} is {state} in {container} (identity {args.identity}); expected {args.expect}")
    return 0 if present == (args.expect == "present") else 1


def publish_test_topics_command(args: argparse.Namespace) -> int:
    """Start or stop synthetic local app publishers for the peer's crossed topics.

    The external OTA harness uses this to exercise non-heartbeat example sessions
    with the same topic payloads that local smoke uses. Heartbeat-only sessions
    are a clean no-op because their plugins already self-publish heartbeats.
    """
    container, ros_setup, cfg = _resolve_running_peer(args, args.identity)
    specs = _smoke_publish_specs(cfg, source_peer_key=args.identity)
    if args.stop:
        _stop_smoke_topic_publishers({args.identity: container}, specs)
        print(f"Stopped {len(specs)} test topic publisher(s) in {container} (identity {args.identity})")
        return 0

    if not specs:
        print(f"No synthetic test topic publishers needed for identity {args.identity}")
        return 0

    try:
        started = _start_smoke_topic_publishers(
            {args.identity: container},
            {args.identity: ros_setup},
            cfg,
            duration=args.duration,
            source_peer_key=args.identity,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Started {len(started)} test topic publisher(s) in {container} (identity {args.identity})")
    return 0


def _load_status_reports(logs_dir: Path) -> dict[str, Any]:
    reports: dict[str, Any] = {}
    for peer_dir in sorted(p for p in logs_dir.iterdir() if p.is_dir()):
        status_json = peer_dir / "status" / "status.json"
        if status_json.exists():
            reports[peer_dir.name] = json.loads(status_json.read_text(encoding="utf-8"))
    return reports


def test_command(args: argparse.Namespace) -> int:
    """Assert a running/recent session meets its status + per-topic `expect` contract.

    Reads each peer's self-reported status.json (the live status overview) for the
    most recent instance and checks every crossed topic was delivered and meets its
    declared hz/latency expectations. Orchestration (bringing the session up) is the
    caller's job (e.g. `smoke` or the multi-machine harness)."""
    from . import status_eval  # local import: the package's lazy __getattr__ makes a top-level one re-entrant

    runtime = _load_runtime_config(args)
    session = _resolve_session(getattr(args, "session_dir", None) or DEFAULT_SMOKE_SESSION, runtime)
    cfg = _effective_session_config(session.host_dir, runtime)
    instance_dir = _find_latest_instance_dir(runtime, session, getattr(args, "instance_id", None))
    logs_dir = instance_dir / "logs"
    deadline = time.time() + max(0.0, float(getattr(args, "timeout", 30.0)))
    interval = max(0.5, float(getattr(args, "interval", 2.0)))
    expect_by_topic = status_eval.expectations_from_cfg(cfg)
    reports: dict[str, Any] = {}
    failures: list[str] = []

    while True:
        reports = _load_status_reports(logs_dir) if logs_dir.is_dir() else {}
        if reports:
            failures = status_eval.evaluate_reports(reports, expect_by_topic)
            if not failures:
                topic_count = sum(len(r.get("topics", [])) for r in reports.values())
                print(f"TEST OK: {len(reports)} peer(s), {topic_count} topic(s) meet status + expectations")
                return 0

        if time.time() >= deadline:
            break
        time.sleep(interval)

    if not reports:
        print(
            f"rosotacom test: no status.json under {logs_dir}. Start the session with "
            "shared.use_status_overview=true first.",
            file=sys.stderr,
        )
        return 1

    for failure in failures:
        print(f"TEST FAIL: {failure}", file=sys.stderr)
    return 1


def smoke(args: argparse.Namespace) -> int:
    if not args.local:
        raise RuntimeError("Only --local smoke mode is implemented.")
    session_dir = args.session_dir or DEFAULT_SMOKE_SESSION
    runtime = _load_runtime_config(args)
    session = _resolve_session(session_dir, runtime)
    instance_id = getattr(args, "instance_id", None) or _new_instance_id()
    smoke_instance = _resolve_session_instance(runtime, session, instance_id)
    smoke_log = smoke_instance.logs_host_dir / "smoke-verification.log"

    def log_line(message: str) -> None:
        print(message)
        _append_log(smoke_log, message)

    peer_address_args = _smoke_peer_address_args()
    peer_overrides = _parse_peer_address_overrides(peer_address_args)
    cfg = _effective_session_config(session.host_dir, runtime, peer_address_overrides=peer_overrides)
    common = {
        "rosotacom_config": args.rosotacom_config,
        "ros2docker_config": args.ros2docker_config,
        "session_configs_dir": args.session_configs_dir,
        "session_instances_dir": getattr(args, "session_instances_dir", None),
        "data_dict": args.data_dict,
        "session_dir": session_dir,
        "mode": "detached",
        "force": True,
        "rewrite_formatting": False,
        "overwrite_peers_via_remote_peer": None,
        "peer_address": peer_address_args,
        "instance_id": smoke_instance.instance_id,
        "network_name": SMOKE_NETWORK_NAME,
    }

    log_line(f"Starting local smoke test with PEER_ADDRESSES={', '.join(peer_address_args)}")
    log_line(f"Smoke peers isolated on docker network {SMOKE_NETWORK_NAME} ({SMOKE_NETWORK_SUBNET})")
    log_line(f"Smoke artifacts: {smoke_instance.host_dir}")
    a_container = None
    b_container = None
    smoke_publishers: list[SmokeTopicSpec] = []
    try:
        _ensure_smoke_network()
        a_container = start_session(
            argparse.Namespace(**common, identity="a", auto_identity=True, network_ip=SMOKE_PEER_IPS["a"])
        )
        b_container = start_session(
            argparse.Namespace(**common, identity="b", auto_identity=True, network_ip=SMOKE_PEER_IPS["b"])
        )

        plugin_text = "\n".join(
            (smoke_instance.config_host_dir / peer / "plugin.yaml").read_text(encoding="utf-8") for peer in ("a", "b")
        )
        expected_addresses = {arg.split("=", 1)[1] for arg in peer_address_args}
        if any(address not in plugin_text for address in expected_addresses) or "data:" in plugin_text:
            raise RuntimeError("Smoke verification failed: generated plugin.yaml did not use literal CLI addresses.")
        log_line("OK: generated plugin.yaml files use literal CLI addresses")

        def detail(msg: str) -> None:
            _append_log(smoke_log, msg)

        ros_setup_a = _smoke_ros_setup(smoke_instance.config_container_dir, cfg, "a")
        ros_setup_b = _smoke_ros_setup(smoke_instance.config_container_dir, cfg, "b")
        containers = {"a": a_container, "b": b_container}
        ros_setups = {"a": ros_setup_a, "b": ros_setup_b}
        smoke_publishers = _start_smoke_topic_publishers(
            containers,
            ros_setups,
            cfg,
            log_line=log_line,
            duration=SMOKE_PUBLISHER_DURATION_S,
        )
        errors: list[str] = []
        # Delivery: each peer must receive the other's crossed topics within bounds.
        errors += _verify_received_topics(b_container, ros_setup_b, cfg, "b", log_line=log_line, detail_log=detail)
        errors += _verify_received_topics(a_container, ros_setup_a, cfg, "a", log_line=log_line, detail_log=detail)
        # Isolation: a local-only topic published on a must not cross to b. On a
        # single host the distinct test domain IDs make this hold; it exercises the
        # same assertion that proves the real OTA guarantee multi-machine.
        errors += _verify_isolation(
            a_container, ros_setup_a, b_container, ros_setup_b, ISOLATION_PROBE_TOPIC, log_line=log_line
        )
        test_rc = test_command(
            argparse.Namespace(
                rosotacom_config=args.rosotacom_config,
                ros2docker_config=args.ros2docker_config,
                session_configs_dir=args.session_configs_dir,
                session_instances_dir=getattr(args, "session_instances_dir", None),
                data_dict=args.data_dict,
                session_dir=session_dir,
                instance_id=smoke_instance.instance_id,
                timeout=45.0,
                interval=2.0,
            )
        )
        if test_rc != 0:
            errors.append("rosotacom test failed for the session self-report")
        if errors:
            raise RuntimeError("Smoke verification failed:\n  - " + "\n  - ".join(errors))
    except Exception as exc:
        _append_log(smoke_log, f"ERROR: {exc}")
        raise
    finally:
        _stop_smoke_topic_publishers({"a": a_container or "", "b": b_container or ""}, smoke_publishers)
        for started_container, peer in [(a_container, "a"), (b_container, "b")]:
            if started_container:
                _write_docker_log(started_container, smoke_instance, peer)
        if not args.keep_running:
            runtime = _load_runtime_config(args)
            for started_container in [a_container, b_container]:
                if started_container:
                    _stop_container_name(started_container, runtime)
            _remove_smoke_network()
        print(f"Smoke artifacts: {smoke_instance.host_dir}")
    return 0


def _find_latest_instance_dir(runtime: RuntimeConfig, session: ResolvedSession, instance_id: str | None) -> Path:
    root = _session_instances_root(runtime)
    session_slug = _session_instance_slug(session, runtime)
    if instance_id:
        pattern = f"*/{session_slug}_*_{_safe_path_token(instance_id)}"
    else:
        pattern = f"*/{session_slug}_*"
    matches = sorted(p for p in root.glob(pattern) if p.is_dir())
    if not matches:
        raise RuntimeError(
            f"No session instance found for '{session.host_dir.name}' under {root}. "
            "Start a session with shared.use_status_overview=true first."
        )
    return matches[-1].resolve()


def _status_identities(logs_dir: Path, identity: str | None) -> list[str]:
    if identity:
        return [identity]
    if not logs_dir.is_dir():
        return []
    found = []
    for peer_dir in sorted(logs_dir.iterdir()):
        if (peer_dir / "status" / "status.json").exists():
            found.append(peer_dir.name)
    return found


def _render_status_for_identities(logs_dir: Path, identities: list[str], as_json: bool) -> str:
    if as_json:
        combined: dict[str, Any] = {}
        for identity in identities:
            json_path = logs_dir / identity / "status" / "status.json"
            if json_path.exists():
                combined[identity] = json.loads(json_path.read_text(encoding="utf-8"))
        return json.dumps(combined, indent=2)

    chunks: list[str] = []
    for identity in identities:
        status_dir = logs_dir / identity / "status"
        txt_path = status_dir / "status.txt"
        json_path = status_dir / "status.json"
        if txt_path.exists():
            chunks.append(txt_path.read_text(encoding="utf-8").rstrip())
        elif json_path.exists():
            snapshot = json.loads(json_path.read_text(encoding="utf-8"))
            chunks.append(_render_status_text_fallback(snapshot))
        else:
            chunks.append(f"(no status.json yet for identity '{identity}')")
    return "\n\n".join(chunks)


def _render_status_text_fallback(snapshot: dict[str, Any]) -> str:
    summary = snapshot.get("summary", {})
    lines = [
        f"rosotacom status  peer={snapshot.get('peer')} remote={snapshot.get('remote')}  "
        f"{snapshot.get('generated_at')}",
        f"summary: OK={summary.get('OK', 0)} PARTIAL={summary.get('PARTIAL', 0)} "
        f"STALLED={summary.get('STALLED', 0)} ABSENT={summary.get('ABSENT', 0)}",
        "",
    ]
    for topic in snapshot.get("topics", []):
        arrow = "->" if topic.get("direction") == "outbound" else "<-"
        lines.append(
            f"[{topic.get('overall'):<7}] {arrow} {topic.get('base')}  "
            f"(reached: {topic.get('reached_stage')}, blocked: {topic.get('blocked_at')})"
        )
        if topic.get("diagnosis"):
            lines.append(f"    -> {topic['diagnosis']}")
    return "\n".join(lines)


def status(args: argparse.Namespace) -> int:
    runtime = _load_runtime_config(args)
    session = _resolve_session(args.session_dir or DEFAULT_SMOKE_SESSION, runtime)
    instance_dir = _find_latest_instance_dir(runtime, session, getattr(args, "instance_id", None))
    logs_dir = instance_dir / "logs"

    def render_once() -> str:
        identities = _status_identities(logs_dir, getattr(args, "identity", None))
        if not identities:
            return (
                f"No status.json found under {logs_dir}.\n"
                "Ensure the session was started with shared.use_status_overview=true "
                "and has had time to initialize."
            )
        return _render_status_for_identities(logs_dir, identities, bool(getattr(args, "json", False)))

    if not getattr(args, "watch", False):
        print(render_once())
        return 0

    interval = max(0.5, float(getattr(args, "watch_interval", 2.0)))
    try:
        while True:
            # Clear screen for a live view.
            print("\033[2J\033[H", end="")
            print(f"(watching {instance_dir.name}; refresh {interval}s; Ctrl-C to stop)\n")
            print(render_once(), flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def _add_common_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--rosotacom-config",
        "--project",
        dest="rosotacom_config",
        help="Path to rosotacom.yaml (overrides cwd discovery and the global default).",
    )
    parser.add_argument("-f", "--ros2docker-config", help="Path to ros2docker JSON config.")
    parser.add_argument("--session-configs-dir", help="Host directory containing named session configs.")
    parser.add_argument("--scenario-configs-dir", help="Host directory containing named scenario configs.")
    parser.add_argument("--session-instances-dir", help="Host directory for generated session instances and logs.")
    parser.add_argument("--data-dict", help="Host data_dict.json path for data:<key> address expressions.")


def _add_session_arg(parser: argparse.ArgumentParser, *args: str, **kwargs: Any) -> argparse.Action:
    action = parser.add_argument(*args, **kwargs)
    cast(Any, action).completer = _session_name_completer
    return action


def _add_scenario_arg(
    parser: argparse.ArgumentParser,
    *args: str,
    active_only: bool = False,
    **kwargs: Any,
) -> argparse.Action:
    action = parser.add_argument(*args, **kwargs)
    cast(Any, action).completer = _active_scenario_name_completer if active_only else _scenario_name_completer
    return action


def _add_identity_arg(parser: argparse.ArgumentParser, *, required: bool = False, help: str | None = None) -> None:
    action = parser.add_argument("--identity", required=required, help=help)
    cast(Any, action).completer = _identity_completer


def _add_scenario_identity_args(parser: argparse.ArgumentParser) -> None:
    _add_identity_arg(parser)
    parser.add_argument("--no-auto-identity", dest="auto_identity", action="store_false")
    parser.add_argument("--overwrite-peers-via-remote-peer")
    parser.add_argument("--peer-address", action="append", default=[], metavar="PEER=ADDRESS_EXPR")
    parser.set_defaults(auto_identity=True)


def _add_start_args(parser: argparse.ArgumentParser) -> None:
    _add_common_config_args(parser)
    _add_session_arg(parser, "session_dir_positional", nargs="?")
    _add_session_arg(parser, "-s", "--session-dir", dest="session_dir")
    _add_identity_arg(parser)
    parser.add_argument("--mode", choices=["auto", "attach", "detached"], default="auto")
    parser.add_argument("--no-auto-identity", dest="auto_identity", action="store_false")
    parser.add_argument("--no-force", dest="force", action="store_false")
    parser.add_argument("--force", dest="force", action="store_true")
    parser.add_argument("--rewrite-formatting", action="store_true")
    parser.add_argument("--overwrite-peers-via-remote-peer")
    parser.add_argument("--peer-address", action="append", default=[], metavar="PEER=ADDRESS_EXPR")
    parser.add_argument("--instance-id", help="Join or create a named runtime session instance.")
    parser.add_argument("--scenario-managed", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(force=True, auto_identity=True)


def _normalize_session_arg(args: argparse.Namespace) -> None:
    positional = getattr(args, "session_dir_positional", None)
    explicit = getattr(args, "session_dir", None)
    if positional and explicit and positional != explicit:
        raise RuntimeError(f"conflicting session dirs: --session-dir={explicit} and positional={positional}")
    args.session_dir = explicit or positional
    if not args.session_dir:
        raise RuntimeError("--session-dir is required.")


def start_command(args: argparse.Namespace) -> int:
    _normalize_session_arg(args)
    start_session(args)
    return 0


def stop_command(args: argparse.Namespace) -> int:
    _normalize_session_arg(args)
    stop_session(args)
    return 0


def list_sessions_command(args: argparse.Namespace) -> int:
    list_sessions(args)
    return 0


def _safe_completion_shellcode(executable: str) -> str:
    """Register completion only when the executable currently on PATH supports argcomplete."""
    loader_path = importlib_resources.files("argcomplete").joinpath("bash_completion.d").joinpath("_python-argcomplete")
    loader = loader_path.read_text(encoding="utf-8")
    registration_marker = '\nif [[ -z "${ZSH_VERSION-}" ]]; then\n    complete -o default'
    registration_start = loader.rfind(registration_marker)
    if registration_start < 0:
        raise RuntimeError("Installed argcomplete does not contain the expected shell loader.")
    loader = loader[:registration_start].rstrip()
    command = shlex.quote(executable)
    return (
        f"{loader}\n"
        'if [[ -z "${ZSH_VERSION-}" ]]; then\n'
        f"    complete -o default -o bashdefault -F _python_argcomplete_global {command}\n"
        "else\n"
        "    autoload -Uz is-at-least\n"
        f"    compdef _python_argcomplete_global {command}\n"
        "fi\n"
    )


def completion_command(args: argparse.Namespace) -> int:
    shell = args.shell or Path(os.environ.get("SHELL", "bash")).name
    if shell not in {"bash", "zsh"}:
        raise RuntimeError(f"Could not infer a supported shell from {shell!r}; pass `bash` or `zsh` explicitly.")
    executable = Path(sys.argv[0]).name
    if executable in {"__main__.py", "cli.py"}:
        executable = "rosotacom"
    print(_safe_completion_shellcode(executable), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {
        "start",
        "stop",
        "doctor",
        "smoke",
        "status",
        "test",
        "verify",  # retired; keep guarded so it is not rewritten as `start verify`.
        "probe-publish",
        "probe-check",
        "publish-test-topics",
        "list-sessions",
        "scenario",
        "examples",
        "setup-env",
        "config",
        "completion",
    }
    if not argv:
        argv = ["start"]
    elif argv[0] not in commands and not argv[0].startswith("-"):
        argv = ["start", *argv]

    parser = argparse.ArgumentParser(prog="rosotacom")
    parser.add_argument("--version", action="version", version=f"rosotacom {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="Start a rosotacom session.")
    _add_start_args(start_parser)
    start_parser.set_defaults(func=start_command)

    stop_parser = subparsers.add_parser("stop", help="Stop rosotacom containers for a session.")
    _add_common_config_args(stop_parser)
    _add_session_arg(stop_parser, "session_dir_positional", nargs="?")
    _add_session_arg(stop_parser, "-s", "--session-dir", dest="session_dir")
    _add_identity_arg(stop_parser)
    stop_parser.add_argument("--auto-identity", action="store_true")
    stop_parser.add_argument("--peer-address", action="append", default=[], metavar="PEER=ADDRESS_EXPR")
    stop_parser.add_argument("--overwrite-peers-via-remote-peer")
    stop_parser.set_defaults(func=stop_command)

    doctor_parser = subparsers.add_parser("doctor", help="Report rosotacom host readiness diagnostics.")
    _add_common_config_args(doctor_parser)
    doctor_parser.set_defaults(func=doctor)

    smoke_parser = subparsers.add_parser("smoke", help="Run a local smoke test.")
    _add_common_config_args(smoke_parser)
    _add_session_arg(smoke_parser, "session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    smoke_parser.add_argument("--local", action="store_true", default=True)
    smoke_parser.add_argument("--local-ip")
    smoke_parser.add_argument("--keep-running", action="store_true", help="Leave smoke-test containers running.")
    smoke_parser.add_argument("--instance-id", help="Join or create a named runtime session instance.")
    smoke_parser.set_defaults(func=smoke)

    status_parser = subparsers.add_parser(
        "status", help="Show the live per-topic pipeline status for a session instance."
    )
    _add_common_config_args(status_parser)
    _add_session_arg(status_parser, "session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    _add_identity_arg(status_parser, help="Show only this peer identity (default: all available).")
    status_parser.add_argument(
        "--instance-id", help="Inspect a specific instance id (default: most recent for the session)."
    )
    status_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    status_parser.add_argument("--watch", action="store_true", help="Continuously refresh the view.")
    status_parser.add_argument("--watch-interval", type=float, default=2.0, help="Watch refresh interval (s).")
    status_parser.set_defaults(func=status)

    test_parser = subparsers.add_parser(
        "test", help="Assert a running/recent session meets its status + per-topic expect contract."
    )
    _add_common_config_args(test_parser)
    _add_session_arg(test_parser, "session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    test_parser.add_argument("--instance-id", help="Evaluate a specific instance id (default: most recent).")
    test_parser.add_argument("--timeout", type=float, default=30.0, help="Seconds to wait for status to settle.")
    test_parser.add_argument("--interval", type=float, default=2.0, help="Polling interval while waiting (s).")
    test_parser.set_defaults(func=test_command)

    probe_publish_parser = subparsers.add_parser(
        "probe-publish", help="Publish a local-only probe topic in a running peer's local domain (isolation)."
    )
    _add_common_config_args(probe_publish_parser)
    _add_session_arg(probe_publish_parser, "session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    _add_identity_arg(probe_publish_parser, required=True)
    probe_publish_parser.add_argument("--peer-address", action="append", default=[], metavar="PEER=ADDRESS_EXPR")
    probe_publish_parser.add_argument("--instance-id")
    probe_publish_parser.add_argument("--topic", default=ISOLATION_PROBE_TOPIC)
    probe_publish_parser.add_argument("--rate", type=float, default=5.0)
    probe_publish_parser.add_argument("--duration", type=float, default=30.0)
    probe_publish_parser.add_argument(
        "--stop", action="store_true", help="Stop a running probe publisher instead of starting one."
    )
    probe_publish_parser.set_defaults(func=probe_publish_command)

    probe_check_parser = subparsers.add_parser(
        "probe-check", help="Assert a topic is present/absent in a running peer's local domain (isolation)."
    )
    _add_common_config_args(probe_check_parser)
    _add_session_arg(probe_check_parser, "session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    _add_identity_arg(probe_check_parser, required=True)
    probe_check_parser.add_argument("--peer-address", action="append", default=[], metavar="PEER=ADDRESS_EXPR")
    probe_check_parser.add_argument("--instance-id")
    probe_check_parser.add_argument("--topic", default=ISOLATION_PROBE_TOPIC)
    probe_check_parser.add_argument("--expect", choices=["present", "absent"], default="absent")
    probe_check_parser.set_defaults(func=probe_check_command)

    publish_test_topics_parser = subparsers.add_parser(
        "publish-test-topics",
        help="Start/stop synthetic local app publishers for OTA example verification.",
    )
    _add_common_config_args(publish_test_topics_parser)
    _add_session_arg(publish_test_topics_parser, "session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    _add_identity_arg(publish_test_topics_parser, required=True)
    publish_test_topics_parser.add_argument("--peer-address", action="append", default=[], metavar="PEER=ADDRESS_EXPR")
    publish_test_topics_parser.add_argument("--instance-id")
    publish_test_topics_parser.add_argument("--duration", type=float, default=180.0)
    publish_test_topics_parser.add_argument(
        "--stop", action="store_true", help="Stop running synthetic test topic publishers."
    )
    publish_test_topics_parser.set_defaults(func=publish_test_topics_command)

    list_parser = subparsers.add_parser("list-sessions", help="List configured sessions.")
    _add_common_config_args(list_parser)
    list_parser.set_defaults(func=list_sessions_command)

    scenario_parser = subparsers.add_parser(
        "scenario",
        help="Run a communication session together with identity-specific local applications.",
    )
    scenario_subparsers = scenario_parser.add_subparsers(dest="scenario_command", required=True)

    scenario_start_parser = scenario_subparsers.add_parser(
        "start",
        help="Start a scenario in an isolated outer tmux session.",
    )
    _add_common_config_args(scenario_start_parser)
    _add_scenario_arg(scenario_start_parser, "scenario")
    _add_scenario_identity_args(scenario_start_parser)
    scenario_start_parser.add_argument("--mode", choices=["auto", "attach", "detached"], default="auto")
    scenario_start_parser.add_argument("--no-force", dest="force", action="store_false")
    scenario_start_parser.add_argument("--force", dest="force", action="store_true")
    scenario_start_parser.add_argument("--rewrite-formatting", action="store_true")
    scenario_start_parser.add_argument("--instance-id", help="Join or create a named runtime session instance.")
    scenario_start_parser.set_defaults(func=start_scenario, force=True)

    scenario_attach_parser = scenario_subparsers.add_parser(
        "attach",
        help="Attach to a running scenario's outer tmux session.",
    )
    _add_common_config_args(scenario_attach_parser)
    _add_scenario_arg(scenario_attach_parser, "scenario", nargs="?", active_only=True)
    _add_scenario_identity_args(scenario_attach_parser)
    scenario_attach_parser.set_defaults(func=attach_scenario)

    scenario_stop_parser = scenario_subparsers.add_parser(
        "stop",
        help="Stop scenario applications, communication container, and outer tmux.",
    )
    _add_common_config_args(scenario_stop_parser)
    _add_scenario_arg(scenario_stop_parser, "scenario", nargs="?", active_only=True)
    _add_scenario_identity_args(scenario_stop_parser)
    scenario_stop_parser.add_argument("--instance-id", help="Mark a specific scenario instance as stopped.")
    scenario_stop_parser.set_defaults(func=stop_scenario)

    scenario_list_parser = scenario_subparsers.add_parser("list", help="List configured scenarios.")
    _add_common_config_args(scenario_list_parser)
    scenario_list_parser.set_defaults(func=list_scenarios)

    scenario_run_application_parser = scenario_subparsers.add_parser(
        "_run-application",
        help="Internal application runner used by scenario tmux panes.",
    )
    _add_common_config_args(scenario_run_application_parser)
    _add_scenario_arg(scenario_run_application_parser, "scenario")
    _add_identity_arg(scenario_run_application_parser, required=True)
    scenario_run_application_parser.add_argument("--application", required=True)
    scenario_run_application_parser.set_defaults(func=run_scenario_application)

    examples_parser = subparsers.add_parser("examples", help="Manage packaged rosotacom examples.")
    examples_subparsers = examples_parser.add_subparsers(dest="examples_command", required=True)
    examples_create_parser = examples_subparsers.add_parser("create", help="Copy the packaged example project.")
    examples_create_parser.add_argument("target", help="Directory to create.")
    examples_create_parser.add_argument("--force", action="store_true", help="Replace the target if it exists.")
    examples_create_parser.set_defaults(func=examples_create_command)

    setup_env_parser = subparsers.add_parser(
        "setup-env",
        help="[deprecated] Alias for `config set project --shell`. Print shell exports for a rosotacom.yaml.",
    )
    setup_env_parser.add_argument("rosotacom_config", help="Path to rosotacom.yaml.")
    setup_env_parser.set_defaults(func=setup_env_command)

    config_parser = subparsers.add_parser("config", help="Inspect or set the active rosotacom project.")
    config_subparsers = config_parser.add_subparsers(dest="config_action", required=True)

    config_set_parser = config_subparsers.add_parser(
        "set", help="Set the active project (--global, or --shell for this terminal)."
    )
    config_set_parser.add_argument("key", choices=["project"])
    config_set_parser.add_argument("value", help="Path to rosotacom.yaml.")
    config_set_scope = config_set_parser.add_mutually_exclusive_group()
    config_set_scope.add_argument(
        "--global",
        dest="scope",
        action="store_const",
        const="global",
        help="Persist as the machine-wide default for all terminals.",
    )
    config_set_scope.add_argument(
        "--shell",
        dest="scope",
        action="store_const",
        const="shell",
        help="Print an export to eval in the current terminal only (default). "
        "For a directory ('local') scope, just keep a rosotacom.yaml in it.",
    )
    config_set_parser.set_defaults(func=config_command, scope="shell")

    config_get_parser = config_subparsers.add_parser("get", help="Print the resolved active project path.")
    config_get_parser.add_argument("key", choices=["project"], nargs="?", default="project")
    config_get_parser.set_defaults(func=config_command)

    config_show_parser = config_subparsers.add_parser("show", help="Show every project-selection scope and the winner.")
    config_show_parser.set_defaults(func=config_command)

    config_unset_parser = config_subparsers.add_parser("unset", help="Clear the machine-wide default project.")
    config_unset_parser.add_argument("key", choices=["project"], nargs="?", default="project")
    config_unset_parser.add_argument(
        "--global",
        dest="scope",
        action="store_const",
        const="global",
        default="global",
        help="Clear the machine-wide default (the only persisted scope).",
    )
    config_unset_parser.set_defaults(func=config_command)

    completion_parser = subparsers.add_parser(
        "completion",
        help="Print shell code that enables command and session-name tab completion.",
    )
    completion_parser.add_argument(
        "shell",
        choices=["bash", "zsh"],
        nargs="?",
        help="Shell syntax to emit (default: infer from $SHELL).",
    )
    completion_parser.set_defaults(func=completion_command)

    argcomplete.autocomplete(parser)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except Exception as exc:  # noqa: BLE001 - CLI should be concise.
        print(f"rosotacom: error: {exc}", file=sys.stderr)
        return 1


def start_compat_main() -> int:
    return main(["start", *sys.argv[1:]])


def stop_compat_main() -> int:
    return main(["stop", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
