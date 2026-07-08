#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""rosotacom host CLI.

This module owns rosotacom-specific concepts such as project/deployment
configuration, session instances, multi-checkout names, and smoke tests.
ros2docker remains the generic Docker runner underneath.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import importlib
import importlib.resources as importlib_resources
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import argcomplete
import yaml
from argcomplete.completers import DirectoriesCompleter

from . import __version__
from .bundle_check import (
    BundleCheckConfig,
    ExpectedPath,
    check_bundle,
    format_bundle_report,
    load_bundle_manifest,
    merge_bundle_configs,
)
from .deployment import (
    DeploymentConfig,
    PeerBinding,
    load_deployment,
    parse_assignments,
    resolve_peer_bindings,
)

anonymize_lib = importlib.import_module("rosotacom.anonymize")

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
RUN_SESSION_CONTAINER_PATH = "/ws/session/creation/run_session.py"
DEFAULT_SMOKE_SESSION = "1_heartbeat"
OTA_SUDO_MODES = ("passwordless", "askpass")
OTA_DEFAULT_SUDO_MODE = "passwordless"

ws_creation_dir = WS_DIR / "session" / "creation"
session_gen_path = ws_creation_dir / "generate_session_files.py"
spec = importlib.util.spec_from_file_location("session_gen", session_gen_path)
assert spec is not None and spec.loader is not None
session_gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session_gen)

sys.path.append(str(WS_DIR))

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


@dataclass(frozen=True)
class RuntimeConfig:
    rosotacom_config: Path | None
    ros2docker_config: Path
    session_configs_dir: tuple[Path, ...]
    deployment: Path | None
    install_id: str
    session_instances_dir: Path | None = None
    project_source: str | None = None
    scenario_configs_dir: tuple[Path, ...] = ()
    profiles_file: Path | None = None
    benchmarks_dir: Path | None = None


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
class InteractiveSmokeTarget:
    name: str
    target_type: str
    session: ResolvedSession
    cfg: dict[str, Any]
    scenario: ResolvedScenario | None = None
    scenario_definition: ScenarioDefinition | None = None


@dataclass(frozen=True)
class ActiveInteractiveSmokeRun:
    target: str
    target_type: str
    tmux_session: str
    instance_id: str
    network_name: str


@dataclass(frozen=True)
class OtaSmokePeer:
    name: str
    ssh: str | None
    address: str


@dataclass(frozen=True)
class OtaSmokePlan:
    state_path: Path | None
    workdir: str
    rosotacom: str
    project: str
    peers: dict[str, OtaSmokePeer]


@dataclass(frozen=True)
class ActiveOtaSmokeRun:
    target: str
    target_type: str
    tmux_session: str
    instance_id: str
    state_path: str


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
class SmokeNetworkConfig:
    name: str
    subnet: str
    peer_ips: dict[str, str]


@dataclass(frozen=True)
class SmokeTopicSpec:
    source_peer_key: str
    receiver_peer_key: str
    topic: str
    label: str
    enforce_bounds: bool
    use_default_bounds: bool = False
    delivery_mode: str = "stream"
    publish_topic: str | None = None
    publish_type: str | None = None
    publish_rate: float = 5.0
    hz_min: float | None = None
    hz_max: float | None = None
    max_delay_s: float | None = None
    expected_size: int | None = None
    publish_qos: dict[str, Any] | None = None


def _load_yaml_file(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"Config file must contain a mapping: {path}")
    return loaded


def _first_value(*values: Any) -> Any:
    for value in values:
        if value is None or value == "":
            continue
        if isinstance(value, list | tuple) and not value:
            continue
        else:
            return value
    return None


def _path_values(raw: Any, key: str) -> tuple[str | os.PathLike[str], ...]:
    if raw is None:
        return ()
    if isinstance(raw, list | tuple):
        values = raw
    elif isinstance(raw, str) and "," in raw:
        values = tuple(part.strip() for part in raw.split(","))
    else:
        values = (raw,)

    clean_values: list[str | os.PathLike[str]] = []
    for value in values:
        if isinstance(value, str):
            if value.strip():
                clean_values.append(value)
            continue
        if isinstance(value, os.PathLike):
            clean_values.append(value)
            continue
        raise RuntimeError(f"{key} entries must be paths, got: {value!r}")
    return tuple(clean_values)


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


def _resolve_path_list(raw: Any, base_dir: Path, key: str, *, must_exist: bool = True) -> tuple[Path, ...]:
    paths: list[Path] = []
    for value in _path_values(raw, key):
        path = _resolve_path(value, base_dir, must_exist=must_exist)
        if path is not None:
            paths.append(path)
    return tuple(paths)


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
    setup. Nothing is copied — the config and ``sessions/``
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
    allowed_project_keys = {
        "ros2docker_config",
        "session_configs_dir",
        "scenario_configs_dir",
        "session_instances_dir",
        "deployment",
        "profiles",
        "benchmarks_dir",
    }
    unknown_project_keys = sorted(set(cfg) - allowed_project_keys)
    if unknown_project_keys:
        raise RuntimeError(
            f"Unsupported rosotacom.yaml keys {unknown_project_keys}. Allowed keys: {sorted(allowed_project_keys)}"
        )

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
    deployment_raw = _first_value(
        getattr(args, "deployment", None),
        os.environ.get("ROSOTACOM_DEPLOYMENT"),
        cfg.get("deployment"),
    )
    profiles_raw = _first_value(
        getattr(args, "profiles_file", None),
        os.environ.get("ROSOTACOM_PROFILES"),
        cfg.get("profiles"),
    )
    benchmarks_dir_raw = _first_value(
        getattr(args, "artifacts_dir", None),
        os.environ.get("ROSOTACOM_BENCHMARKS_DIR"),
        cfg.get("benchmarks_dir"),
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
        session_configs_dir=_resolve_path_list(
            session_configs_dir_raw,
            config_base,
            "session_configs_dir",
            must_exist=True,
        ),
        deployment=_resolve_path(deployment_raw, config_base, must_exist=True),
        install_id=_install_id(rosotacom_config),
        session_instances_dir=session_instances_dir,
        project_source=project_source,
        scenario_configs_dir=_resolve_path_list(
            scenario_configs_dir_raw,
            config_base,
            "scenario_configs_dir",
            must_exist=True,
        ),
        profiles_file=_resolve_path(profiles_raw, config_base, must_exist=True),
        benchmarks_dir=_resolve_path(benchmarks_dir_raw, config_base, must_exist=False),
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


def _instance_name_token(instance_id: str) -> str:
    # Underscore-free so the instance component of a container name can be split
    # off unambiguously (the surrounding name parts are underscore-separated).
    return _safe_path_token(instance_id).replace("_", "-")


def _workspace_container_prefix(runtime: RuntimeConfig) -> str:
    return f"rosotacom_{runtime.install_id}_"


def _container_name(remote_peer_name: str, runtime: RuntimeConfig, instance_id: str) -> str:
    return _sanitize_docker_name(
        f"rosotacom_{runtime.install_id}_{_instance_name_token(instance_id)}_com_to_{remote_peer_name}"
    )


def _split_workspace_container(name: str, runtime: RuntimeConfig) -> tuple[str, str] | None:
    """Split a workspace container name into (instance_token, rest), or None."""
    prefix = _workspace_container_prefix(runtime)
    if not name.startswith(prefix):
        return None
    instance_token, sep, rest = name[len(prefix) :].partition("_")
    if not sep or not instance_token or not rest:
        return None
    return instance_token, rest


def _list_docker_containers(*, all_states: bool = False) -> list[tuple[str, list[str]]]:
    """Running (or all) docker containers as (name, [network, ...]) pairs."""
    command = ["docker", "ps", "--format", "{{.Names}}\t{{.Networks}}"]
    if all_states:
        command.insert(2, "--all")
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"docker ps failed: {(result.stderr or result.stdout).strip()}")
    containers: list[tuple[str, list[str]]] = []
    for line in result.stdout.splitlines():
        name, _, networks = line.partition("\t")
        if name:
            containers.append((name, [net for net in networks.split(",") if net]))
    return containers


def _matching_com_containers(runtime: RuntimeConfig, remote_peer_name: str, *, all_states: bool = False) -> list[str]:
    """Workspace communication containers for `remote_peer_name`, any instance."""
    suffix = _sanitize_docker_name(f"com_to_{remote_peer_name}")
    names = []
    for name, _networks in _list_docker_containers(all_states=all_states):
        parts = _split_workspace_container(name, runtime)
        if parts is not None and parts[1] == suffix:
            names.append(name)
    return sorted(names)


def _conflict_error(headline: str, containers: list[str], stop_hint: str) -> RuntimeError:
    lines = [headline, "Active containers:"]
    lines.extend(f"  - {name}" for name in sorted(containers))
    lines.append(stop_hint)
    return RuntimeError("\n".join(lines))


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
    for index, root in enumerate(runtime.session_configs_dir):
        rel = _relative_to(session.host_dir, root)
        if rel is not None:
            prefix = "" if index == 0 else f"cfg{index + 1}_"
            return _safe_path_token(f"{prefix}{rel.as_posix().replace('/', '_')}")
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
    bindings: dict[str, PeerBinding],
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
            "peers": {
                peer: {
                    "address": binding.address,
                    "host": binding.host,
                    "ssh_configured": bool(binding.ssh),
                }
                for peer, binding in sorted(bindings.items())
            },
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
                "container_name": _scenario_container_name(
                    runtime, resolved.name, identity, application.name, instance.instance_id
                ),
                "image_name": _scenario_application_image_name(runtime, application),
            }
            for application in applications
        ],
    }
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=True, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


def _interactive_smoke_run_key(target_type: str, target_name: str) -> str:
    return f"{target_type}:{target_name}"


def _write_interactive_smoke_manifest(
    instance: SessionInstance,
    target: InteractiveSmokeTarget,
    runtime: RuntimeConfig,
    *,
    peer_ips: dict[str, str],
    network_name: str,
    network_subnet: str,
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
            "source_session_host_dir": str(target.session.host_dir),
            "source_session_container_dir": target.session.container_dir,
            "source": target.session.source,
            "config_dir": str(instance.config_host_dir),
            "logs_dir": str(instance.logs_host_dir),
            "rosbags_dir": str(instance.rosbags_host_dir),
            "rollout": None,
            "starts": [],
        }
    manifest["updated_at"] = now
    manifest["effective_config_sha256"] = _effective_config_sha256(target.cfg)
    smoke_runs = manifest.setdefault("interactive_smoke_runs", {})
    run: dict[str, Any] = {
        "started_at": now,
        "stopped_at": None,
        "target": target.name,
        "target_type": target.target_type,
        "tmux_socket": _scenario_tmux_socket(runtime),
        "tmux_session": tmux_session,
        "network_name": network_name,
        "network_subnet": network_subnet,
        "peer_address": [f"{peer}={ip}" for peer, ip in peer_ips.items()],
        "communication_containers": {
            peer: _container_name(_remote_peer_name(target.cfg, peer), runtime, instance.instance_id)
            for peer in peer_ips
        },
    }
    if target.scenario is not None and target.scenario_definition is not None:
        run["source_scenario_definition"] = str(target.scenario.definition_path)
        run["applications"] = [
            {
                "identity": identity,
                "name": application.name,
                "ros2docker_config": str(application.ros2docker_config),
                "container_name": _scenario_container_name(
                    runtime, target.scenario.name, identity, application.name, instance.instance_id
                ),
                "image_name": _scenario_application_image_name(runtime, application),
            }
            for identity, applications in target.scenario_definition.applications.items()
            for application in applications
        ]
    smoke_runs[_interactive_smoke_run_key(target.target_type, target.name)] = run
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


def _mark_interactive_smoke_stopped(instance_dir: Path, target_type: str, target_name: str) -> None:
    manifest_path = instance_dir / "manifest.yaml"
    manifest = _load_yaml_file(manifest_path)
    run = (manifest.get("interactive_smoke_runs") or {}).get(_interactive_smoke_run_key(target_type, target_name))
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
    return parse_assignments(overrides, option="--peer-address")


def _parse_peer_assignments(overrides: list[str] | None) -> dict[str, str]:
    return parse_assignments(overrides, option="--peer")


def _parse_peer_ssh_overrides(overrides: list[str] | None) -> dict[str, str]:
    return parse_assignments(overrides, option="--peer-ssh")


def _deployment_config(runtime: RuntimeConfig) -> DeploymentConfig | None:
    return load_deployment(runtime.deployment)


def _resolve_bindings(
    cfg: dict[str, Any],
    runtime: RuntimeConfig,
    *,
    peer: list[str] | None = None,
    peer_address: list[str] | None = None,
    peer_ssh: list[str] | None = None,
    require_addresses: bool = True,
) -> dict[str, PeerBinding]:
    return resolve_peer_bindings(
        cfg,
        _deployment_config(runtime),
        peer_assignments=_parse_peer_assignments(peer),
        address_overrides=_parse_peer_address_overrides(peer_address),
        ssh_overrides=_parse_peer_ssh_overrides(peer_ssh),
        require_addresses=require_addresses,
    )


def _effective_session_config(
    session_host_dir: Path,
    runtime: RuntimeConfig | None = None,
) -> dict[str, Any]:
    cfg = _load_session_config_from_host(session_host_dir)
    session_gen._validate_session_template_cfg(cfg)
    return cfg


def _binding_addresses(bindings: dict[str, PeerBinding]) -> dict[str, str]:
    return {peer: binding.address for peer, binding in bindings.items()}


def _auto_identity(bindings: dict[str, PeerBinding]) -> str:
    local_ips = set(_get_local_ipv4s())
    if not local_ips:
        raise RuntimeError("Auto identity failed: could not determine local IPv4 addresses. Use --identity.")

    matches = []
    for peer_key, binding in bindings.items():
        if local_ips.intersection(_IPV4_RE.findall(binding.address)):
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


def _identity_container_names(
    cfg: dict[str, Any],
    runtime: RuntimeConfig,
    identity: str | None = None,
    *,
    all_states: bool = False,
) -> list[str]:
    """Discover this workspace's communication containers for the given identities.

    Container names carry a per-run instance id, so they cannot be recomputed
    here; instead they are matched against docker's live container list.
    """
    peers = (cfg or {}).get("peers")
    if not isinstance(peers, dict):
        raise RuntimeError("session-config must define a mapping 'peers: { <peer_key>: { ... } }'.")
    identities = [identity] if identity else list(peers.keys())
    names: list[str] = []
    for peer_key in identities:
        if peer_key not in peers:
            raise RuntimeError(f"--identity must be one of peers={list(peers.keys())}")
        names.extend(_matching_com_containers(runtime, _remote_peer_name(cfg, str(peer_key)), all_states=all_states))
    return names


def _relative_to(path: Path, base: Path) -> Path | None:
    try:
        return path.resolve().relative_to(base.resolve())
    except ValueError:
        return None


def _session_config_container_root(index: int) -> str:
    if index == 0:
        return SESSION_DEFINITION_CONTAINER_DIR
    return f"{SESSION_DEFINITION_CONTAINER_DIR}-{index + 1}"


def _configured_session_container_dir(host_dir: Path, runtime: RuntimeConfig) -> str | None:
    for index, root in enumerate(runtime.session_configs_dir):
        cfg_rel = _relative_to(host_dir, root)
        if cfg_rel is not None:
            return f"{_session_config_container_root(index)}/{cfg_rel.as_posix()}"
    return None


def _configured_session_container_root(host_dir: Path, runtime: RuntimeConfig) -> str | None:
    for index, root in enumerate(runtime.session_configs_dir):
        if _relative_to(host_dir, root) is not None:
            return _session_config_container_root(index)
    return None


def _is_session_dir(path: Path) -> bool:
    return (path / "session-parametrization.yaml").exists() or (path / "session-definition.yaml").exists()


def _resolve_session(session_dir: str, runtime: RuntimeConfig) -> ResolvedSession:
    raw = Path(os.path.expandvars(os.path.expanduser(session_dir)))

    candidates: list[tuple[Path, str]] = []
    if raw.is_absolute():
        if str(raw).startswith("/ws/"):
            candidates.append((PROJECT_DIR / str(raw).lstrip("/"), "workspace"))
        for index, root in enumerate(runtime.session_configs_dir):
            container_root = _session_config_container_root(index)
            if str(raw).startswith(f"{container_root}/"):
                rel = Path(str(raw)[len(container_root) :].lstrip("/"))
                candidates.append((root / rel, "session_configs"))
        if str(raw).startswith(f"{SESSION_CONFIG_CONTAINER_DIR}/") and runtime.session_configs_dir:
            rel = Path(str(raw)[len(SESSION_CONFIG_CONTAINER_DIR) :].lstrip("/"))
            candidates.append((runtime.session_configs_dir[0] / rel, "session_configs"))
        candidates.append((raw, "absolute"))
    else:
        candidates.append((Path.cwd() / raw, "cwd"))
        for root in runtime.session_configs_dir:
            candidates.append((root / raw, "session_configs"))

    for candidate, source in candidates:
        if candidate.is_dir() and _is_session_dir(candidate):
            host_dir = candidate.resolve()
            ws_rel = _relative_to(host_dir, WS_DIR)
            if ws_rel is not None:
                return ResolvedSession(host_dir, f"/ws/{ws_rel.as_posix()}", "workspace")
            configured_container_dir = _configured_session_container_dir(host_dir, runtime)
            if configured_container_dir is not None:
                return ResolvedSession(host_dir, configured_container_dir, "session_configs")
            return ResolvedSession(host_dir, EXTERNAL_SESSION_CONTAINER_DIR, source)

    available = _format_available_sessions(runtime)
    raise RuntimeError(f"--session-dir must be a directory, got: {session_dir}\n{available}")


def _format_available_sessions(runtime: RuntimeConfig) -> str:
    lines: list[str] = []
    for root in runtime.session_configs_dir:
        if not root.is_dir():
            continue
        sessions = [path.name for path in sorted(root.iterdir()) if path.is_dir() and _is_session_dir(path)]
        for name in sessions:
            lines.append(f"  - {name} ({root})")
    if lines:
        return "Configured sessions:\n" + "\n".join(lines)
    return (
        "No configured session directories found. Create examples with "
        "`rosotacom examples create ./rosotacom_examples`, then enter that directory "
        "or select its project with `rosotacom config set project ... --shell`."
    )


def _session_names(runtime: RuntimeConfig) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for root in runtime.session_configs_dir:
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if not path.is_dir() or not _is_session_dir(path) or path.name in seen:
                continue
            names.append(path.name)
            seen.add(path.name)
    return names


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
        for root in runtime.scenario_configs_dir:
            candidates.append((root / raw, "scenario_configs"))

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
    names: list[str] = []
    seen: set[str] = set()
    for root in runtime.scenario_configs_dir:
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if not path.is_dir() or not _is_scenario_dir(path) or path.name in seen:
                continue
            names.append(path.name)
            seen.add(path.name)
    return names


def _format_available_scenarios(runtime: RuntimeConfig) -> str:
    lines: list[str] = []
    for root in runtime.scenario_configs_dir:
        if not root.is_dir():
            continue
        scenarios = [path.name for path in sorted(root.iterdir()) if path.is_dir() and _is_scenario_dir(path)]
        for name in scenarios:
            lines.append(f"  - {name} ({root})")
    if lines:
        return "Configured scenarios:\n" + "\n".join(lines)
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


def _peer_keys_from_cfg(cfg: dict[str, Any]) -> list[str]:
    peers = cfg.get("peers")
    if not isinstance(peers, dict) or not peers:
        raise RuntimeError("Smoke target session must define a non-empty peers mapping.")
    return [str(peer) for peer in peers]


def _require_two_peer_smoke_cfg(cfg: dict[str, Any], target_name: str) -> list[str]:
    peers = _peer_keys_from_cfg(cfg)
    if len(peers) != 2:
        raise RuntimeError(
            f"Interactive smoke v1 requires exactly 2 peers for local end-to-end runs; "
            f"target '{target_name}' defines peers={peers}."
        )
    return peers


def _resolve_interactive_smoke_target(
    target_arg: str | None,
    runtime: RuntimeConfig,
    target_type: str,
) -> InteractiveSmokeTarget:
    if target_type not in {"auto", "session", "scenario"}:
        raise RuntimeError("--target-type must be one of: auto, session, scenario")
    target_name = target_arg or DEFAULT_SMOKE_SESSION
    prefer_scenario = target_type == "scenario" or (target_type == "auto" and target_arg is not None)

    scenario_error: Exception | None = None
    if prefer_scenario:
        try:
            scenario = _resolve_scenario(target_name, runtime)
            definition = _load_scenario_definition(scenario)
            session = _resolve_session(definition.session, runtime)
            cfg = _effective_session_config(session.host_dir, runtime)
            _require_two_peer_smoke_cfg(cfg, scenario.name)
            unknown_identities = sorted(set(definition.applications) - set(_peer_keys_from_cfg(cfg)))
            if unknown_identities:
                raise RuntimeError(
                    f"Scenario '{scenario.name}' defines applications for unknown session identities "
                    f"{unknown_identities}."
                )
            return InteractiveSmokeTarget(
                name=scenario.name,
                target_type="scenario",
                session=session,
                cfg=cfg,
                scenario=scenario,
                scenario_definition=definition,
            )
        except Exception as exc:  # noqa: BLE001 - auto falls back to a session target.
            scenario_error = exc
            if target_type == "scenario":
                raise

    try:
        session = _resolve_session(target_name, runtime)
        cfg = _effective_session_config(session.host_dir, runtime)
        _require_two_peer_smoke_cfg(cfg, session.host_dir.name)
        return InteractiveSmokeTarget(name=session.host_dir.name, target_type="session", session=session, cfg=cfg)
    except Exception as exc:
        if scenario_error is not None:
            raise scenario_error from exc
        raise


def _interactive_smoke_tmux_session(target_type: str, target_name: str) -> str:
    return _safe_path_token(f"smoke-{target_type}-{target_name}")


def _active_interactive_smoke_runs(runtime: RuntimeConfig) -> list[ActiveInteractiveSmokeRun]:
    if not shutil.which("tmux"):
        return []
    result = subprocess.run(
        _tmux_command(
            runtime,
            "list-sessions",
            "-F",
            "#{session_name}\t#{@rosotacom_smoke_target}\t#{@rosotacom_smoke_target_type}\t"
            "#{@rosotacom_smoke_instance}\t#{@rosotacom_smoke_network}",
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    runs: list[ActiveInteractiveSmokeRun] = []
    for line in result.stdout.splitlines():
        session_name, _, metadata = line.partition("\t")
        target, _, rest = metadata.partition("\t")
        target_type, _, rest = rest.partition("\t")
        instance_id, _, network_name = rest.partition("\t")
        if target and target_type and instance_id:
            runs.append(
                ActiveInteractiveSmokeRun(
                    target=target,
                    target_type=target_type,
                    tmux_session=session_name,
                    instance_id=instance_id,
                    network_name=network_name,
                )
            )
    return sorted(runs, key=lambda run: (run.target_type, run.target))


def _format_active_interactive_smoke_runs(runs: list[ActiveInteractiveSmokeRun]) -> str:
    if not runs:
        return "Active interactive smoke runs:\n  (none)"
    lines = ["Active interactive smoke runs:"]
    for run in runs:
        lines.append(f"  - {run.target} ({run.target_type}) instance={run.instance_id} network={run.network_name}")
    return "\n".join(lines)


def _infer_active_interactive_smoke_run(
    runtime: RuntimeConfig,
    target_arg: str | None,
    target_type: str,
) -> ActiveInteractiveSmokeRun:
    runs = _active_interactive_smoke_runs(runtime)
    if target_arg:
        target = _resolve_interactive_smoke_target(target_arg, runtime, target_type)
        matches = [run for run in runs if run.target == target.name and run.target_type == target.target_type]
    elif target_type != "auto":
        matches = [run for run in runs if run.target_type == target_type]
    else:
        matches = runs
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError("No active interactive smoke run found.")
    raise RuntimeError(
        "Multiple active interactive smoke runs found; specify TARGET:\n"
        + "\n".join(f"  - {run.target} ({run.target_type})" for run in matches)
    )


def _ota_plan_from_bindings(bindings: dict[str, PeerBinding], *, workdir: str) -> OtaSmokePlan:
    _ota_validate_prepare_workdir(workdir)
    peers = {
        name: OtaSmokePeer(name=name, ssh=binding.ssh, address=binding.address) for name, binding in bindings.items()
    }
    return OtaSmokePlan(
        state_path=None,
        workdir=workdir,
        rosotacom="source/.venv/bin/rosotacom",
        project="project/rosotacom.yaml",
        peers=peers,
    )


def _ota_write_state(instance: SessionInstance, plan: OtaSmokePlan) -> OtaSmokePlan:
    path = instance.host_dir / "ota-deployment.yaml"
    payload = {
        "schema_version": 1,
        "workdir": plan.workdir,
        "rosotacom": plan.rosotacom,
        "project": plan.project,
        "peers": {name: {"address": peer.address, "ssh": peer.ssh} for name, peer in sorted(plan.peers.items())},
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return replace(plan, state_path=path)


def _ota_load_state(path_arg: str) -> OtaSmokePlan:
    path = Path(path_arg).expanduser().resolve()
    raw = _load_yaml_file(path)
    if raw.get("schema_version") != 1:
        raise RuntimeError(f"Unsupported OTA deployment state: {path}")
    peers_raw = raw.get("peers")
    if not isinstance(peers_raw, dict) or len(peers_raw) != 2:
        raise RuntimeError(f"OTA deployment state must define exactly two peers: {path}")
    peers: dict[str, OtaSmokePeer] = {}
    for name, peer_raw in peers_raw.items():
        if not isinstance(peer_raw, dict):
            raise RuntimeError(f"OTA deployment peer '{name}' must be a mapping.")
        peers[str(name)] = OtaSmokePeer(
            name=str(name),
            address=str(peer_raw["address"]),
            ssh=str(peer_raw["ssh"]) if peer_raw.get("ssh") else None,
        )
    return OtaSmokePlan(
        state_path=path,
        workdir=str(raw["workdir"]),
        rosotacom=str(raw["rosotacom"]),
        project=str(raw["project"]),
        peers=peers,
    )


def _ota_peer_address_args(plan: OtaSmokePlan) -> list[str]:
    return [f"{name}={peer.address}" for name, peer in plan.peers.items()]


def _ota_validate_prepare_workdir(workdir: str) -> None:
    raw = workdir.strip()
    if raw in {"", "/", ".", "~", "$HOME", "${HOME}"}:
        raise RuntimeError(f"Refusing dangerous OTA prepare workdir: {workdir!r}")
    if raw.startswith("-"):
        raise RuntimeError(f"Refusing OTA prepare workdir that looks like an option: {workdir!r}")
    if raw.startswith("/") and len(Path(raw).parts) <= 2:
        raise RuntimeError(f"Refusing broad top-level OTA prepare workdir: {workdir!r}")


def _ota_quote_cmd(parts: list[str]) -> str:
    return shlex.join(parts)


def _ota_remote_argv(peer: OtaSmokePeer, script: str, *, tty: bool = False, batch: bool = False) -> list[str]:
    if peer.ssh:
        argv = ["ssh"]
        if batch:
            argv.extend(["-o", "BatchMode=yes"])
        if tty:
            argv.append("-t")
        argv.extend([peer.ssh, script])
        return argv
    return ["bash", "-lc", script]


def _ota_run(
    peer: OtaSmokePeer,
    script: str,
    *,
    label: str,
    dry_run: bool = False,
    check: bool = True,
    tty: bool = False,
    batch: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = _ota_remote_argv(peer, script, tty=tty, batch=batch)
    print(f"+ {label}: running remote command")
    if dry_run:
        return subprocess.CompletedProcess(argv, 0, "", "")
    result = subprocess.run(argv, text=True, capture_output=not tty, check=False, timeout=timeout)
    if check and result.returncode != 0:
        detail = ((result.stdout or "") + (result.stderr or "")).strip()
        raise RuntimeError(f"{label} failed with exit code {result.returncode}:\n{detail}")
    return result


def _ota_run_with_secret_stdin(
    peer: OtaSmokePeer,
    script: str,
    *,
    label: str,
    secret_stdin: str,
    dry_run: bool = False,
    check: bool = True,
    batch: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = _ota_remote_argv(peer, script, batch=batch)
    if dry_run:
        return subprocess.CompletedProcess(argv, 0, "", "")
    result = subprocess.run(argv, input=secret_stdin, text=True, capture_output=True, check=False, timeout=timeout)
    if check and result.returncode != 0:
        detail = ((result.stdout or "") + (result.stderr or "")).strip()
        raise RuntimeError(f"{label} failed with exit code {result.returncode}:\n{detail}")
    return result


def _ota_log_action(label: str, detail: str) -> None:
    print(f"+ {label}: {detail}")


def _ota_print_failure_output(label: str, result: subprocess.CompletedProcess[str]) -> None:
    """Echo a failed verify step's captured output so OTA smoke failures are
    diagnosable. ``_ota_run`` captures stdout/stderr for non-tty steps and, for
    ``check=False`` verify calls, never surfaces them on its own."""
    detail = ((result.stdout or "") + (result.stderr or "")).strip()
    if detail:
        print(f"--- {label} output (exit {result.returncode}) ---", file=sys.stderr)
        print(detail, file=sys.stderr)
        print(f"--- end {label} output ---", file=sys.stderr)


def _print_completed_output(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)


def _ota_project_cli_args(plan: OtaSmokePlan) -> list[str]:
    return ["--rosotacom-config", plan.project]


def _ota_rosotacom_command(plan: OtaSmokePlan, parts: list[str]) -> str:
    command = [plan.rosotacom, *parts, *_ota_project_cli_args(plan)]
    return f"cd {shlex.quote(plan.workdir)} && {_ota_quote_cmd(command)}"


def _ota_source_checkout() -> Path | None:
    for candidate in (PACKAGE_DIR.parents[2], *PACKAGE_DIR.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "install.sh").is_file():
            return candidate.resolve()
    return None


def _ota_packaged_source_bundle(destination: Path) -> Path:
    source_root = destination / "rosotacom-source"
    package_target = source_root / "src" / "rosotacom"
    package_target.parent.mkdir(parents=True)
    shutil.copytree(PACKAGE_DIR, package_target)
    (source_root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[build-system]",
                'requires = ["setuptools>=80", "wheel"]',
                'build-backend = "setuptools.build_meta"',
                "",
                "[project]",
                'name = "rosotacom"',
                f'version = "{__version__}"',
                'requires-python = ">=3.10"',
                'dependencies = ["argcomplete>=3.6,<4", "PyYAML>=6", "ros2docker>=0.1.3,<0.2"]',
                "",
                "[project.scripts]",
                'rosotacom = "rosotacom.cli:main"',
                'start_rosotacom = "rosotacom.cli:start_compat_main"',
                'stop_rosotacom = "rosotacom.cli:stop_compat_main"',
                "",
                "[tool.setuptools]",
                'package-dir = {"" = "src"}',
                "include-package-data = true",
                "",
                "[tool.setuptools.packages.find]",
                'where = ["src"]',
                "",
                "[tool.setuptools.package-data]",
                'rosotacom = ["py.typed", "resources/**/*"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return source_root


def _ota_target_session_arg(target: InteractiveSmokeTarget) -> str:
    if target.target_type == "scenario" and target.scenario_definition is not None:
        return target.scenario_definition.session
    return target.name


def _ota_start_parts(
    target: InteractiveSmokeTarget,
    identity: str,
    instance_id: str,
    peer_args: list[str],
    *,
    mode: str,
    force: bool = True,
    link_trace_parts: list[str] | None = None,
) -> list[str]:
    if target.target_type == "scenario":
        parts = ["scenario", "start", target.name, "--identity", identity, "--mode", mode, "--instance-id", instance_id]
    else:
        parts = [
            "start",
            _ota_target_session_arg(target),
            "--identity",
            identity,
            "--mode",
            mode,
            "--instance-id",
            instance_id,
        ]
    parts.append("--force" if force else "--no-force")
    parts.extend(link_trace_parts or [])
    for override in peer_args:
        parts.extend(["--peer-address", override])
    return parts


def _ota_communication_start_parts(
    target: InteractiveSmokeTarget,
    identity: str,
    instance_id: str,
    peer_args: list[str],
    *,
    mode: str,
    force: bool = True,
    link_trace_parts: list[str] | None = None,
) -> list[str]:
    parts = [
        "start",
        _ota_target_session_arg(target),
        "--identity",
        identity,
        "--mode",
        mode,
        "--instance-id",
        instance_id,
        "--smoke-managed",
    ]
    parts.append("--force" if force else "--no-force")
    parts.extend(link_trace_parts or [])
    for override in peer_args:
        parts.extend(["--peer-address", override])
    return parts


def _link_trace_parts_from_args(args: argparse.Namespace) -> list[str]:
    parts: list[str] = []
    if getattr(args, "link_trace", None):
        parts.append("--link-trace")
    if getattr(args, "link_trace_interval_s", None) is not None:
        parts.extend(["--link-trace-interval", str(args.link_trace_interval_s)])
    if getattr(args, "link_trace_modem_command", None):
        parts.extend(["--link-trace-modem-command", args.link_trace_modem_command])
    return parts


def _ota_application_parts(
    target: InteractiveSmokeTarget,
    identity: str,
    application: ScenarioApplication,
    instance_id: str,
) -> list[str]:
    return [
        "scenario",
        "_run-application",
        target.name,
        "--identity",
        identity,
        "--application",
        application.name,
        "--instance-id",
        instance_id,
    ]


def _ota_stop_parts(
    target: InteractiveSmokeTarget,
    identity: str,
    instance_id: str | None,
    peer_args: list[str],
) -> list[str]:
    if target.target_type == "scenario":
        parts = ["scenario", "stop", target.name, "--identity", identity]
        if instance_id:
            parts.extend(["--instance-id", instance_id])
    else:
        parts = ["stop", _ota_target_session_arg(target), "--identity", identity]
    for override in peer_args:
        parts.extend(["--peer-address", override])
    return parts


def _ota_publish_parts(
    target: InteractiveSmokeTarget,
    identity: str,
    peer_args: list[str],
    *,
    stop: bool = False,
) -> list[str]:
    parts = [
        "publish-test-topics",
        _ota_target_session_arg(target),
        "--identity",
        identity,
        "--duration",
        str(int(SMOKE_PUBLISHER_DURATION_S)),
    ]
    if stop:
        parts.append("--stop")
    for override in peer_args:
        parts.extend(["--peer-address", override])
    return parts


def _ota_test_parts(target: InteractiveSmokeTarget, instance_id: str, *, profile: str | None = None) -> list[str]:
    parts = [
        "test",
        _ota_target_session_arg(target),
        "--instance-id",
        instance_id,
        "--timeout",
        "120",
        "--interval",
        "2",
    ]
    if profile:
        parts += ["--profile", profile]  # assert P's conditional band (RFC 0004)
    return parts


def _ota_probe_publish_parts(
    target: InteractiveSmokeTarget,
    identity: str,
    peer_args: list[str],
    *,
    stop: bool,
) -> list[str]:
    parts = [
        "probe-publish",
        _ota_target_session_arg(target),
        "--identity",
        identity,
        "--topic",
        ISOLATION_PROBE_TOPIC,
    ]
    if stop:
        parts.append("--stop")
    for override in peer_args:
        parts.extend(["--peer-address", override])
    return parts


def _ota_probe_check_parts(target: InteractiveSmokeTarget, identity: str, peer_args: list[str]) -> list[str]:
    parts = [
        "probe-check",
        _ota_target_session_arg(target),
        "--identity",
        identity,
        "--topic",
        ISOLATION_PROBE_TOPIC,
        "--expect",
        "absent",
    ]
    for override in peer_args:
        parts.extend(["--peer-address", override])
    return parts


def _ota_content_check_parts(
    target: InteractiveSmokeTarget,
    identity: str,
    peer_args: list[str],
    topic: str,
    msg_type: str,
    field: str,
    expected: str,
) -> list[str]:
    parts = [
        "probe-content",
        _ota_target_session_arg(target),
        "--identity",
        identity,
        "--topic",
        topic,
        "--type",
        msg_type,
        "--field",
        field,
        "--expect",
        expected,
    ]
    for override in peer_args:
        parts.extend(["--peer-address", override])
    return parts


def _ota_write_manifest(
    instance: SessionInstance,
    target: InteractiveSmokeTarget,
    runtime: RuntimeConfig,
    plan: OtaSmokePlan,
    *,
    tmux_session: str | None,
    interactive: bool,
    phase: str,
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
            "source_session_host_dir": str(target.session.host_dir),
            "source_session_container_dir": target.session.container_dir,
            "source": target.session.source,
            "config_dir": str(instance.config_host_dir),
            "logs_dir": str(instance.logs_host_dir),
            "rosbags_dir": str(instance.rosbags_host_dir),
            "rollout": None,
            "starts": [],
        }
    manifest["updated_at"] = now
    run_key = _interactive_smoke_run_key(target.target_type, target.name)
    runs = manifest.setdefault("ota_smoke_runs", {})
    run = runs.setdefault(
        run_key,
        {
            "started_at": now,
            "stopped_at": None,
            "target": target.name,
            "target_type": target.target_type,
            "interactive": interactive,
            "deployment_state": str(plan.state_path) if plan.state_path else None,
            "workdir": plan.workdir,
            "project": plan.project,
            "peers": {
                name: {"ssh_configured": bool(peer.ssh), "address": peer.address} for name, peer in plan.peers.items()
            },
        },
    )
    run["phase"] = phase
    run["updated_at"] = now
    run["tmux_socket"] = _scenario_tmux_socket(runtime) if tmux_session else None
    run["tmux_session"] = tmux_session
    if phase == "stopped":
        run["stopped_at"] = now
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=True, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


def _ota_smoke_tmux_session(target_type: str, target_name: str) -> str:
    return _safe_path_token(f"ota-smoke-{target_type}-{target_name}")


def _active_ota_smoke_runs(runtime: RuntimeConfig) -> list[ActiveOtaSmokeRun]:
    if not shutil.which("tmux"):
        return []
    result = subprocess.run(
        _tmux_command(
            runtime,
            "list-sessions",
            "-F",
            "#{session_name}\t#{@rosotacom_ota_smoke_target}\t#{@rosotacom_ota_smoke_target_type}\t"
            "#{@rosotacom_ota_smoke_instance}\t#{@rosotacom_ota_smoke_state}",
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    runs: list[ActiveOtaSmokeRun] = []
    for line in result.stdout.splitlines():
        session_name, _, metadata = line.partition("\t")
        target, _, rest = metadata.partition("\t")
        target_type, _, rest = rest.partition("\t")
        instance_id, _, state_path = rest.partition("\t")
        if target and target_type and instance_id and state_path:
            runs.append(
                ActiveOtaSmokeRun(
                    target=target,
                    target_type=target_type,
                    tmux_session=session_name,
                    instance_id=instance_id,
                    state_path=state_path,
                )
            )
    return sorted(runs, key=lambda run: (run.target_type, run.target))


def _format_active_ota_smoke_runs(runs: list[ActiveOtaSmokeRun]) -> str:
    if not runs:
        return "Active OTA smoke runs:\n  (none)"
    lines = ["Active OTA smoke runs:"]
    for run in runs:
        lines.append(f"  - {run.target} ({run.target_type}) instance={run.instance_id}")
    return "\n".join(lines)


def _manifest_ota_smoke_runs(runtime: RuntimeConfig) -> list[ActiveOtaSmokeRun]:
    root = _session_instances_root(runtime)
    runs: list[ActiveOtaSmokeRun] = []
    for manifest_path in sorted(root.glob("*/*/manifest.yaml"), reverse=True):
        manifest = _load_yaml_file(manifest_path)
        instance_id = str(manifest.get("instance_id") or manifest_path.parent.name.rsplit("_", 1)[-1])
        for run in (manifest.get("ota_smoke_runs") or {}).values():
            if not isinstance(run, dict):
                continue
            if run.get("stopped_at") or run.get("phase") == "stopped":
                continue
            target = str(run.get("target") or "")
            target_type = str(run.get("target_type") or "")
            state_path = str(run.get("deployment_state") or "")
            if not target or not target_type or not state_path:
                continue
            tmux_session = str(run.get("tmux_session") or _ota_smoke_tmux_session(target_type, target))
            runs.append(
                ActiveOtaSmokeRun(
                    target=target,
                    target_type=target_type,
                    tmux_session=tmux_session,
                    instance_id=instance_id,
                    state_path=state_path,
                )
            )
    return sorted(runs, key=lambda run: (run.target_type, run.target, run.instance_id))


def _infer_active_ota_smoke_run(
    runtime: RuntimeConfig,
    target_arg: str | None,
    target_type: str,
    instance_id: str | None = None,
) -> ActiveOtaSmokeRun:
    runs = _active_ota_smoke_runs(runtime)
    seen = {(run.target, run.target_type, run.instance_id) for run in runs}
    runs.extend(
        run for run in _manifest_ota_smoke_runs(runtime) if (run.target, run.target_type, run.instance_id) not in seen
    )
    if instance_id:
        instance_token = _safe_path_token(instance_id)
        runs = [run for run in runs if run.instance_id == instance_token]
    if target_arg:
        target = _resolve_interactive_smoke_target(target_arg, runtime, target_type)
        matches = [run for run in runs if run.target == target.name and run.target_type == target.target_type]
    elif target_type != "auto":
        matches = [run for run in runs if run.target_type == target_type]
    else:
        matches = runs
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError("No active OTA smoke run found.")
    raise RuntimeError(
        "Multiple active OTA smoke runs found; specify TARGET:\n"
        + "\n".join(f"  - {run.target} ({run.target_type})" for run in matches)
    )


def _resolve_ota_smoke_context(
    args: argparse.Namespace,
) -> tuple[RuntimeConfig, OtaSmokePlan, InteractiveSmokeTarget]:
    runtime = _load_runtime_config(args)
    target = _resolve_interactive_smoke_target(
        getattr(args, "target", None), runtime, getattr(args, "target_type", "auto")
    )
    state_file = getattr(args, "state_file", None)
    if state_file:
        plan = _ota_load_state(state_file)
    else:
        bindings = _resolve_bindings(
            target.cfg,
            runtime,
            peer=getattr(args, "peer", None),
            peer_address=getattr(args, "peer_address", None),
            peer_ssh=getattr(args, "peer_ssh", None),
        )
        plan = _ota_plan_from_bindings(
            bindings,
            workdir=str(getattr(args, "workdir", None) or "/tmp/rosotacom_ota"),
        )
    plan_peers = set(plan.peers)
    target_peers = set(_peer_keys_from_cfg(target.cfg))
    if plan_peers != target_peers:
        raise RuntimeError(
            "OTA smoke peers must match the target session peers: "
            f"deployment={sorted(plan_peers)} target={sorted(target_peers)}"
        )
    return runtime, plan, target


def _validate_ota_sudo_mode(sudo_mode: str) -> str:
    if sudo_mode not in OTA_SUDO_MODES:
        raise RuntimeError(f"Unsupported OTA sudo mode {sudo_mode!r}; expected one of {', '.join(OTA_SUDO_MODES)}.")
    return sudo_mode


def _ota_network_sudo_passwords(
    plan: OtaSmokePlan,
    *,
    sudo_mode: str,
    require_network_shaping_sudo: bool,
    dry_run: bool,
) -> dict[str, str]:
    sudo_mode = _validate_ota_sudo_mode(sudo_mode)
    if not require_network_shaping_sudo or sudo_mode != "askpass":
        return {}
    if dry_run:
        return {peer.name: "" for peer in plan.peers.values()}
    passwords: dict[str, str] = {}
    for peer in plan.peers.values():
        target = peer.ssh or "local shell"
        passwords[peer.name] = getpass.getpass(f"sudo password for {peer.name} ({target}) [network shaping only]: ")
    return passwords


def _sudo_stdin(sudo_password: str | None) -> str | None:
    return None if sudo_password is None else sudo_password + "\n"


def _sudo_command(argv: Sequence[str], *, askpass: bool = False) -> str:
    mode = "sudo -S -p ''" if askpass else "sudo -n"
    return f"{mode} {shlex.join(list(argv))}"


def _passwordless_watchdog_command(argv: Sequence[str]) -> str:
    argv = list(argv)
    if len(argv) >= 3 and argv[0] == "sh" and argv[1] == "-c":
        script = str(argv[2])
        script = script.replace("; tc ", "; sudo -n tc ").replace("; ip ", "; sudo -n ip ")
        if script.startswith("tc "):
            script = "sudo -n " + script
        if script.startswith("ip "):
            script = "sudo -n " + script
        return f"nohup sh -c {shlex.quote(script)} >/dev/null 2>&1 &"
    return f"nohup sudo -n {shlex.join(argv)} >/dev/null 2>&1 &"


def _ota_conflict_check(plan: OtaSmokePlan, *, dry_run: bool) -> None:
    """Fail-safe preflight: OTA runs require exclusive control of the peers.

    Aborts when a peer already runs rosotacom containers or has active network
    shaping (netem/tbf/htb) on the data interface toward another peer — both
    signal an OTA run that is still active or was not cleaned up.
    """
    from .network_shaper import ota_interface_from_route

    for peer in plan.peers.values():
        result = _ota_run(
            peer,
            "docker ps --format '{{.Names}}'",
            label=f"{peer.name}: scan for active rosotacom containers",
            dry_run=dry_run,
            batch=True,
        )
        active = [line.strip() for line in (result.stdout or "").splitlines() if line.strip().startswith("rosotacom_")]
        if active:
            raise _conflict_error(
                f"Peer {peer.name} already runs rosotacom containers; OTA runs need exclusive hosts.",
                active,
                "Stop the active OTA run first (rosotacom ota-smoke <target> --stop) or pass --skip-conflict-check.",
            )
        for other in plan.peers.values():
            if other.name == peer.name:
                continue
            route = _ota_run(
                peer,
                f"ip route get {shlex.quote(other.address)} 2>/dev/null | head -n 1",
                label=f"{peer.name}: resolve data interface toward {other.name}",
                dry_run=dry_run,
                batch=True,
                check=False,
            )
            try:
                interface = ota_interface_from_route(route.stdout or "")
            except ValueError:
                continue
            qdisc = _ota_run(
                peer,
                f"tc qdisc show dev {shlex.quote(interface)}",
                label=f"{peer.name}: check for active network shaping on {interface}",
                dry_run=dry_run,
                batch=True,
                check=False,
            )
            shaped = [
                line.strip() for line in (qdisc.stdout or "").splitlines() if re.search(r"\b(netem|tbf|htb)\b", line)
            ]
            if shaped:
                raise RuntimeError(
                    f"Active network shaping detected on {peer.name} ({interface}); another OTA run may be active:\n"
                    + "\n".join(f"  {line}" for line in shaped)
                    + f"\nStop the run that applied it, clear it (sudo tc qdisc del dev {interface} root),"
                    " or pass --skip-conflict-check."
                )


def _ota_preflight(
    plan: OtaSmokePlan,
    *,
    require_tmux: bool,
    check_peer_reachability: bool,
    dry_run: bool,
    require_network_shaping_sudo: bool = False,
    sudo_mode: str = OTA_DEFAULT_SUDO_MODE,
    sudo_passwords: Mapping[str, str] | None = None,
    check_conflicts: bool = True,
) -> None:
    sudo_mode = _validate_ota_sudo_mode(sudo_mode)
    sudo_passwords = sudo_passwords or {}
    for peer in plan.peers.values():
        if peer.ssh:
            _ota_run(peer, "true", label=f"{peer.name}: SSH reachable", dry_run=dry_run, batch=True)
        required = ["python3", "docker", "tar"]
        if require_tmux:
            required.append("tmux")
        for command in required:
            _ota_run(
                peer,
                f"command -v {shlex.quote(command)} >/dev/null 2>&1",
                label=f"{peer.name}: required command {command}",
                dry_run=dry_run,
                batch=True,
            )
        _ota_run(
            peer,
            'tmp="$(mktemp -d)" && python3 -m venv "$tmp/venv" >/dev/null 2>&1; rc=$?; rm -rf "$tmp"; exit "$rc"',
            label=f"{peer.name}: Python venv support",
            dry_run=dry_run,
            batch=True,
        )
        _ota_run(peer, "docker ps >/dev/null", label=f"{peer.name}: docker access", dry_run=dry_run, batch=True)
        if require_network_shaping_sudo:
            _ota_run(
                peer,
                "command -v tc >/dev/null 2>&1 && command -v ip >/dev/null 2>&1",
                label=f"{peer.name}: required commands for network shaping",
                dry_run=dry_run,
                batch=True,
            )
            if sudo_mode == "askpass":
                script = "sudo -S -p '' true"
                label = f"{peer.name}: sudo authentication for network shaping"
                _ota_log_action(label, "authenticating sudo via stdin")
                _ota_run_with_secret_stdin(
                    peer,
                    script,
                    label=label,
                    dry_run=dry_run,
                    batch=True,
                    secret_stdin=_sudo_stdin(sudo_passwords.get(peer.name)) or "",
                )
            else:
                _ota_run(
                    peer,
                    "sudo -n tc qdisc show >/dev/null && sudo -n ip link show >/dev/null",
                    label=f"{peer.name}: passwordless sudo for network shaping",
                    dry_run=dry_run,
                    batch=True,
                )

    if check_peer_reachability:
        for src in plan.peers.values():
            for dst in plan.peers.values():
                if src.name == dst.name:
                    continue
                _ota_run(
                    src,
                    "output=$("
                    f"ping -c3 -W2 {shlex.quote(dst.address)} 2>&1 || true"
                    '); printf "%s\\n" "$output"; '
                    'printf "%s\\n" "$output" | grep -Eq "[1-9][0-9]* received"',
                    label=f"{src.name}: reach peer {dst.name} ({dst.address})",
                    dry_run=dry_run,
                    batch=True,
                )

    if check_conflicts:
        _ota_conflict_check(plan, dry_run=dry_run)


_OTA_STAGE_EXCLUDES = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "session-instances",
}


def _ota_copytree_local(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(*_OTA_STAGE_EXCLUDES, "*.pyc"),
    )


def _ota_stage_tree(peer: OtaSmokePeer, source: Path, destination: str, *, dry_run: bool, label: str) -> None:
    destination_q = shlex.quote(destination)
    exclude_args = [f"--exclude={name}" for name in sorted(_OTA_STAGE_EXCLUDES)]
    command = [
        "tar",
        *exclude_args,
        "--exclude=*.pyc",
        "-C",
        str(source),
        "-czf",
        "-",
        ".",
    ]
    if not peer.ssh:
        print(f"+ {label}: copy {source} -> {destination}")
        if not dry_run:
            _ota_copytree_local(source, Path(destination))
        return

    remote_script = f"rm -rf {destination_q} && mkdir -p {destination_q} && tar -xzf - -C {destination_q}"
    remote = ["ssh", peer.ssh, remote_script]
    print(f"+ {label}: {_ota_quote_cmd(command)} | {_ota_quote_cmd(remote)}")
    if dry_run:
        return
    producer = subprocess.Popen(command, stdout=subprocess.PIPE)
    assert producer.stdout is not None
    consumer = subprocess.run(remote, stdin=producer.stdout, check=False)
    producer.stdout.close()
    producer_rc = producer.wait()
    if producer_rc != 0 or consumer.returncode != 0:
        raise RuntimeError(f"{label} failed: tar exit={producer_rc}, remote extract exit={consumer.returncode}")


def _ota_stage_text(peer: OtaSmokePeer, text: str, destination: str, *, dry_run: bool, label: str) -> None:
    print(f"+ {label}: write {destination} on {peer.ssh or 'local host'}")
    if dry_run:
        return
    if peer.ssh:
        parent = shlex.quote(str(Path(destination).parent))
        command = ["ssh", peer.ssh, f"mkdir -p {parent} && cat > {shlex.quote(destination)}"]
        result = subprocess.run(command, input=text, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"{label} failed with exit code {result.returncode}")
    else:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _ota_remap_config_roots(
    raw: Any, key: str, project_root: Path, source_checkout: Path | None, staged_source: str
) -> Any:
    """Rewrite config-root paths so they resolve on the remote staged tree.

    - relative roots resolve under the staged project, so keep them verbatim;
    - absolute roots inside the project become relative (staged with the project);
    - absolute roots inside the staged rosotacom source point at the remote source
      path (``{workdir}/source/...``), since the examples are staged there;
    - anything else is left untouched (it cannot be staged automatically and will
      surface as a clear "Path does not exist" on the remote).
    """
    values = list(_path_values(raw, key))
    scalar = not isinstance(raw, (list, tuple)) and not (isinstance(raw, str) and "," in raw)
    remapped: list[str] = []
    for value in values:
        path = Path(os.path.expandvars(os.path.expanduser(str(value))))
        if not path.is_absolute():
            remapped.append(str(value))
            continue
        path = path.resolve()
        rel_to_project = _relative_to(path, project_root)
        if rel_to_project is not None:
            remapped.append(rel_to_project.as_posix())
            continue
        rel_to_source = _relative_to(path, source_checkout) if source_checkout else None
        if rel_to_source is not None:
            remapped.append(f"{staged_source}/{rel_to_source.as_posix()}")
            continue
        remapped.append(str(value))
    if scalar:
        return remapped[0] if remapped else raw
    return remapped


def _ota_remote_project_config(runtime: RuntimeConfig, plan: OtaSmokePlan, source_checkout: Path | None) -> str:
    if not runtime.rosotacom_config:
        raise RuntimeError("ota-smoke requires an active rosotacom.yaml project.")
    raw = _load_yaml_file(runtime.rosotacom_config)
    project_root = runtime.rosotacom_config.parent
    if runtime.deployment and _relative_to(runtime.deployment, project_root) is None:
        raw["deployment"] = ".rosotacom/deployment.yaml"
    if runtime.profiles_file and _relative_to(runtime.profiles_file, project_root) is None:
        raw["profiles"] = ".rosotacom/profiles.yaml"
    staged_source = f"{plan.workdir}/source"
    for key in ("session_configs_dir", "scenario_configs_dir"):
        if raw.get(key) is not None:
            raw[key] = _ota_remap_config_roots(raw[key], key, project_root, source_checkout, staged_source)
    return yaml.safe_dump(raw, sort_keys=False)


def _ota_prepare_hosts(args: argparse.Namespace, runtime: RuntimeConfig, plan: OtaSmokePlan) -> None:
    if not runtime.rosotacom_config:
        raise RuntimeError("ota-smoke requires an active rosotacom.yaml project.")
    dry_run = bool(getattr(args, "dry_run", False))
    source_checkout = _ota_source_checkout()
    temporary_bundle: Path | None = None
    if source_checkout is None:
        temporary_bundle = Path(tempfile.mkdtemp(prefix="rosotacom-ota-source-"))
        source_checkout = _ota_packaged_source_bundle(temporary_bundle)
    reuse = bool(getattr(args, "reuse", False))
    try:
        for peer in plan.peers.values():
            if not reuse:
                _ota_stage_tree(
                    peer,
                    source_checkout,
                    f"{plan.workdir}/source",
                    dry_run=dry_run,
                    label=f"{peer.name}: stage rosotacom source",
                )
                source_dir = shlex.quote(plan.workdir + "/source")
                if (source_checkout / "install.sh").is_file():
                    install = f"cd {source_dir} && ./install.sh"
                else:
                    install = (
                        f"cd {source_dir} && python3 -m venv .venv && "
                        ".venv/bin/python -m pip install --upgrade pip && "
                        ".venv/bin/python -m pip install -e ."
                    )
                _ota_run(peer, install, label=f"{peer.name}: install rosotacom", dry_run=dry_run)
            else:
                _ota_run(
                    peer,
                    f"test -x {shlex.quote(plan.workdir + '/' + plan.rosotacom)}",
                    label=f"{peer.name}: reuse rosotacom",
                    dry_run=dry_run,
                )

            project_root = runtime.rosotacom_config.parent
            _ota_stage_tree(
                peer,
                project_root,
                f"{plan.workdir}/project",
                dry_run=dry_run,
                label=f"{peer.name}: stage project",
            )
            _ota_stage_text(
                peer,
                _ota_remote_project_config(runtime, plan, source_checkout),
                f"{plan.workdir}/{plan.project}",
                dry_run=dry_run,
                label=f"{peer.name}: write staged project config",
            )
            if runtime.deployment and _relative_to(runtime.deployment, project_root) is None:
                _ota_stage_text(
                    peer,
                    runtime.deployment.read_text(encoding="utf-8"),
                    f"{plan.workdir}/project/.rosotacom/deployment.yaml",
                    dry_run=dry_run,
                    label=f"{peer.name}: stage deployment",
                )
            if runtime.profiles_file and _relative_to(runtime.profiles_file, project_root) is None:
                _ota_stage_text(
                    peer,
                    runtime.profiles_file.read_text(encoding="utf-8"),
                    f"{plan.workdir}/project/.rosotacom/profiles.yaml",
                    dry_run=dry_run,
                    label=f"{peer.name}: stage profiles",
                )
    finally:
        if temporary_bundle is not None:
            shutil.rmtree(temporary_bundle, ignore_errors=True)


# --- RFC 0004 network-profile arming (bench-only; privileged tc/netem) ------ #

# A watchdog reverts a crashed run's shaping within this bound, so a killed OTA
# smoke can never leave a qdisc corrupting later results.
_PROFILE_SAFETY_MAX_S = 3600.0


def _resolve_ota_profile(
    runtime: RuntimeConfig, target: InteractiveSmokeTarget, args: argparse.Namespace
) -> tuple[str | None, Any]:
    """Resolve the selected static network profile for an OTA run (RFC 0004).

    Returns ``(name, Profile)``, or ``(None, None)`` when unshaped. ota-smoke is the
    bench tool, so shaping is allowed; a timeline profile is rejected here — its
    stepping is the RFC 0005 recovery driver's job, not a one-shot smoke."""
    name = _resolve_active_profile(runtime, target.cfg, args, allow_shaping=True)
    if name is None:
        return None, None
    if runtime.profiles_file is None:
        raise RuntimeError(
            f"--profile {name!r} needs a profiles file: set 'profiles:' in rosotacom.yaml or pass --profiles-file."
        )
    from .network_profiles import load_profiles_file

    profile = load_profiles_file(runtime.profiles_file)[name]
    if profile.is_timeline and not getattr(args, "benchmark_stepping", False):
        raise RuntimeError(
            f"profile {name!r} is a timeline profile; ota-smoke applies a static condition only. "
            "Timeline / recovery runs are driven by the benchmark recovery genre (RFC 0005)."
        )
    return name, profile


def _profile_directions(plan: OtaSmokePlan, cfg: dict[str, Any]) -> dict[str, str]:
    """Map each peer to the profile direction its egress carries (RFC 0004).

    Default for the a/b convention: the first peer (center / receiver) shapes
    ``downlink``, the second (vehicle / sender) shapes ``uplink`` — the dominant
    telemetry direction. Override with ``shared.profile_directions: { a: …, b: … }``."""
    peers = sorted(plan.peers)
    directions: dict[str, str] = {peers[0]: "downlink", peers[1]: "uplink"} if len(peers) == 2 else {}
    shared = cfg.get("shared")
    override = shared.get("profile_directions") if isinstance(shared, dict) else None
    if isinstance(override, dict):
        for peer_name, direction in override.items():
            directions[str(peer_name)] = str(direction)
    invalid = {peer: direction for peer, direction in directions.items() if direction not in ("uplink", "downlink")}
    if invalid:
        raise RuntimeError(f"shared.profile_directions values must be 'uplink' or 'downlink'; got {invalid}")
    return directions


def _ota_resolve_interfaces(peer: OtaSmokePeer, peer_addr: str, *, dry_run: bool) -> tuple[str, str | None]:
    """Discover ``(OTA egress, control)`` interfaces on ``peer`` from the kernel, not
    guesses: the OTA interface is the route to the other peer's data address; the
    control interface is the route back to the SSH client, so it is never shaped."""
    from .network_shaper import ota_interface_from_route

    script = (
        f"ip route get {shlex.quote(peer_addr)}; echo '---CTRL---'; "
        '{ [ -n "$SSH_CONNECTION" ] && ip route get "${SSH_CONNECTION%% *}"; } || true'
    )
    result = _ota_run(peer, script, label=f"{peer.name}: resolve OTA + control interface", dry_run=dry_run)
    ota_text, _, ctrl_text = (result.stdout or "").partition("---CTRL---")
    ota_iface = ota_interface_from_route(ota_text)
    try:
        control_iface: str | None = ota_interface_from_route(ctrl_text)
    except ValueError:
        control_iface = None  # local run / no SSH_CONNECTION
    return ota_iface, control_iface


def _peer_command_runner(
    peer: OtaSmokePeer, *, dry_run: bool, sudo_password: str | None = None
) -> Callable[[Sequence[str]], None]:
    """A CommandRunner that runs one privileged argv on ``peer`` via the SSH path."""

    def run(argv: Sequence[str]) -> None:
        label = f"{peer.name}: tc/netem"
        if sudo_password is None:
            script = _sudo_command(argv)
            _ota_run(peer, script, label=label, dry_run=dry_run)
        else:
            script = _sudo_command(argv, askpass=True)
            _ota_log_action(label, "running remote sudo command via stdin")
            _ota_run_with_secret_stdin(
                peer,
                script,
                label=label,
                dry_run=dry_run,
                secret_stdin=_sudo_stdin(sudo_password) or "",
            )

    return run


def _peer_watchdog_launcher(
    peer: OtaSmokePeer, *, dry_run: bool, sudo_password: str | None = None
) -> Callable[[Sequence[str]], None]:
    """Launch the safety-watchdog argv detached on ``peer`` so it survives a crash."""

    def launch(argv: Sequence[str]) -> None:
        if sudo_password is None:
            detached = _passwordless_watchdog_command(argv)
            _ota_run(peer, detached, label=f"{peer.name}: profile safety watchdog", dry_run=dry_run, check=False)
        else:
            inner = f"nohup {shlex.join(list(argv))} >/dev/null 2>&1 &"
            detached = f"sudo -S -p '' sh -c {shlex.quote(inner)}"
            label = f"{peer.name}: profile safety watchdog"
            _ota_log_action(label, "arming remote sudo watchdog via stdin")
            _ota_run_with_secret_stdin(
                peer,
                detached,
                label=label,
                dry_run=dry_run,
                check=False,
                secret_stdin=_sudo_stdin(sudo_password) or "",
            )

    return launch


def _ota_arm_profile(
    plan: OtaSmokePlan,
    profile: Any,
    directions: dict[str, str],
    *,
    dry_run: bool,
    sudo_passwords: Mapping[str, str] | None = None,
) -> list[Any]:
    """Arm the static profile per direction on every peer's OTA egress (RFC 0004),
    returning the ``ProfileShaper`` handles to revert in the run's ``finally``."""
    from .network_profiles import shaping_commands
    from .network_shaper import ProfileShaper

    shapers: list[Any] = []
    peer_names = sorted(plan.peers)
    for peer_name in peer_names:
        peer = plan.peers[peer_name]
        direction = directions.get(peer_name)
        shaping = profile.uplink if direction == "uplink" else profile.downlink
        if shaping is None or shaping.is_empty:
            print(f"OTA profile: {peer_name} ({direction}) — nothing to shape.")
            continue
        other_addr = next(plan.peers[name].address for name in peer_names if name != peer_name)
        if dry_run:
            ota_iface, control_iface = "<ota-if>", None
        else:
            ota_iface, control_iface = _ota_resolve_interfaces(peer, other_addr, dry_run=False)
        sudo_password = (sudo_passwords or {}).get(peer_name)
        shaper = ProfileShaper(
            ota_iface,
            _peer_command_runner(peer, dry_run=dry_run, sudo_password=sudo_password),
            control_interface=control_iface,
            safety_max_duration_s=_PROFILE_SAFETY_MAX_S,
            watchdog_launcher=_peer_watchdog_launcher(peer, dry_run=dry_run, sudo_password=sudo_password),
        )
        # Register before arming so a mid-arm failure is still reverted in `finally`.
        shapers.append(shaper)
        print(f"OTA profile: arming {profile.name!r} {direction} on {peer_name}:{ota_iface}")
        shaper.arm(shaping_commands(ota_iface, shaping))
    return shapers


def _ota_teardown_profile(shapers: list[Any]) -> None:
    """Revert every armed profile (always runs in the run's ``finally``)."""
    for shaper in shapers:
        shaper.teardown()


def _ota_start_peers(
    target: InteractiveSmokeTarget,
    plan: OtaSmokePlan,
    instance_id: str,
    *,
    dry_run: bool,
    mode: str = "detached",
    link_trace_parts: list[str] | None = None,
) -> None:
    peer_args = _ota_peer_address_args(plan)
    for peer_name in sorted(plan.peers):
        peer = plan.peers[peer_name]
        command = _ota_rosotacom_command(
            plan,
            _ota_start_parts(target, peer_name, instance_id, peer_args, mode=mode, link_trace_parts=link_trace_parts),
        )
        _ota_run(peer, command, label=f"{peer_name}: start {target.name}", dry_run=dry_run)


def _ota_start_session_publishers(
    target: InteractiveSmokeTarget,
    plan: OtaSmokePlan,
    *,
    dry_run: bool,
) -> None:
    if target.target_type != "session":
        print("OTA smoke: target is a scenario; using its application publishers.")
        return
    peer_args = _ota_peer_address_args(plan)
    for peer_name in sorted(plan.peers):
        peer = plan.peers[peer_name]
        command = _ota_rosotacom_command(plan, _ota_publish_parts(target, peer_name, peer_args))
        result = _ota_run(peer, command, label=f"{peer_name}: start synthetic publishers", dry_run=dry_run, check=False)
        _print_completed_output(result)
        if result.returncode != 0:
            _ota_print_failure_output(f"{peer_name}: start synthetic publishers", result)


def _ota_stop_session_publishers(
    target: InteractiveSmokeTarget,
    plan: OtaSmokePlan,
    *,
    dry_run: bool,
) -> None:
    if target.target_type != "session":
        return
    peer_args = _ota_peer_address_args(plan)
    for peer_name in sorted(plan.peers):
        peer = plan.peers[peer_name]
        command = _ota_rosotacom_command(plan, _ota_publish_parts(target, peer_name, peer_args, stop=True))
        _ota_run(peer, command, label=f"{peer_name}: stop synthetic publishers", dry_run=dry_run, check=False)


def _ota_verify_delivery(
    target: InteractiveSmokeTarget,
    plan: OtaSmokePlan,
    instance_id: str,
    *,
    dry_run: bool,
    profile: str | None = None,
) -> list[str]:
    errors: list[str] = []
    print("OTA smoke: running status/expectation checks on every peer.")
    for peer_name in sorted(plan.peers):
        peer = plan.peers[peer_name]
        command = _ota_rosotacom_command(plan, _ota_test_parts(target, instance_id, profile=profile))
        result = _ota_run(peer, command, label=f"{peer_name}: rosotacom test", dry_run=dry_run, check=False)
        if result.returncode != 0:
            _ota_print_failure_output(f"{peer_name}: rosotacom test", result)
            errors.append(f"{peer_name}: rosotacom test failed")
        else:
            _print_completed_output(result)
    return errors


def _ota_verify_isolation(
    target: InteractiveSmokeTarget,
    plan: OtaSmokePlan,
    *,
    dry_run: bool,
) -> list[str]:
    peer_names = sorted(plan.peers)
    source_name, receiver_name = peer_names[0], peer_names[1]
    source = plan.peers[source_name]
    receiver = plan.peers[receiver_name]
    peer_args = _ota_peer_address_args(plan)
    errors: list[str] = []
    print(f"OTA smoke: checking isolation from {source_name} to {receiver_name}.")
    publish = _ota_rosotacom_command(plan, _ota_probe_publish_parts(target, source_name, peer_args, stop=False))
    published = _ota_run(source, publish, label=f"{source_name}: publish isolation probe", dry_run=dry_run, check=False)
    if published.returncode != 0:
        _ota_print_failure_output(f"{source_name}: publish isolation probe", published)
        errors.append(f"{source_name}: isolation probe publisher failed")
        return errors
    _print_completed_output(published)
    check = _ota_rosotacom_command(plan, _ota_probe_check_parts(target, receiver_name, peer_args))
    checked = _ota_run(
        receiver,
        check,
        label=f"{receiver_name}: check isolation probe absent",
        dry_run=dry_run,
        check=False,
    )
    if checked.returncode != 0:
        _ota_print_failure_output(f"{receiver_name}: check isolation probe absent", checked)
        errors.append(f"{receiver_name}: isolation probe crossed OTA boundary")
    else:
        _print_completed_output(checked)
    stop = _ota_rosotacom_command(plan, _ota_probe_publish_parts(target, source_name, peer_args, stop=True))
    stopped = _ota_run(source, stop, label=f"{source_name}: stop isolation probe", dry_run=dry_run, check=False)
    _print_completed_output(stopped)
    return errors


def _ota_verify_content_integrity(
    target: InteractiveSmokeTarget,
    plan: OtaSmokePlan,
    cfg: dict[str, Any],
    *,
    dry_run: bool,
) -> list[str]:
    """For each peer's PASS-THROUGH String topic, assert the delivered payload is
    byte-identical to what the sender published (the synthetic publishers are still
    running from the test phase, so their known payload is the ground truth)."""
    if target.target_type != "session":
        # The byte-equality ground truth is the synthetic publisher payload, which
        # only runs for session targets (see _ota_start_session_publishers).
        # Scenarios drive their own application publishers with no fixed payload,
        # so there is nothing to byte-compare against; delivery + isolation already
        # cover the OTA guarantee for them.
        print("OTA smoke: skipping content integrity (scenario uses application publishers).")
        return []
    peer_args = _ota_peer_address_args(plan)
    errors: list[str] = []
    for receiver_name, receiver in plan.peers.items():
        for topic, msg_type, field, expected in _content_integrity_specs(cfg, receiver_name):
            parts = _ota_content_check_parts(target, receiver_name, peer_args, topic, msg_type, field, expected)
            cmd = _ota_rosotacom_command(plan, parts)
            print(f"OTA smoke: checking content integrity for {receiver_name} {topic}.")
            result = _ota_run(
                receiver, cmd, label=f"{receiver_name}: content integrity {topic}", dry_run=dry_run, check=False
            )
            if result.returncode != 0:
                _ota_print_failure_output(f"{receiver_name}: content integrity {topic}", result)
                errors.append(f"{receiver_name}: content integrity mismatch on {topic}")
            else:
                _print_completed_output(result)
    return errors


def _ota_collect_logs(
    instance: SessionInstance,
    plan: OtaSmokePlan,
    *,
    dry_run: bool,
) -> None:
    project = shlex.quote(plan.project)
    instance_suffix = shlex.quote(f"*_{instance.instance_id}")
    script = (
        f"cd {shlex.quote(plan.workdir)} && "
        f"project_dir=$(dirname {project}) && "
        f'instance_dir=$(find "$project_dir/session-instances" -mindepth 2 -maxdepth 2 '
        f"-type d -name {instance_suffix} -print -quit 2>/dev/null) && "
        'if [ -n "$instance_dir" ]; then '
        'relative=${instance_dir#"$project_dir"/}; '
        'tar cz -C "$project_dir" "$relative" 2>/dev/null | base64; fi'
    )
    for peer in plan.peers.values():
        result = _ota_run(peer, script, label=f"{peer.name}: collect session-instances", dry_run=dry_run, check=False)
        if dry_run or not result.stdout.strip():
            continue
        _ota_extract_peer_artifacts(instance, peer, result.stdout)


def _ota_extract_peer_artifacts(instance: SessionInstance, peer: OtaSmokePeer, encoded: str) -> None:
    """Extract a peer's base64 ``session-instances/<date>/<name>`` tarball into the
    local instance directory, reproducing the layout a local run writes
    (``config/<id>``, ``logs/<id>/{catmux,status,launcher.log,...}``).

    Each peer host names its instance directory with its own timestamp (only the
    instance-id suffix matches), so the leading ``session-instances/<date>/<name>/``
    prefix (3 path components) is stripped rather than matched. The per-peer
    ``manifest.yaml`` is skipped so the orchestration manifest is preserved.
    """
    try:
        raw = base64.b64decode(encoded, validate=False)
    except (binascii.Error, ValueError):
        print(f"OTA smoke: could not decode collected artifacts from peer {peer.name}", file=sys.stderr)
        return
    dest_root = instance.host_dir.resolve()
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            for member in archive.getmembers():
                parts = [part for part in member.name.split("/") if part not in ("", ".")]
                if len(parts) <= 3:
                    continue  # the stripped session-instances/<date>/<name> prefix itself
                rel = Path(*parts[3:])
                if str(rel) == "manifest.yaml":
                    continue
                target = (dest_root / rel).resolve()
                try:
                    target.relative_to(dest_root)
                except ValueError:
                    continue  # refuse paths escaping the instance directory
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isreg():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                with extracted, target.open("wb") as handle:
                    shutil.copyfileobj(extracted, handle)
    except tarfile.TarError:
        print(f"OTA smoke: could not unpack collected artifacts from peer {peer.name}", file=sys.stderr)


def _ota_stop_peers(
    target: InteractiveSmokeTarget,
    plan: OtaSmokePlan,
    instance_id: str | None,
    *,
    dry_run: bool,
) -> None:
    _ota_stop_session_publishers(target, plan, dry_run=dry_run)
    peer_args = _ota_peer_address_args(plan)
    for peer_name in sorted(plan.peers):
        peer = plan.peers[peer_name]
        command = _ota_rosotacom_command(plan, _ota_stop_parts(target, peer_name, instance_id, peer_args))
        _ota_run(peer, command, label=f"{peer_name}: stop {target.name}", dry_run=dry_run, check=False)


def _ota_verify_only(args: argparse.Namespace) -> int:
    _runtime, plan, target = _resolve_ota_smoke_context(args)
    instance_id = getattr(args, "instance_id", None)
    if not instance_id:
        raise RuntimeError("ota-smoke --verify-only requires --instance-id.")
    dry_run = bool(getattr(args, "dry_run", False))
    print(f"OTA smoke verification starting: {target.name} ({target.target_type}), instance={instance_id}")
    print("OTA smoke: starting synthetic publishers where needed.")
    _ota_start_session_publishers(target, plan, dry_run=dry_run)
    errors = _ota_verify_delivery(target, plan, instance_id, dry_run=dry_run)
    errors += _ota_verify_isolation(target, plan, dry_run=dry_run)
    if errors:
        for error in errors:
            print(f"OTA SMOKE ERROR: {error}", file=sys.stderr)
        return 1
    print("OTA SMOKE OK")
    return 0


def _ota_wait_for_running_container_suffix_script(suffix: str, label: str) -> str:
    pattern = shlex.quote(f"{re.escape(suffix)}$")
    quoted_label = shlex.quote(label)
    return (
        "container=''; "
        f"until container=$(docker ps --filter status=running --format '{{{{.Names}}}}' "
        f"| grep -E {pattern} | head -n 1) "
        '&& [ -n "$container" ]; do '
        f"echo '[INFO] waiting for running container:' {quoted_label}; "
        "sleep 2; "
        "done; "
        'echo "[INFO] found running container: $container"'
    )


def _ota_application_run_script(
    plan: OtaSmokePlan,
    target: InteractiveSmokeTarget,
    peer_name: str,
    application: ScenarioApplication,
    instance_id: str,
) -> str:
    communication_suffix = _sanitize_docker_name(f"_com_to_{_remote_peer_name(target.cfg, peer_name)}")
    label = f"{peer_name}:{application.name}"
    command = _ota_rosotacom_command(plan, _ota_application_parts(target, peer_name, application, instance_id))
    return (
        f"{_ota_wait_for_running_container_suffix_script(communication_suffix, f'{peer_name}:communication')}; "
        f"echo '[INFO] starting native application container:' {shlex.quote(label)}; "
        f"{command}; "
        "rc=$?; "
        "echo; echo '[INFO] native application exited with status' \"$rc\"; "
        "exec bash"
    )


def _ota_create_tmux(
    runtime: RuntimeConfig,
    target: InteractiveSmokeTarget,
    plan: OtaSmokePlan,
    instance: SessionInstance,
    link_trace_parts: list[str] | None = None,
) -> str:
    _require_tmux()
    session_name = _ota_smoke_tmux_session(target.target_type, target.name)
    peer_args = _ota_peer_address_args(plan)
    peers = [plan.peers[name] for name in sorted(plan.peers)]
    first_peer = peers[0]
    first_start = _ota_rosotacom_command(
        plan,
        _ota_communication_start_parts(
            target,
            first_peer.name,
            instance.instance_id,
            peer_args,
            mode="attach",
            link_trace_parts=link_trace_parts,
        ),
    )
    first_script = (
        "set -e; "
        f"echo '[INFO] starting interactive communication for remote peer {first_peer.name}'; "
        "echo '[INFO] this pane attaches to the peer communication/catmux session'; "
        f"{first_start}; "
        "echo; "
        f"echo '[INFO] remote peer {first_peer.name} communication exited'; "
        "exec bash"
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
            _safe_path_token(f"{first_peer.name}_communication"),
            _ota_quote_cmd(_ota_remote_argv(first_peer, first_script, tty=bool(first_peer.ssh))),
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    first_pane = created.stdout.strip()
    subprocess.run(
        _tmux_command(runtime, "set-window-option", "-g", "-t", session_name, "remain-on-exit", "on"),
        check=True,
    )
    subprocess.run(_tmux_command(runtime, "set-option", "-t", session_name, "prefix", "C-b"), check=True)
    subprocess.run(_tmux_command(runtime, "bind-key", "-T", "prefix", "C-b", "send-prefix"), check=True)
    for key, value in (
        ("@rosotacom_ota_smoke_target", target.name),
        ("@rosotacom_ota_smoke_target_type", target.target_type),
        ("@rosotacom_ota_smoke_instance", instance.instance_id),
        ("@rosotacom_ota_smoke_state", str(plan.state_path)),
    ):
        subprocess.run(_tmux_command(runtime, "set-option", "-t", session_name, key, value), check=True)
    subprocess.run(_tmux_command(runtime, "set-option", "-t", session_name, "pane-border-status", "top"), check=True)
    subprocess.run(
        _tmux_command(runtime, "set-option", "-t", session_name, "pane-border-format", " #{pane_title} "),
        check=True,
    )
    subprocess.run(
        _tmux_command(
            runtime,
            "set-option",
            "-t",
            session_name,
            "status-right",
            " ota smoke | windows: C-b n/p | inner catmux: C-b C-b ",
        ),
        check=True,
    )
    subprocess.run(
        _tmux_command(runtime, "select-pane", "-t", first_pane, "-T", f"{first_peer.name}:communication"),
        check=True,
    )
    _attach_tmux_pipe(
        runtime,
        first_pane,
        instance.logs_host_dir / "ota-smoke" / f"{first_peer.name}-communication.log",
    )

    for peer in peers[1:]:
        start = _ota_rosotacom_command(
            plan,
            _ota_communication_start_parts(
                target,
                peer.name,
                instance.instance_id,
                peer_args,
                mode="attach",
                link_trace_parts=link_trace_parts,
            ),
        )
        script = (
            "set -e; "
            f"echo '[INFO] starting interactive communication for remote peer {peer.name}'; "
            "echo '[INFO] this pane attaches to the peer communication/catmux session'; "
            f"{start}; "
            "echo; "
            f"echo '[INFO] remote peer {peer.name} communication exited'; "
            "exec bash"
        )
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
                _safe_path_token(f"{peer.name}_communication"),
                _ota_quote_cmd(_ota_remote_argv(peer, script, tty=bool(peer.ssh))),
            ),
            text=True,
            capture_output=True,
            check=True,
        )
        pane_id = created_window.stdout.strip()
        subprocess.run(
            _tmux_command(runtime, "select-pane", "-t", pane_id, "-T", f"{peer.name}:communication"),
            check=True,
        )
        _attach_tmux_pipe(runtime, pane_id, instance.logs_host_dir / "ota-smoke" / f"{peer.name}-communication.log")

    if target.scenario_definition:
        for peer in peers:
            for application in target.scenario_definition.applications.get(peer.name, ()):
                application_script = _ota_application_run_script(
                    plan, target, peer.name, application, instance.instance_id
                )
                created_application = subprocess.run(
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
                        _safe_path_token(f"{peer.name}_{application.name}"),
                        _ota_quote_cmd(_ota_remote_argv(peer, application_script, tty=bool(peer.ssh))),
                    ),
                    text=True,
                    capture_output=True,
                    check=True,
                )
                pane_id = created_application.stdout.strip()
                subprocess.run(
                    _tmux_command(
                        runtime,
                        "select-pane",
                        "-t",
                        pane_id,
                        "-T",
                        f"{peer.name}:application:{application.name}",
                    ),
                    check=True,
                )
                _attach_tmux_pipe(
                    runtime,
                    pane_id,
                    instance.logs_host_dir
                    / "ota-smoke"
                    / f"{peer.name}-application-{_safe_path_token(application.name)}.log",
                )

    verify_parts = [
        sys.executable,
        "-m",
        "rosotacom",
        "ota-smoke",
        target.name,
        "--state-file",
        str(plan.state_path),
        "--target-type",
        target.target_type,
        "--instance-id",
        instance.instance_id,
        "--verify-only",
        *_runtime_cli_args(runtime),
    ]
    verify_cmd = _ota_quote_cmd(verify_parts)
    verify_script = (
        f"echo '[INFO] starting OTA smoke verification for {target.name}'; "
        f"{verify_cmd}; rc=$?; "
        "echo; echo '[INFO] verification exited with status' \"$rc\"; "
        "echo '[INFO] verification log remains in this pane'; "
        "exec bash"
    )
    verification = subprocess.run(
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
            "verification",
            _host_shell(verify_script),
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    verification_pane = verification.stdout.strip()
    subprocess.run(_tmux_command(runtime, "select-pane", "-t", verification_pane, "-T", "verification"), check=True)
    _attach_tmux_pipe(runtime, verification_pane, instance.logs_host_dir / "ota-smoke" / "verification.log")
    subprocess.run(_tmux_command(runtime, "select-window", "-t", f"{session_name}:verification"), check=True)
    return session_name


def _list_ota_smoke(args: argparse.Namespace) -> int:
    runtime = _load_runtime_config(args)
    print(_format_active_ota_smoke_runs(_active_ota_smoke_runs(runtime)))
    return 0


def _ota_cleanup_hosts(plan: OtaSmokePlan, *, dry_run: bool) -> None:
    _ota_validate_prepare_workdir(plan.workdir)
    for peer in plan.peers.values():
        _ota_run(
            peer,
            f"rm -rf {shlex.quote(plan.workdir)}",
            label=f"{peer.name}: remove OTA workdir",
            dry_run=dry_run,
            check=False,
        )


def _start_interactive_ota_smoke(args: argparse.Namespace) -> int:
    runtime, plan, target = _resolve_ota_smoke_context(args)
    dry_run = bool(getattr(args, "dry_run", False))
    if not getattr(args, "skip_preflight", False):
        # Conflicts are checked after the attach shortcut below: re-attaching to
        # this run's own containers must not count as a conflict.
        _ota_preflight(
            plan,
            require_tmux=target.target_type == "scenario",
            check_peer_reachability=bool(getattr(args, "check_peer_reachability", False)),
            dry_run=dry_run,
            check_conflicts=False,
        )
    tmux_session = _ota_smoke_tmux_session(target.target_type, target.name)
    mode = _resolve_mode(getattr(args, "mode", "auto"))
    if _tmux_session_exists(runtime, tmux_session):
        print(f"OTA smoke already running: {target.name} ({target.target_type})")
        if mode == "attach":
            subprocess.run(_tmux_command(runtime, "attach-session", "-t", tmux_session), check=True)
        else:
            print(f"Attach with: rosotacom ota-smoke {shlex.quote(target.name)} --interactive")
        return 0

    if not getattr(args, "skip_preflight", False) and not getattr(args, "skip_conflict_check", False):
        _ota_conflict_check(plan, dry_run=dry_run)
    _ota_prepare_hosts(args, runtime, plan)
    instance = _resolve_session_instance(
        runtime,
        target.session,
        getattr(args, "instance_id", None) or _new_instance_id(),
    )
    if dry_run:
        plan = replace(plan, state_path=instance.host_dir / "ota-deployment.yaml")
    else:
        plan = _ota_write_state(instance, plan)
    _ota_write_manifest(
        instance,
        target,
        runtime,
        plan,
        tmux_session=tmux_session,
        interactive=True,
        phase="running",
    )
    if dry_run:
        print(f"Would create OTA smoke tmux session: {tmux_session}")
        print(f"OTA smoke artifacts: {instance.host_dir}")
        return 0
    created = _ota_create_tmux(runtime, target, plan, instance, link_trace_parts=_link_trace_parts_from_args(args))
    print(f"rosotacom OTA smoke instance: {instance.host_dir}")
    print(f"rosotacom OTA smoke started: {target.name} ({target.target_type})")
    print("Local control tmux prefix: Ctrl-b. Send the peer tmux/catmux prefix with Ctrl-b Ctrl-b.")
    if target.target_type == "scenario":
        for peer_name, peer in plan.peers.items():
            attach = _ota_rosotacom_command(plan, ["scenario", "attach", target.name, "--identity", peer_name])
            attach_cmd = _ota_quote_cmd(_ota_remote_argv(peer, attach, tty=bool(peer.ssh)))
            print(f"Manual remote scenario reattach for {peer_name}: {attach_cmd}")
    if mode == "attach":
        subprocess.run(_tmux_command(runtime, "attach-session", "-t", created), check=True)
    else:
        print(f"Attach with: rosotacom ota-smoke {shlex.quote(target.name)} --interactive")
        print(f"Stop with: rosotacom ota-smoke {shlex.quote(target.name)} --interactive --stop")
    return 0


def _start_noninteractive_ota_smoke(args: argparse.Namespace) -> int:
    runtime, plan, target = _resolve_ota_smoke_context(args)
    dry_run = bool(getattr(args, "dry_run", False))
    if not getattr(args, "skip_preflight", False):
        _ota_preflight(
            plan,
            require_tmux=target.target_type == "scenario",
            check_peer_reachability=bool(getattr(args, "check_peer_reachability", False)),
            dry_run=dry_run,
            check_conflicts=not getattr(args, "skip_conflict_check", False),
        )
    _ota_prepare_hosts(args, runtime, plan)
    instance = _resolve_session_instance(
        runtime,
        target.session,
        getattr(args, "instance_id", None) or _new_instance_id(),
    )
    if dry_run:
        plan = replace(plan, state_path=instance.host_dir / "ota-deployment.yaml")
    else:
        plan = _ota_write_state(instance, plan)
    _ota_write_manifest(
        instance,
        target,
        runtime,
        plan,
        tmux_session=None,
        interactive=False,
        phase="running",
    )
    profile_name, profile = _resolve_ota_profile(runtime, target, args)
    directions = _profile_directions(plan, target.cfg) if profile is not None else {}
    shapers: list[Any] = []
    errors: list[str] = []
    try:
        if profile is not None:
            shapers = _ota_arm_profile(plan, profile, directions, dry_run=dry_run)
        _ota_start_peers(
            target,
            plan,
            instance.instance_id,
            dry_run=dry_run,
            link_trace_parts=_link_trace_parts_from_args(args),
        )
        if not dry_run:
            time.sleep(12)
        _ota_start_session_publishers(target, plan, dry_run=dry_run)
        errors += _ota_verify_delivery(target, plan, instance.instance_id, dry_run=dry_run, profile=profile_name)
        errors += _ota_verify_isolation(target, plan, dry_run=dry_run)
        errors += _ota_verify_content_integrity(target, plan, target.cfg, dry_run=dry_run)
        if errors:
            raise RuntimeError("OTA smoke verification failed:\n  - " + "\n  - ".join(errors))
        print("OTA SMOKE OK")
        return 0
    finally:
        # Revert shaping first — a stuck qdisc must never outlive the run (RFC 0004).
        _ota_teardown_profile(shapers)
        _ota_collect_logs(instance, plan, dry_run=dry_run)
        if not getattr(args, "keep_running", False):
            _ota_stop_peers(target, plan, instance.instance_id, dry_run=dry_run)
            _ota_write_manifest(
                instance,
                target,
                runtime,
                plan,
                tmux_session=None,
                interactive=False,
                phase="stopped",
            )
            if not getattr(args, "keep_workdir", False):
                _ota_cleanup_hosts(plan, dry_run=dry_run)
        print(f"OTA smoke artifacts: {instance.host_dir}")


def _stop_ota_smoke(args: argparse.Namespace) -> int:
    runtime = _load_runtime_config(args)
    dry_run = bool(getattr(args, "dry_run", False))
    target_arg = getattr(args, "target", None)
    target_type = getattr(args, "target_type", "auto")
    active: ActiveOtaSmokeRun | None = None
    if not getattr(args, "state_file", None):
        try:
            active = _infer_active_ota_smoke_run(
                runtime,
                target_arg,
                target_type,
                getattr(args, "instance_id", None),
            )
        except RuntimeError:
            if not target_arg:
                raise
            active = None
        else:
            args.state_file = active.state_path
            args.instance_id = getattr(args, "instance_id", None) or active.instance_id
            args.target = target_arg or active.target
            args.target_type = active.target_type
    runtime, plan, target = _resolve_ota_smoke_context(args)
    instance_id = getattr(args, "instance_id", None)
    _ota_stop_peers(target, plan, instance_id, dry_run=dry_run)
    tmux_session = active.tmux_session if active else _ota_smoke_tmux_session(target.target_type, target.name)
    if dry_run:
        print(f"Would stop OTA smoke tmux session: {tmux_session}")
    elif _kill_scenario_tmux(runtime, tmux_session):
        print(f"Stopped OTA smoke tmux session: {tmux_session}")
    if instance_id and not dry_run:
        instance = _resolve_session_instance(runtime, target.session, instance_id)
        # Collect the full per-peer artifacts (config, catmux, status, launcher, ...)
        # before the remote workdir is wiped below.
        _ota_collect_logs(instance, plan, dry_run=dry_run)
        _ota_write_manifest(
            instance,
            target,
            runtime,
            plan,
            tmux_session=None,
            interactive=bool(active),
            phase="stopped",
        )
    if not getattr(args, "keep_workdir", False):
        _ota_cleanup_hosts(plan, dry_run=dry_run)
    print(f"OTA smoke cleanup attempted for: {target.name} ({target.target_type})")
    return 0


def ota_smoke(args: argparse.Namespace) -> int:
    if getattr(args, "verify_only", False):
        return _ota_verify_only(args)
    if getattr(args, "list", False):
        return _list_ota_smoke(args)
    if getattr(args, "stop", False):
        return _stop_ota_smoke(args)
    if getattr(args, "interactive", False):
        requested_profile = getattr(args, "profile", None)
        if requested_profile and requested_profile != "none":
            raise RuntimeError(
                "--profile is not supported in interactive mode (no clean teardown hook for a detached tmux "
                "session). Run the non-interactive ota-smoke to shape the link, or apply the profile manually."
            )
        return _start_interactive_ota_smoke(args)
    return _start_noninteractive_ota_smoke(args)


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


def _smoke_target_completer(
    prefix: str,
    parsed_args: argparse.Namespace,
    **_: Any,
) -> dict[str, str]:
    completions = _session_name_completer(prefix, parsed_args)
    try:
        runtime = _load_runtime_config(parsed_args)
    except (FileNotFoundError, RuntimeError, OSError, yaml.YAMLError):
        return completions
    completions.update({name: "configured scenario" for name in _scenario_names(runtime) if name.startswith(prefix)})
    return completions


def _deployment_value_keys(runtime: RuntimeConfig) -> list[str]:
    deployment = _deployment_config(runtime)
    if deployment is None:
        return []

    def walk(mapping: dict[str, Any], prefix: str = "") -> list[str]:
        keys: list[str] = []
        for key, value in mapping.items():
            full = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                keys.extend(walk(value, full))
            else:
                keys.append(full)
        return keys

    return sorted(walk(deployment.values))


def _peer_keys_for_completion(runtime: RuntimeConfig, parsed_args: argparse.Namespace) -> list[str]:
    try:
        command = getattr(parsed_args, "command", None)
        if command == "scenario":
            scenario_name = getattr(parsed_args, "scenario", None)
            if not scenario_name:
                return []
            resolved = _resolve_scenario(scenario_name, runtime)
            definition = _load_scenario_definition(resolved)
            session = _resolve_session(definition.session, runtime)
        elif command in {"smoke", "ota-smoke"}:
            target = _resolve_interactive_smoke_target(
                getattr(parsed_args, "session_dir", None) or getattr(parsed_args, "target", None),
                runtime,
                getattr(parsed_args, "target_type", "auto"),
            )
            return _peer_keys_from_cfg(target.cfg)
        else:
            session_name = getattr(parsed_args, "session_dir", None) or getattr(
                parsed_args, "session_dir_positional", None
            )
            if not session_name:
                return []
            session = _resolve_session(session_name, runtime)
        cfg = _effective_session_config(session.host_dir, runtime)
        return _peer_keys_from_cfg(cfg)
    except (FileNotFoundError, RuntimeError, OSError, yaml.YAMLError):
        return []


def _peer_address_completer(
    prefix: str,
    parsed_args: argparse.Namespace,
    **_: Any,
) -> dict[str, str]:
    try:
        runtime = _load_runtime_config(parsed_args)
    except (FileNotFoundError, RuntimeError, OSError, yaml.YAMLError):
        return {}
    if "=" not in prefix:
        return {
            f"{peer}=": "peer address override"
            for peer in _peer_keys_for_completion(runtime, parsed_args)
            if peer.startswith(prefix)
        }
    peer, value_prefix = prefix.split("=", 1)
    if value_prefix and not "value:".startswith(value_prefix) and not value_prefix.startswith("value:"):
        return {}
    key_prefix = value_prefix[len("value:") :] if value_prefix.startswith("value:") else ""
    return {
        f"{peer}=value:{key}": "deployment value"
        for key in _deployment_value_keys(runtime)
        if key.startswith(key_prefix)
    }


def _peer_host_completer(
    prefix: str,
    parsed_args: argparse.Namespace,
    **_: Any,
) -> dict[str, str]:
    try:
        runtime = _load_runtime_config(parsed_args)
        deployment = _deployment_config(runtime)
    except (FileNotFoundError, RuntimeError, OSError, yaml.YAMLError):
        return {}
    if "=" not in prefix:
        return {
            f"{peer}=": "logical peer"
            for peer in _peer_keys_for_completion(runtime, parsed_args)
            if peer.startswith(prefix)
        }
    peer, host_prefix = prefix.split("=", 1)
    if deployment is None:
        return {}
    return {f"{peer}={host}": "deployment host" for host in sorted(deployment.hosts) if host.startswith(host_prefix)}


def _peer_ssh_completer(
    prefix: str,
    parsed_args: argparse.Namespace,
    **_: Any,
) -> dict[str, str]:
    try:
        runtime = _load_runtime_config(parsed_args)
    except (FileNotFoundError, RuntimeError, OSError, yaml.YAMLError):
        return {}
    if "=" not in prefix:
        return {
            f"{peer}=": "logical peer"
            for peer in _peer_keys_for_completion(runtime, parsed_args)
            if peer.startswith(prefix)
        }
    peer, value_prefix = prefix.split("=", 1)
    return {f"{peer}=local": "run peer locally"} if "local".startswith(value_prefix) else {}


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
    instance_id: str,
) -> str:
    base = _sanitize_docker_name(
        f"rosotacom_{runtime.install_id}_{_instance_name_token(instance_id)}"
        f"_scenario_{scenario_name}_{identity}_{application_name}"
    )
    if len(base) <= 120:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    return f"{base[:109]}_{digest}"


def _matching_scenario_containers(
    runtime: RuntimeConfig,
    scenario_name: str,
    identity: str,
    application_name: str,
    *,
    all_states: bool = False,
) -> list[str]:
    """Workspace containers for one scenario application, any instance.

    Reconstructs the exact name from each candidate's instance token so the
    long-name truncation applies identically.
    """
    names = []
    for name, _networks in _list_docker_containers(all_states=all_states):
        parts = _split_workspace_container(name, runtime)
        if parts is None:
            continue
        if name == _scenario_container_name(runtime, scenario_name, identity, application_name, parts[0]):
            names.append(name)
    return sorted(names)


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
    for session_configs_dir in runtime.session_configs_dir:
        args.extend(["--session-configs-dir", str(session_configs_dir)])
    for scenario_configs_dir in runtime.scenario_configs_dir:
        args.extend(["--scenario-configs-dir", str(scenario_configs_dir)])
    if runtime.session_instances_dir:
        args.extend(["--session-instances-dir", str(runtime.session_instances_dir)])
    if runtime.deployment:
        args.extend(["--deployment", str(runtime.deployment)])
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
    if getattr(args, "link_trace", None):
        command.append("--link-trace")
    if getattr(args, "link_trace_interval_s", None) is not None:
        command.extend(["--link-trace-interval", str(args.link_trace_interval_s)])
    if getattr(args, "link_trace_modem_command", None):
        command.extend(["--link-trace-modem-command", args.link_trace_modem_command])
    for assignment in getattr(args, "peer", []) or []:
        command.extend(["--peer", assignment])
    for override in getattr(args, "peer_address", []) or []:
        command.extend(["--peer-address", override])
    network_name = getattr(args, "network_name", None)
    if network_name:
        command.extend(["--network-name", network_name])
    network_ip = getattr(args, "network_ip", None)
    if network_ip:
        command.extend(["--network-ip", network_ip])
    return command


def _scenario_application_command(
    runtime: RuntimeConfig,
    resolved: ResolvedScenario,
    identity: str,
    application: ScenarioApplication,
    instance_id: str,
    *,
    network_name: str | None = None,
    network_ip: str | None = None,
) -> list[str]:
    command = [
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
        "--instance-id",
        instance_id,
        *_runtime_cli_args(runtime),
    ]
    if network_name:
        command.extend(["--network-name", network_name])
    if network_ip:
        command.extend(["--network-ip", network_ip])
    return command


def _scenario_log_path(instance: SessionInstance, identity: str, component: str) -> Path:
    return instance.logs_host_dir / _safe_path_token(identity) / "scenario" / f"{_safe_path_token(component)}.log"


def _attach_tmux_pipe(runtime: RuntimeConfig, pane_id: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        _tmux_command(runtime, "pipe-pane", "-o", "-t", pane_id, f"cat >> {shlex.quote(str(log_path))}"),
        check=True,
    )


def _create_tmux_split_below(
    runtime: RuntimeConfig,
    target_pane: str,
    title: str,
    command: str,
    *,
    log_path: Path | None = None,
) -> str:
    created = subprocess.run(
        _tmux_command(
            runtime,
            "split-window",
            "-d",
            "-v",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            target_pane,
            command,
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    pane_id = created.stdout.strip()
    subprocess.run(_tmux_command(runtime, "select-pane", "-t", pane_id, "-T", title), check=True)
    if log_path is not None:
        _attach_tmux_pipe(runtime, pane_id, log_path)
    return pane_id


def _create_scenario_tmux(
    runtime: RuntimeConfig,
    resolved: ResolvedScenario,
    definition: ScenarioDefinition,
    instance: SessionInstance,
    identity: str,
    applications: tuple[ScenarioApplication, ...],
    communication_container: str,
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
        application_network = f"container:{communication_container}" if getattr(args, "network_name", None) else None
        application_command = shlex.join(
            _scenario_application_command(
                runtime,
                resolved,
                identity,
                application,
                instance.instance_id,
                network_name=application_network,
            )
        )
        if application_network:
            application_command = f"{_wait_for_container_ready_script(communication_container)}; {application_command}"
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
                application_command,
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
    for index, session_configs_dir in enumerate(runtime.session_configs_dir):
        args.extend(["-v", f"{session_configs_dir}:{_session_config_container_root(index)}:ro"])
    if runtime.session_configs_dir:
        session_container_root = _configured_session_container_root(session.host_dir, runtime)
        if session_container_root is None:
            session_container_root = _session_config_container_root(0)
        args.extend(
            [
                "-e",
                f"SESSION_DEFINITIONS_DIR={session_container_root}",
                "-e",
                f"SESSION_CONFIGS_DIR={session_container_root}",
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
    return args


def _session_command(
    session: ResolvedSession,
    instance: SessionInstance,
    identity: str,
    *,
    force: bool,
    rewrite_formatting: bool,
    peer_address_overrides: dict[str, str],
    attach_mode: str,
    link_trace: bool | None = None,
    link_trace_interval_s: float | None = None,
    link_trace_modem_command: str | None = None,
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
    if link_trace:
        parts.append("--link-trace")
    if link_trace_interval_s is not None:
        parts.extend(["--link-trace-interval", str(link_trace_interval_s)])
    if link_trace_modem_command:
        parts.extend(["--link-trace-modem-command", link_trace_modem_command])
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
    cfg = _effective_session_config(session.host_dir, runtime)
    bindings = _resolve_bindings(
        cfg,
        runtime,
        peer=getattr(args, "peer", None),
        peer_address=getattr(args, "peer_address", None),
    )
    if not args.identity and not getattr(args, "auto_identity", True):
        raise RuntimeError("Missing --identity. Provide --identity <peer> or allow auto identity.")
    identity = args.identity or _auto_identity(bindings)
    if not args.identity:
        print(f"Auto-selected identity: {identity}")
    remote_name = _remote_peer_name(cfg, identity)
    instance = _resolve_session_instance(runtime, session, getattr(args, "instance_id", None))
    container_name = _container_name(remote_name, runtime, instance.instance_id)
    network_isolated = bool(getattr(args, "network_name", None))
    if not network_isolated and not getattr(args, "skip_conflict_check", False):
        conflicts = [name for name in _matching_com_containers(runtime, remote_name) if name != container_name]
        if conflicts and args.force:
            for conflict in conflicts:
                _stop_container_name(conflict, runtime, quiet_missing=True)
                print(f"Stopped conflicting session container: {conflict}")
        elif conflicts:
            raise _conflict_error(
                f"An incompatible session for identity {identity!r} is already running in this workspace.",
                conflicts,
                f"Stop it first with: rosotacom stop {shlex.quote(args.session_dir)} --identity {shlex.quote(identity)}"
                " (or pass --force to replace it, --skip-conflict-check to ignore it).",
            )
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
        peer_address_overrides=_binding_addresses(bindings),
        attach_mode=mode,
        link_trace=getattr(args, "link_trace", None),
        link_trace_interval_s=getattr(args, "link_trace_interval_s", None),
        link_trace_modem_command=getattr(args, "link_trace_modem_command", None),
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
    if not getattr(args, "scenario_managed", False) and not getattr(args, "smoke_managed", False):
        _write_instance_manifest(
            instance,
            session,
            runtime,
            cfg,
            identity=identity,
            container_name=container_name,
            mode=mode,
            bindings=bindings,
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
    cfg = _effective_session_config(session.host_dir, runtime)
    identity = args.identity
    if not identity and getattr(args, "auto_identity", False):
        bindings = _resolve_bindings(
            cfg,
            runtime,
            peer=getattr(args, "peer", None),
            peer_address=getattr(args, "peer_address", None),
        )
        identity = _auto_identity(bindings)
        print(f"Auto-selected identity: {identity}")
    containers = _identity_container_names(cfg, runtime, identity, all_states=True)
    if not containers:
        scope = f"identity {identity!r}" if identity else "any identity"
        print(f"No rosotacom session containers found in this workspace for {scope}.")
        return
    for container_name in containers:
        _stop_container_name(container_name, runtime)


def _resolve_scenario_context(
    args: argparse.Namespace,
    *,
    require_bindings: bool = True,
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
    cfg = _effective_session_config(session.host_dir, runtime)
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
        bindings = _resolve_bindings(
            cfg,
            runtime,
            peer=getattr(args, "peer", None),
            peer_address=getattr(args, "peer_address", None),
        )
        identity = _auto_identity(bindings)
        print(f"Auto-selected identity: {identity}")
    elif require_bindings:
        _resolve_bindings(
            cfg,
            runtime,
            peer=getattr(args, "peer", None),
            peer_address=getattr(args, "peer_address", None),
        )
    if not identity:
        raise RuntimeError("Missing --identity. Provide --identity <peer> or allow auto identity.")
    if identity not in peers:
        raise RuntimeError(f"--identity must be one of peers={list(peers.keys())}")
    # A scenario peer may legitimately run no local application -- a pure
    # receiver (e.g. remote_assist's `center`/a only consumes OTA telemetry).
    # `identity in peers` already validated it is a real peer, so a missing
    # applications entry just means "communication session only".
    applications = definition.applications.get(identity, ())
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
    instance_id: str | None = None,
) -> bool:
    if instance_id:
        containers = [_scenario_container_name(runtime, resolved.name, identity, application.name, instance_id)]
        containers = [name for name in containers if _container_exists(name)]
    else:
        containers = _matching_scenario_containers(runtime, resolved.name, identity, application.name, all_states=True)
    for container_name in containers:
        ros2docker_stop(
            config_file=application.ros2docker_config,
            override={"container_name": container_name},
        )
    return bool(containers)


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
            print(f"Application container not found: {resolved.name}/{identity}/{application.name}")
    communication_containers = _matching_com_containers(runtime, _remote_peer_name(cfg, identity), all_states=True)
    if not communication_containers and not quiet_missing:
        print(f"Communication container not found for identity: {identity}")
    for communication_container in communication_containers:
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
    communication_container = _container_name(_remote_peer_name(cfg, identity), runtime, instance.instance_id)
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
        communication_container,
        args,
    )
    print(f"rosotacom scenario instance: {instance.host_dir}")
    print(f"rosotacom scenario started: {resolved.name} ({identity})")
    print("Outer tmux prefix: Ctrl-b; send the inner catmux prefix with Ctrl-b Ctrl-b.")
    mode = _resolve_mode(getattr(args, "mode", "auto"))
    if mode == "attach":
        subprocess.run(_tmux_command(runtime, "attach-session", "-t", created_session), check=True)
    else:
        # Parity with `start_session` (detached): the catmux session builds the
        # image and ROS workspace asynchronously, so a bare detached start would
        # return before the communication bridge is up. Block until the comm
        # container has sourced its overlay so callers (the OTA smoke harness in
        # particular) verify against a ready bridge instead of racing a cold
        # image/workspace build. Generous ceiling: a scenario may build both the
        # communication and application images from scratch on a first run.
        print(f"Waiting for communication container to become ready: {communication_container}")
        _wait_for_container_ready(communication_container, timeout_s=600)
        print("Attach with: rosotacom scenario attach")
    return 0


def attach_scenario(args: argparse.Namespace) -> int:
    _require_tmux()
    _infer_active_scenario_selector(args, require_active=True)
    runtime, resolved, _definition, _session, _cfg, identity, _applications = _resolve_scenario_context(
        args,
        require_bindings=False,
    )
    tmux_session = _scenario_tmux_session(resolved.name, identity)
    if not _tmux_session_exists(runtime, tmux_session):
        raise RuntimeError(f"Scenario tmux session is not running: {tmux_session}")
    subprocess.run(_tmux_command(runtime, "attach-session", "-t", tmux_session), check=True)
    return 0


def stop_scenario(args: argparse.Namespace) -> int:
    _require_ros2docker()
    if not getattr(args, "scenario", None) or not getattr(args, "identity", None):
        _infer_active_scenario_selector(args, require_active=False)
    runtime, resolved, _definition, _session, cfg, identity, applications = _resolve_scenario_context(
        args,
        require_bindings=False,
    )
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
    instance_id = getattr(args, "instance_id", None) or _new_instance_id()
    container_name = _scenario_container_name(runtime, resolved.name, args.identity, application.name, instance_id)
    if _container_exists(container_name):
        ros2docker_stop(
            config_file=application.ros2docker_config,
            override={"container_name": container_name},
        )
    override: dict[str, object] = {
        "container_name": container_name,
        "image_name": _scenario_application_image_name(runtime, application),
    }
    network_name = getattr(args, "network_name", None)
    if network_name:
        base_run_args = load_config(application.ros2docker_config).get("run_args", []) or []
        override["run_args"] = _isolated_network_run_args(
            [str(arg) for arg in base_run_args],
            network_name,
            getattr(args, "network_ip", None),
        )
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
            for index, session_configs_dir in enumerate(runtime.session_configs_dir):
                line(
                    "OK",
                    "session definitions",
                    f"{session_configs_dir} -> {_session_config_container_root(index)}",
                )
        else:
            line("INFO", "session definitions", "not configured")
        if runtime.scenario_configs_dir:
            for scenario_configs_dir in runtime.scenario_configs_dir:
                line("OK", "scenario definitions", str(scenario_configs_dir))
        else:
            line("INFO", "scenario definitions", "not configured")
        line("OK", "session instances", f"{_session_instances_root(runtime)} -> {SESSION_INSTANCE_CONTAINER_DIR}")
        if runtime.deployment:
            deployment = _deployment_config(runtime)
            assert deployment is not None
            line("OK", "deployment", f"{runtime.deployment} ({len(deployment.hosts)} hosts)")
        else:
            line("INFO", "deployment", "not configured; literal peer arguments do not need one")
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


def ps_command(args: argparse.Namespace) -> int:
    """Answer "can I start something?" by classifying active rosotacom containers."""
    runtime = _load_runtime_config(args)
    prefix = _workspace_container_prefix(runtime)
    smoke_isolated: list[tuple[str, str]] = []
    host_shared: list[str] = []
    other_workspaces: list[str] = []
    for name, networks in _list_docker_containers():
        if not name.startswith("rosotacom_"):
            continue
        if not name.startswith(prefix):
            other_workspaces.append(name)
            continue
        smoke_nets = [net for net in networks if net.startswith("rosotacom_smoke_") or net == SMOKE_NETWORK_NAME]
        if smoke_nets:
            smoke_isolated.append((name, smoke_nets[0]))
        else:
            host_shared.append(name)
    print(f"Workspace {runtime.install_id} — active rosotacom containers:")
    if not smoke_isolated and not host_shared:
        print("  (none — you can start anything)")
    if smoke_isolated:
        print("  Smoke-isolated (parallel-safe; only a smoke run of the same target conflicts):")
        for name, network in sorted(smoke_isolated):
            print(f"    - {name} (network: {network})")
    if host_shared:
        print("  Host-shared (a new run for the same identity conflicts; stop with `rosotacom stop`):")
        for name in sorted(host_shared):
            print(f"    - {name}")
    if other_workspaces:
        print(f"Containers from other rosotacom workspaces: {len(other_workspaces)}")
        for name in sorted(other_workspaces):
            print(f"    - {name}")
    print("See CONCURRENCY.md for what can run in parallel.")
    return 0


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
            ["docker", "exec", container_name, "bash", "-c", command],
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


def _bounded_docker_name(name: str, *, max_len: int = 63) -> str:
    token = _sanitize_docker_name(name)
    if len(token) <= max_len:
        return token
    digest = hashlib.sha1(token.encode("utf-8")).hexdigest()[:10]
    return f"{token[: max_len - 11]}_{digest}"


def _smoke_subnet_from_token(token: str) -> str:
    value = int(hashlib.sha1(token.encode("utf-8")).hexdigest()[:8], 16)
    third_octet = value % 256
    fourth_octet = ((value // 256) % 32) * 8
    return f"10.137.{third_octet}.{fourth_octet}/29"


def _interactive_smoke_network_config(
    runtime: RuntimeConfig, target_type: str, target_name: str, instance_id: str
) -> tuple[str, str]:
    token = _bounded_docker_name(
        f"rosotacom_smoke_{runtime.install_id}_{target_type}_{target_name}_{_instance_name_token(instance_id)}"
    )
    return token, _smoke_subnet_from_token(token)


def _smoke_target_key(target_type: str, target_name: str) -> str:
    return _safe_path_token(f"{target_type}_{target_name}")


def _smoke_network_labels(runtime: RuntimeConfig, target_key: str) -> dict[str, str]:
    return {
        "rosotacom.kind": "smoke",
        "rosotacom.install": runtime.install_id,
        "rosotacom.target": target_key,
    }


def _matching_smoke_networks(runtime: RuntimeConfig, target_key: str) -> list[str]:
    command = ["docker", "network", "ls", "--format", "{{.Name}}"]
    for key, value in _smoke_network_labels(runtime, target_key).items():
        command.extend(["--filter", f"label={key}={value}"])
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"docker network ls failed: {(result.stderr or result.stdout).strip()}")
    return sorted(name for name in result.stdout.splitlines() if name)


def _smoke_network_active_containers(network_name: str) -> list[str]:
    result = subprocess.run(
        ["docker", "ps", "--filter", f"network={network_name}", "--format", "{{.Names}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker ps failed: {(result.stderr or result.stdout).strip()}")
    return sorted(name for name in result.stdout.splitlines() if name)


def _abort_on_active_smoke_run(runtime: RuntimeConfig, target_key: str, target_label: str, *, skip: bool) -> None:
    """Fail-safe preflight: refuse to start a second smoke run of the same target.

    Runs before any network or workspace allocation. Different targets and
    different workspaces stay parallel-safe; leftover networks without running
    containers are not conflicts (they are recreated per run anyway).
    """
    if skip:
        return
    conflicts: list[str] = []
    for network_name in _matching_smoke_networks(runtime, target_key):
        conflicts.extend(_smoke_network_active_containers(network_name))
    if conflicts:
        raise _conflict_error(
            f"A smoke run for {target_label} is already active in this workspace.",
            conflicts,
            f"Stop it first with: rosotacom smoke {shlex.quote(target_label)} --stop (interactive runs), wait for it"
            " to finish, or pass --skip-conflict-check. Different targets and workspaces run in parallel.",
        )


def _noninteractive_smoke_network_config(
    runtime: RuntimeConfig,
    session: ResolvedSession,
    instance_id: str,
) -> SmokeNetworkConfig:
    session_slug = _session_instance_slug(session, runtime)
    token = _bounded_docker_name(f"rosotacom_smoke_{runtime.install_id}_{session_slug}_{instance_id}")
    subnet = _smoke_subnet_from_token(token)
    return SmokeNetworkConfig(
        name=token,
        subnet=subnet,
        peer_ips=_smoke_peer_ips_for_subnet(["a", "b"], subnet),
    )


def _smoke_peer_ips_for_subnet(peers: list[str], subnet: str) -> dict[str, str]:
    sorted_peers = sorted(peers)
    match_24 = re.match(r"^(\d+\.\d+\.\d+)\.0/24$", subnet)
    if match_24:
        prefix = match_24.group(1)
        if len(sorted_peers) > 250:
            raise RuntimeError(f"Too many peers for a /24 smoke subnet: {len(sorted_peers)}")
        return {peer: f"{prefix}.{index + 2}" for index, peer in enumerate(sorted_peers)}

    match_29 = re.match(r"^(\d+\.\d+\.\d+)\.(\d+)/29$", subnet)
    if match_29:
        prefix = match_29.group(1)
        base = int(match_29.group(2))
        if base % 8 != 0 or base > 248:
            raise RuntimeError(f"Smoke /29 subnet must start on an 8-address boundary, got: {subnet}")
        if len(sorted_peers) > 5:
            raise RuntimeError(f"Too many peers for a /29 smoke subnet: {len(sorted_peers)}")
        return {peer: f"{prefix}.{base + index + 2}" for index, peer in enumerate(sorted_peers)}

    raise RuntimeError(f"Smoke requires a /24 or /29 IPv4 subnet, got: {subnet}")


def _smoke_peer_address_args(peer_ips: dict[str, str] | None = None) -> list[str]:
    source = peer_ips or SMOKE_PEER_IPS
    return [f"{peer}={source[peer]}" for peer in sorted(source)]


def _ensure_smoke_network(
    network_name: str = SMOKE_NETWORK_NAME,
    subnet: str = SMOKE_NETWORK_SUBNET,
    labels: dict[str, str] | None = None,
) -> None:
    # Recreate from a clean slate so a leftover network from a crashed run cannot
    # cause a subnet-overlap failure on create.
    _remove_smoke_network(network_name)
    command = ["docker", "network", "create", "--subnet", subnet]
    for key, value in (labels or {}).items():
        command.extend(["--label", f"{key}={value}"])
    command.append(network_name)
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to create smoke network {network_name} ({subnet}): {(result.stderr or result.stdout).strip()}"
        )


def _remove_smoke_network(network_name: str = SMOKE_NETWORK_NAME) -> None:
    subprocess.run(
        ["docker", "network", "rm", network_name],
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
    out.extend(["--network", network_name, "--cap-add", "NET_ADMIN"])
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


def _smoke_inbound_bridge_topic(cfg: dict[str, Any], source_peer_key: str, receiver_peer_key: str) -> str:
    peers = cfg.get("peers", {}) or {}
    if not isinstance(peers, dict):
        raise RuntimeError("Smoke verification requires a session config with peers.")
    source_name = _peer_com_name(peers, source_peer_key)
    heartbeat_topic = _smoke_forward_topic_for_inbound(
        cfg,
        source_peer_key,
        receiver_peer_key,
        _smoke_heartbeat_topic(cfg, source_peer_key),
    ).lstrip("/")
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
    if pipe.get("trickle_hz") is not None:
        # Mirror generate_session_files._postprocessed_topic: the receiver-side
        # trickle republishes the delivered topic at <final>/trickle, and that is
        # the stage the smoke should drive/assert.
        return str(pipe["final"]) + "/trickle"
    if pipe.get("compress"):
        return str(pipe["comp_in"])
    if pipe.get("ota_wrap"):
        return str(pipe["ota_in"])
    if pipe.get("framebridge") == "global_to_local":
        return str(pipe["fb_g2l_base"])
    transport = pipe.get("transport")
    if transport is not None and bool(getattr(transport, "local_republish", False)):
        return str(pipe["final"]) + "/raw"
    return str(pipe["final"])


def _smoke_source_publish_topic(
    cfg: dict[str, Any],
    source_peer_key: str,
    receiver_peer_key: str,
    entry: Any,
    pipe: dict[str, Any],
) -> str:
    source_topic = str(pipe["final"]) if pipe.get("framebridge") == "global_to_local" else entry.base
    return _smoke_forward_topic_for_inbound(cfg, source_peer_key, receiver_peer_key, source_topic)


def _smoke_expect_bounds(expect: Any) -> tuple[float | None, float | None, float | None]:
    if not isinstance(expect, dict):
        return None, None, None
    hz = expect.get("hz") or {}
    latency = expect.get("latency_ms") or {}
    hz_min = float(hz["min"]) if isinstance(hz, dict) and hz.get("min") is not None else None
    hz_max = float(hz["max"]) if isinstance(hz, dict) and hz.get("max") is not None else None
    max_delay_s = (
        float(latency["max"]) / 1000.0
        if isinstance(latency, dict) and latency.get("max") is not None and not latency.get("stage")
        else None
    )
    return hz_min, hz_max, max_delay_s


def _smoke_expect_mode(expect: Any) -> str:
    if not isinstance(expect, dict):
        return "stream"
    mode = str(expect.get("mode", "stream")).strip().lower() or "stream"
    return mode if mode in {"stream", "latched", "existence"} else "stream"


def _smoke_publish_qos(qos: Any) -> dict[str, Any] | None:
    if not isinstance(qos, dict):
        return None
    allowed = {"depth", "reliability", "durability", "history"}
    clean = {key: value for key, value in qos.items() if key in allowed and value is not None}
    return clean or None


def _smoke_publish_rate(expect: Any) -> float:
    hz_min, hz_max, _ = _smoke_expect_bounds(expect)
    if hz_min is not None and hz_max is not None:
        return (hz_min + hz_max) / 2.0
    if hz_min is not None:
        return max(1.0, hz_min * 1.5)
    if hz_max is not None:
        return max(0.5, hz_max / 2.0)
    return 5.0


def _smoke_native_publish_rate(expect: Any) -> float:
    """The rate the synthetic source should publish the NATIVE topic at.

    For rate-preserving topics this is derived from the expect hz bounds. But a
    rate-changing feature (drop/throttle) needs the native rate to be HIGHER than
    the asserted (received) rate, so an example may declare `expect.smoke_native_hz`
    to drive the source faster than the post-processing bounds it asserts."""
    if isinstance(expect, dict):
        native_hz = expect.get("smoke_native_hz")
        if native_hz is not None:
            return float(native_hz)
    return _smoke_publish_rate(expect)


def _smoke_source_publish_rate(expect: Any, pipe: dict[str, Any]) -> float:
    native_rate = _smoke_native_publish_rate(expect)
    if isinstance(expect, dict) and expect.get("smoke_native_hz") is not None:
        return native_rate
    drop_count = pipe.get("drop_count")
    window_size = pipe.get("window_size")
    if drop_count is None or window_size is None:
        return native_rate
    delivered_per_window = int(window_size) - int(drop_count)
    if delivered_per_window <= 0:
        return native_rate
    return native_rate * (int(window_size) / delivered_per_window)


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


SMOKE_CYCLONEDDS_MAX_AUTO_PARTICIPANT_INDEX = 99


def _smoke_default_cyclonedds_uri() -> str:
    return (
        "<CycloneDDS><Domain><Discovery><ParticipantIndex>auto</ParticipantIndex>"
        f"<MaxAutoParticipantIndex>{SMOKE_CYCLONEDDS_MAX_AUTO_PARTICIPANT_INDEX}</MaxAutoParticipantIndex>"
        "</Discovery></Domain></CycloneDDS>"
    )


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
    local_config_commands = _smoke_local_config_commands(config_container_dir, cfg, receiver_peer_key)
    if local_config_commands:
        commands.extend(local_config_commands)
    elif rmw_env == "rmw_cyclonedds_cpp":
        commands.append(f"export CYCLONEDDS_URI={shlex.quote(_smoke_default_cyclonedds_uri())}")
    return " && ".join(commands)


# --- Shared smoke/probe primitives -------------------------------------------
# Single source of truth for the heartbeat delivery bounds and the isolation
# probe topic used by local smoke and by external OTA harnesses.
SMOKE_HZ_MIN = 5.0
SMOKE_HZ_MAX = 20.0
SMOKE_MAX_DELAY_S = 1.0
SMOKE_PUBLISHER_DURATION_S = 3600.0
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
                    topic=_smoke_inbound_bridge_topic(cfg, source, receiver_peer_key),
                    label=f"{source}->{receiver_peer_key} inbound bridge heartbeat",
                    enforce_bounds=False,
                ),
                SmokeTopicSpec(
                    source_peer_key=source,
                    receiver_peer_key=receiver_peer_key,
                    topic=_smoke_receiver_final_topic(
                        cfg, source, receiver_peer_key, _smoke_heartbeat_topic(cfg, source)
                    ),
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
            if isinstance(entry.expect, dict) and entry.expect.get("smoke_probe") is False:
                continue
            pipe = _smoke_topic_pipeline(cfg, entry)
            final_topic = str(pipe["final"])
            if bool(shared.get("use_heartbeat", False)) and final_topic == _smoke_heartbeat_topic(cfg, src):
                continue
            postprocessed_topic = _smoke_postprocessed_topic(entry, pipe)
            received_topic = _smoke_receiver_final_topic(cfg, src, dst, postprocessed_topic)
            inbound_topic = _smoke_inbound_forward_topic(cfg, src, dst, final_topic)
            expect_hz_min, expect_hz_max, expect_max_delay_s = _smoke_expect_bounds(entry.expect)
            expect_mode = _smoke_expect_mode(entry.expect)
            specs.extend(
                [
                    SmokeTopicSpec(
                        source_peer_key=src,
                        receiver_peer_key=dst,
                        topic=inbound_topic,
                        label=f"{src}->{dst} inbound bridge topic",
                        enforce_bounds=False,
                        delivery_mode=expect_mode,
                    ),
                    SmokeTopicSpec(
                        source_peer_key=src,
                        receiver_peer_key=dst,
                        topic=received_topic,
                        label=f"{src}->{dst} final topic",
                        enforce_bounds=expect_mode == "stream"
                        and any(value is not None for value in (expect_hz_min, expect_hz_max, expect_max_delay_s)),
                        delivery_mode=expect_mode,
                        publish_topic=_smoke_source_publish_topic(cfg, src, dst, entry, pipe),
                        publish_type=entry.msg_type,
                        publish_rate=_smoke_source_publish_rate(entry.expect, pipe),
                        hz_min=expect_hz_min,
                        hz_max=expect_hz_max,
                        max_delay_s=expect_max_delay_s,
                        expected_size=66000 if entry.msg_type == "com_msgs/msg/SizedPayload" else None,
                        publish_qos=_smoke_publish_qos(entry.qos),
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
        log_line(f"Waiting for {label} ({topic}) in {container_name}")
        if spec.delivery_mode in {"latched", "existence"}:
            present = False
            for _ in range(10):
                if _topic_present(container_name, ros_setup, topic):
                    present = True
                    break
                time.sleep(1)
            if not present:
                errors.append(f"{label} ({topic}) not present in {container_name}")
                continue
            log_line(f"OK: {label} ({topic}) is present in {container_name} ({spec.delivery_mode})")
            log_line(_smoke_metric_line(label=label, topic=topic, container_name=container_name, hz=None, delay_s=None))
            continue
        output = _wait_for_topic_hz(container_name, ros_setup, topic)
        if detail_log:
            detail_log(f"\n--- {label} ({topic}) in {container_name} ---\n{output}")
        if "average rate" not in output:
            errors.append(f"{label} ({topic}) not publishing in {container_name}")
            continue
        log_line(f"OK: {label} ({topic}) is publishing in {container_name}")
        hz = _parse_topic_hz_rate(output)
        needs_delay = spec.use_default_bounds or spec.max_delay_s is not None
        if needs_delay:
            delay_output = _measure_topic_delay(container_name, ros_setup, topic)
            if detail_log:
                detail_log(f"\n--- delay {label} ({topic}) in {container_name} ---\n{delay_output}")
            delay_s = _parse_topic_delay_seconds(delay_output)
        else:
            delay_s = None
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
    if normalized in {"std_msgs/msg/Empty", "std_msgs/Empty"}:
        return "{}"
    if normalized in {"std_msgs/msg/Float32", "std_msgs/Float32", "std_msgs/msg/Float64", "std_msgs/Float64"}:
        return "{data: 1.0}"
    if normalized in {"geometry_msgs/msg/PoseStamped", "geometry_msgs/PoseStamped"}:
        return "{header: {frame_id: map}, pose: {orientation: {w: 1.0}}}"
    if normalized in {"geometry_msgs/msg/TwistStamped", "geometry_msgs/TwistStamped"}:
        return "{header: {frame_id: base_link}, twist: {linear: {x: 1.0}, angular: {z: 0.1}}}"
    if normalized in {"tf2_msgs/msg/TFMessage", "tf2_msgs/TFMessage"}:
        return "{transforms: [{header: {frame_id: map}, child_frame_id: base_link, transform: {rotation: {w: 1.0}}}]}"
    if normalized in {"visualization_msgs/msg/MarkerArray", "visualization_msgs/MarkerArray"}:
        return (
            "{markers: [{header: {frame_id: map}, ns: smoke, id: 1, type: 1, action: 0, "
            "pose: {orientation: {w: 1.0}}, scale: {x: 1.0, y: 1.0, z: 1.0}, "
            "color: {r: 0.0, g: 0.7, b: 1.0, a: 1.0}}]}"
        )
    if normalized == "nav_msgs/msg/OccupancyGrid":
        return (
            "{header: {frame_id: map}, "
            "info: {resolution: 0.5, width: 4, height: 4, origin: {orientation: {w: 1.0}}}, "
            "data: [0, 0, 0, 0, 0, 25, 50, 0, 0, 50, 100, 0, -1, -1, -1, -1]}"
        )
    if normalized in {"sensor_msgs/msg/CameraInfo", "sensor_msgs/CameraInfo"}:
        return "{header: {frame_id: camera}, height: 1, width: 1}"
    if normalized in {"sensor_msgs/msg/Image", "sensor_msgs/Image"}:
        return _smoke_sensor_image_message()
    if normalized in {"sensor_msgs/msg/CompressedImage", "sensor_msgs/CompressedImage"}:
        return (
            "{header: {frame_id: camera}, format: png, data: ["
            "137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82, "
            "0, 0, 0, 32, 0, 0, 0, 32, 8, 2, 0, 0, 0, 252, 24, 237, 163, "
            "0, 0, 0, 93, 73, 68, 65, 84, 72, 13, 181, 193, 1, 1, 0, 0, "
            "0, 130, 160, 124, 238, 243, 90, 17, 80, 115, 69, 205, 21, 53, "
            "87, 212, 92, 81, 115, 69, 205, 21, 53, 87, 212, 92, 81, 115, "
            "69, 205, 21, 53, 87, 212, 92, 81, 115, 69, 205, 21, 53, 87, "
            "212, 92, 81, 115, 69, 205, 21, 53, 87, 212, 92, 81, 115, 69, "
            "205, 21, 53, 87, 212, 92, 81, 115, 69, 205, 21, 53, 87, 212, "
            "92, 81, 115, 69, 205, 21, 53, 87, 212, 92, 13, 245, 197, 48, "
            "1, 194, 66, 132, 57, "
            "0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130]}"
        )
    if normalized in {"sensor_msgs/msg/NavSatFix", "sensor_msgs/NavSatFix"}:
        return (
            "{header: {frame_id: gps}, status: {status: 0, service: 1}, "
            "latitude: 48.0, longitude: 8.0, altitude: 100.0}"
        )
    if normalized in {"gps_msgs/msg/GPSFix", "gps_msgs/GPSFix"}:
        return (
            "{header: {frame_id: gps}, status: {status: 0, position_source: 1}, "
            "latitude: 48.0, longitude: 8.0, altitude: 100.0}"
        )
    if normalized == "com_msgs/msg/CompressedData":
        return "{header: {frame_id: map}, msg_type: 'anonymized', data: [0, 1, 2, 3]}"
    if normalized == "ffmpeg_image_transport_msgs/msg/FFMPEGPacket":
        return (
            "{header: {frame_id: camera}, width: 1, height: 1, encoding: h264, "
            "pts: 0, flags: 0, is_bigendian: false, data: [0, 1, 2, 3]}"
        )
    if normalized in {"geometry_msgs/msg/PointStamped", "geometry_msgs/PointStamped"}:
        # A deliberately STALE header.stamp (epoch+1000s == 1970) so the restamp
        # example is meaningful: without restamp the monitor sees an absurd age and
        # guards it to None (a latency_ms contract then fails); restamp rewrites the
        # stamp to "now" at send time, making OTA latency small and measurable.
        return "{header: {stamp: {sec: 1000, nanosec: 0}, frame_id: map}, point: {x: 1.0}}"
    raise RuntimeError(f"Smoke cannot synthesize a publisher for message type {msg_type!r}.")


def _smoke_expected_field(msg_type: str, field: str) -> str | None:
    """The value of `field` in the synthetic payload this peer publishes for `msg_type`,
    used as the content-integrity ground truth (what the receiver should observe)."""
    try:
        msg = yaml.safe_load(_smoke_publish_message(msg_type))
    except (RuntimeError, yaml.YAMLError):
        return None
    return str(msg[field]) if isinstance(msg, dict) and field in msg else None


def _content_integrity_specs(cfg: dict[str, Any], receiver_peer_key: str) -> list[tuple[str, str, str, str]]:
    """(topic, msg_type, field, expected) for the receiver's PASS-THROUGH String topics.

    Content integrity requires byte-equality, which only holds without a payload
    transform -- so we take crossed topics whose received name equals the published
    base (no restamp/framebridge/compress suffix) and whose payload field is known."""
    out: list[tuple[str, str, str, str]] = []
    for spec in _received_crossed_topics(cfg, receiver_peer_key):
        if not spec.publish_topic or spec.publish_type not in {"std_msgs/msg/String", "std_msgs/String"}:
            continue
        if spec.topic != spec.publish_topic:  # a transform ran -> not byte-equal
            continue
        expected = _smoke_expected_field(spec.publish_type, "data")
        if expected is not None:
            out.append((spec.topic, spec.publish_type, "data", expected))
    return out


def _smoke_sensor_image_message() -> str:
    width = 32
    height = 32
    data: list[str] = []
    for y in range(height):
        for x in range(width):
            base = (x * 7 + y * 13) % 256
            data.extend((str(base), str((base + 53) % 256), str((base + 101) % 256)))
    return (
        "{header: {frame_id: camera}, "
        f"height: {height}, width: {width}, encoding: rgb8, is_bigendian: false, step: {width * 3}, "
        "data: [" + ", ".join(data) + "]}"
    )


def _smoke_topic_pub_qos_args(qos: dict[str, Any] | None) -> str:
    if not qos:
        return ""
    args: list[str] = []
    if qos.get("reliability") is not None:
        args.extend(["--qos-reliability", str(qos["reliability"])])
    if qos.get("durability") is not None:
        args.extend(["--qos-durability", str(qos["durability"])])
    if qos.get("history") is not None:
        args.extend(["--qos-history", str(qos["history"])])
    if qos.get("depth") is not None:
        args.extend(["--qos-depth", str(qos["depth"])])
    return " ".join(shlex.quote(arg) for arg in args) + (" " if args else "")


def _smoke_publisher_command(spec: SmokeTopicSpec, ros_setup: str, duration: float) -> str:
    assert spec.publish_topic is not None and spec.publish_type is not None
    if spec.publish_type == "com_msgs/msg/SizedPayload":
        size = spec.expected_size or 66000
        return (
            f"{ros_setup} && timeout {duration} ros2 run com_py sized_publisher --ros-args "
            f"-p topic:={shlex.quote(spec.publish_topic)} -p size:={size} -p rate:={spec.publish_rate}"
        )
    message = _smoke_publish_message(spec.publish_type)
    qos_args = _smoke_topic_pub_qos_args(spec.publish_qos)
    return (
        f"{ros_setup} && timeout {duration} ros2 topic pub -r {spec.publish_rate} {qos_args}"
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
        log_line(
            f"Starting smoke publisher {spec.source_peer_key}->{spec.receiver_peer_key} "
            f"{spec.publish_topic} ({spec.publish_type}) in {container}"
        )
        subprocess.run(
            ["docker", "exec", "-d", container, "bash", "-c", cmd],
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
        ["docker", "exec", "-d", container_name, "bash", "-c", cmd], capture_output=True, text=True, check=False
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
    log_line(f"Starting isolation probe {topic} in {pub_container}")
    _publish_isolation_probe(pub_container, pub_setup, topic)
    try:
        live = False
        for _ in range(8):
            log_line(f"Waiting for isolation probe {topic} to advertise in {pub_container}")
            if _topic_present(pub_container, pub_setup, topic):
                live = True
                break
            time.sleep(1)
        if not live:
            return [f"isolation check inconclusive: {topic} never advertised in {pub_container}"]
        log_line(f"Checking that isolation probe {topic} is absent in {check_container}")
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
    cfg = _effective_session_config(session.host_dir, runtime)
    containers = _identity_container_names(cfg, runtime, identity)
    instance_id = getattr(args, "instance_id", None)
    if instance_id:
        instance_token = _instance_name_token(instance_id)
        containers = [
            name
            for name in containers
            if (parts := _split_workspace_container(name, runtime)) is not None and parts[0] == instance_token
        ]
    if not containers:
        raise RuntimeError(f"No running container found for identity {identity!r}")
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


def _normalize_echo_value(text: str) -> str:
    """The scalar value from a `ros2 topic echo --once --field <f>` output: the first
    meaningful line, with the message separator and one layer of quotes removed."""
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s == "---":
            continue
        if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
            s = s[1:-1]
        return s
    return ""


def content_matches(echo_stdout: str, expected: str) -> bool:
    """True if a topic-echo'd field byte-equals the expected sent value (pure)."""
    return _normalize_echo_value(echo_stdout) == str(expected).strip()


def _topic_echo_field_once(
    container: str, ros_setup: str, topic: str, msg_type: str, field: str, *, timeout_s: int = 20
) -> str:
    """Capture one delivered message's field via `ros2 topic echo --once`."""
    cmd = (
        f"{ros_setup} && timeout -k 2 {timeout_s} ros2 topic echo --once "
        f"{shlex.quote(topic)} {shlex.quote(msg_type)} --field {shlex.quote(field)}"
    )
    return _run_container_shell(container, cmd, timeout_s=_TOPIC_PROBE_EXEC_TIMEOUT_S).stdout or ""


def probe_content_command(args: argparse.Namespace) -> int:
    """Content integrity: assert a delivered topic's field byte-equals an expected value.

    Echoes one message of the delivered topic on this (receiver) peer and compares a
    field to --expect. For a pass-through topic (no restamp/framebridge/compress) the
    OTA must deliver the payload byte-identical, so this catches silent corruption /
    truncation / a wrong serialization that a presence or rate check would miss."""
    container, ros_setup, _ = _resolve_running_peer(args, args.identity)
    captured = _topic_echo_field_once(container, ros_setup, args.topic, args.type, args.field)
    ok = content_matches(captured, args.expect)
    got = _normalize_echo_value(captured)
    print(
        f"{args.topic}.{args.field} in {container} (identity {args.identity}): "
        f"got {got!r}, expected {args.expect!r} -> {'OK' if ok else 'MISMATCH'}"
    )
    return 0 if ok else 1


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


def _resolve_active_profile(
    runtime: RuntimeConfig,
    cfg: dict[str, Any],
    args: argparse.Namespace,
    *,
    allow_shaping: bool = True,
) -> str | None:
    """Resolve the selected network profile name (RFC 0004): `--profile` > `shared.profile`
    > unshaped. Validated against the configured profiles file when one is set."""
    from .network_profiles import load_profiles_file, resolve_profile_selection

    available = set(load_profiles_file(runtime.profiles_file)) if runtime.profiles_file is not None else None
    shared = cfg.get("shared")
    shared_default = shared.get("profile") if isinstance(shared, dict) else None
    return resolve_profile_selection(
        getattr(args, "profile", None),
        shared_default=shared_default,
        available=available,
        allow_shaping=allow_shaping,
    )


def _load_status_reports(logs_dir: Path) -> dict[str, Any]:
    reports: dict[str, Any] = {}
    for peer_dir in sorted(p for p in logs_dir.iterdir() if p.is_dir()):
        status_json = peer_dir / "status" / "status.json"
        if status_json.exists():
            reports[peer_dir.name] = json.loads(status_json.read_text(encoding="utf-8"))
    return reports


def calibrate_command(args: argparse.Namespace) -> int:
    """Report a replay bag's per-topic ground truth and (optionally) validate a
    session's `expect` against it -- the replay-only "contract calibration" of
    docs/rfcs/0002. Pure: reads only the bag's metadata.yaml, runs nothing."""
    from . import bag_ground_truth, status_eval

    gt = bag_ground_truth.bag_ground_truth(args.bag)
    if not gt:
        print(f"No topics found in bag metadata: {args.bag}", file=sys.stderr)
        return 1

    width = max(len(t) for t in gt)
    print(f"Bag ground truth ({len(gt)} topics) from {args.bag}:")
    for topic in sorted(gt):
        g = gt[topic]
        hz = f"{g['native_hz']:.1f} Hz" if g["native_hz"] is not None else "   -   "
        print(
            f"  {topic:<{width}}  {g['count']:>7} msgs  {hz:>10}  "
            f"{(g.get('durability') or ''):<16} {g.get('msg_type') or ''}"
        )

    warnings: list[str] = []
    session_dir = getattr(args, "session_dir", None)
    if session_dir:
        runtime = _load_runtime_config(args)
        session = _resolve_session(session_dir, runtime)
        cfg = _effective_session_config(session.host_dir, runtime)
        expect_by_topic = status_eval.expectations_from_cfg(cfg)
        warnings = bag_ground_truth.validate_expect_against_bag(expect_by_topic, gt)
        print()
        if warnings:
            print(f"CALIBRATION WARNINGS ({len(warnings)}):")
            for w in warnings:
                print(f"  ! {w}")
        else:
            print(f"OK: {len(expect_by_topic)} topic expectation(s) are consistent with the bag.")
    return 1 if warnings else 0


def expect_from_bag_command(args: argparse.Namespace) -> int:
    """Generate a mergeable whole-bag ``expect`` fragment from bag metadata."""
    from . import bag_ground_truth

    runtime = _load_runtime_config(args)
    session = _resolve_session(args.session_dir, runtime)
    cfg = _effective_session_config(session.host_dir, runtime)
    gt = bag_ground_truth.bag_ground_truth(args.bag)
    if not gt:
        print(f"No topics found in bag metadata: {args.bag}", file=sys.stderr)
        return 1

    fragment = bag_ground_truth.generate_whole_bag_expectations(
        cfg,
        gt,
        min_ratio=float(args.min_ratio),
        stream_min_hz=float(args.stream_min_hz),
    )
    generated_count = sum(len(entries) for entries in fragment.topics.values())
    if generated_count == 0:
        print(
            f"No session-carried topics from {session.host_dir} were found in bag metadata: {args.bag}",
            file=sys.stderr,
        )
        return 1

    text = bag_ground_truth.render_whole_bag_expect_fragment(fragment, bag=args.bag, session=session.host_dir)
    out = getattr(args, "out", None)
    if out:
        out_path = Path(out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {generated_count} generated expect block(s) to {out_path}")
    else:
        print(text)

    if fragment.missing_session_topics:
        print(
            "Warning: skipped session topics absent from the bag: " + ", ".join(fragment.missing_session_topics),
            file=sys.stderr,
        )
    if fragment.uncarried_bag_topics:
        print(
            "Warning: bag topics not carried by this session: " + ", ".join(fragment.uncarried_bag_topics),
            file=sys.stderr,
        )
    return 0


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
    link_expect = status_eval.link_expect_from_cfg(cfg)
    profile = _resolve_active_profile(runtime, cfg, args)
    ground_truth: dict[str, Any] | None = None
    bag = getattr(args, "bag", None)
    if bag:
        from . import bag_ground_truth

        ground_truth = bag_ground_truth.bag_ground_truth(bag)
    reports: dict[str, Any] = {}
    failures: list[str] = []

    # --suggest: emit a starter `expect` block from the current run instead of asserting.
    if getattr(args, "suggest", False):
        reports = _load_status_reports(logs_dir) if logs_dir.is_dir() else {}
        if not reports:
            print(f"rosotacom test --suggest: no status.json under {logs_dir}.", file=sys.stderr)
            return 1
        if profile:
            suggestions = status_eval.suggest_profile_band(reports, profile)
            print(f"# Suggested per-profile '{profile}' conditional band from this run (RFC 0004; author/narrow):")
        else:
            suggestions = status_eval.suggest_expectations(reports)
            print("# Suggested expect blocks from this run (author/narrow before committing):")
        print(yaml.safe_dump(suggestions, sort_keys=True, default_flow_style=False).rstrip())
        return 0

    wait_timeout = max(0.0, float(getattr(args, "timeout", 30.0)))
    if profile:
        print(f"rosotacom test: asserting the '{profile}' conditional band (RFC 0004 per-profile expect)")
    print(f"rosotacom test: waiting up to {wait_timeout:g}s for status reports under {logs_dir}")
    saw_reports = False
    while True:
        reports = _load_status_reports(logs_dir) if logs_dir.is_dir() else {}
        if reports:
            if not saw_reports:
                print(f"rosotacom test: evaluating {len(reports)} peer status report(s)")
                saw_reports = True
            failures = status_eval.evaluate_reports(reports, expect_by_topic, link_expect, ground_truth, profile)
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


def _interactive_smoke_session_arg(target: InteractiveSmokeTarget) -> str:
    if target.target_type == "scenario" and target.scenario_definition is not None:
        return target.scenario_definition.session
    return str(target.session.host_dir)


def _interactive_smoke_peer_address_args(peer_ips: dict[str, str]) -> list[str]:
    return [f"{peer}={ip}" for peer, ip in peer_ips.items()]


def _interactive_smoke_log_path(instance: SessionInstance, identity: str | None, component: str) -> Path:
    token = _safe_path_token(component)
    if identity is None:
        return instance.logs_host_dir / "interactive-smoke" / f"{token}.log"
    return instance.logs_host_dir / _safe_path_token(identity) / "interactive-smoke" / f"{token}.log"


def _interactive_smoke_communication_command(
    runtime: RuntimeConfig,
    target: InteractiveSmokeTarget,
    identity: str,
    instance: SessionInstance,
    peer_ips: dict[str, str],
    network_name: str,
    *,
    force: bool,
    link_trace_parts: list[str] | None = None,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "rosotacom",
        "start",
        _interactive_smoke_session_arg(target),
        "--identity",
        identity,
        "--mode",
        "attach",
        "--instance-id",
        instance.instance_id,
        "--smoke-managed",
        "--network-name",
        network_name,
        "--network-ip",
        peer_ips[identity],
        *_runtime_cli_args(runtime),
    ]
    command.append("--force" if force else "--no-force")
    command.extend(link_trace_parts or [])
    for override in _interactive_smoke_peer_address_args(peer_ips):
        command.extend(["--peer-address", override])
    return command


def _interactive_smoke_application_command(
    runtime: RuntimeConfig,
    target: InteractiveSmokeTarget,
    identity: str,
    application: ScenarioApplication,
    instance_id: str,
    network_name: str,
) -> list[str]:
    assert target.scenario is not None
    return _scenario_application_command(
        runtime,
        target.scenario,
        identity,
        application,
        instance_id,
        network_name=network_name,
    )


def _interactive_smoke_verify_command(
    runtime: RuntimeConfig,
    target: InteractiveSmokeTarget,
    instance: SessionInstance,
    peer_ips: dict[str, str],
) -> list[str]:
    target_arg = target.scenario.name if target.scenario is not None else str(target.session.host_dir)
    command = [
        sys.executable,
        "-m",
        "rosotacom",
        "smoke",
        target_arg,
        "--interactive",
        "--verify-only",
        "--target-type",
        target.target_type,
        "--instance-id",
        instance.instance_id,
        *_runtime_cli_args(runtime),
    ]
    for override in _interactive_smoke_peer_address_args(peer_ips):
        command.extend(["--peer-address", override])
    return command


def _interactive_smoke_status_command(
    runtime: RuntimeConfig,
    target: InteractiveSmokeTarget,
    instance: SessionInstance,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "rosotacom",
        "status",
        _interactive_smoke_session_arg(target),
        "--instance-id",
        instance.instance_id,
        "--watch",
        *_runtime_cli_args(runtime),
    ]


def _host_shell(script: str) -> str:
    return shlex.join(["bash", "-lc", script])


def _wait_for_peer_spec_script(instance: SessionInstance, identity: str) -> str:
    spec = instance.config_host_dir / identity / "session_specification.yaml"
    quoted = shlex.quote(str(spec))
    return f"until [ -f {quoted} ]; do echo '[INFO] waiting for generated config: {quoted}'; sleep 2; done"


def _wait_for_container_running_script(container_name: str) -> str:
    quoted = shlex.quote(container_name)
    return (
        f"until [ \"$(docker inspect -f '{{{{.State.Running}}}}' {quoted} 2>/dev/null)\" = true ]; do "
        f"echo '[INFO] waiting for container: {quoted}'; sleep 2; done"
    )


def _wait_for_container_ready_script(container_name: str, timeout_s: int = 600) -> str:
    quoted = shlex.quote(container_name)
    marker = shlex.quote("Sourced ROS 2 workspace overlay")
    return (
        f"end=$(( $(date +%s) + {timeout_s} )); "
        "while :; do "
        f"if docker logs {quoted} 2>&1 | grep -Fq {marker}; then "
        f"echo '[INFO] container ready: {quoted}'; break; "
        "fi; "
        f"state=$(docker inspect -f '{{{{.State.Running}}}}' {quoted} 2>/dev/null || true); "
        f"if [ \"$state\" = false ]; then echo '[ERROR] container exited before ready: {quoted}' >&2; exit 1; fi; "
        'if [ "$(date +%s)" -ge "$end" ]; then '
        f"echo '[ERROR] timed out waiting for container readiness: {quoted}' >&2; exit 1; "
        "fi; "
        f"echo '[INFO] waiting for container readiness: {quoted}'; sleep 2; "
        "done"
    )


def _create_interactive_smoke_tmux(
    runtime: RuntimeConfig,
    target: InteractiveSmokeTarget,
    instance: SessionInstance,
    peer_ips: dict[str, str],
    network_name: str,
    link_trace_parts: list[str] | None = None,
) -> str:
    session_name = _interactive_smoke_tmux_session(target.target_type, target.name)
    peers = sorted(peer_ips)
    first_peer = peers[0]
    first_command = shlex.join(
        _interactive_smoke_communication_command(
            runtime,
            target,
            first_peer,
            instance,
            peer_ips,
            network_name,
            force=True,
            link_trace_parts=link_trace_parts,
        )
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
            _safe_path_token(f"{first_peer}_communication"),
            _host_shell(first_command),
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    first_pane = created.stdout.strip()
    subprocess.run(
        _tmux_command(runtime, "set-window-option", "-g", "-t", session_name, "remain-on-exit", "on"),
        check=True,
    )
    subprocess.run(_tmux_command(runtime, "set-option", "-t", session_name, "prefix", "C-b"), check=True)
    subprocess.run(
        _tmux_command(runtime, "set-option", "-t", session_name, "@rosotacom_smoke_target", target.name),
        check=True,
    )
    subprocess.run(
        _tmux_command(runtime, "set-option", "-t", session_name, "@rosotacom_smoke_target_type", target.target_type),
        check=True,
    )
    subprocess.run(
        _tmux_command(runtime, "set-option", "-t", session_name, "@rosotacom_smoke_instance", instance.instance_id),
        check=True,
    )
    subprocess.run(
        _tmux_command(runtime, "set-option", "-t", session_name, "@rosotacom_smoke_network", network_name),
        check=True,
    )
    subprocess.run(_tmux_command(runtime, "bind-key", "-T", "prefix", "C-b", "send-prefix"), check=True)
    subprocess.run(
        _tmux_command(runtime, "set-option", "-t", session_name, "pane-border-status", "top"),
        check=True,
    )
    subprocess.run(
        _tmux_command(runtime, "set-option", "-t", session_name, "pane-border-format", " #{pane_title} "),
        check=True,
    )
    subprocess.run(
        _tmux_command(
            runtime,
            "set-option",
            "-t",
            session_name,
            "status-right",
            " interactive smoke | windows: C-b n/p | inner catmux: C-b C-b ",
        ),
        check=True,
    )
    subprocess.run(
        _tmux_command(runtime, "select-pane", "-t", first_pane, "-T", f"{first_peer}:communication"),
        check=True,
    )
    _attach_tmux_pipe(runtime, first_pane, _interactive_smoke_log_path(instance, first_peer, "communication"))

    for peer in peers[1:]:
        command = shlex.join(
            _interactive_smoke_communication_command(
                runtime,
                target,
                peer,
                instance,
                peer_ips,
                network_name,
                force=False,
                link_trace_parts=link_trace_parts,
            )
        )
        script = f"{_wait_for_peer_spec_script(instance, peer)}; exec {command}"
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
                _safe_path_token(f"{peer}_communication"),
                _host_shell(script),
            ),
            text=True,
            capture_output=True,
            check=True,
        )
        pane_id = created_window.stdout.strip()
        subprocess.run(_tmux_command(runtime, "select-pane", "-t", pane_id, "-T", f"{peer}:communication"), check=True)
        _attach_tmux_pipe(runtime, pane_id, _interactive_smoke_log_path(instance, peer, "communication"))

    if target.target_type == "scenario" and target.scenario_definition is not None:
        for peer in peers:
            communication_container = _container_name(
                _remote_peer_name(target.cfg, peer), runtime, instance.instance_id
            )
            app_network = f"container:{communication_container}"
            for application in target.scenario_definition.applications.get(peer, ()):
                command = shlex.join(
                    _interactive_smoke_application_command(
                        runtime, target, peer, application, instance.instance_id, app_network
                    )
                )
                script = (
                    f"{_wait_for_peer_spec_script(instance, peer)}; "
                    f"{_wait_for_container_ready_script(communication_container)}; "
                    f"exec {command}"
                )
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
                        _safe_path_token(f"{peer}_{application.name}"),
                        _host_shell(script),
                    ),
                    text=True,
                    capture_output=True,
                    check=True,
                )
                pane_id = created_window.stdout.strip()
                subprocess.run(
                    _tmux_command(
                        runtime,
                        "select-pane",
                        "-t",
                        pane_id,
                        "-T",
                        f"{peer}:application:{application.name}",
                    ),
                    check=True,
                )
                _attach_tmux_pipe(
                    runtime,
                    pane_id,
                    _interactive_smoke_log_path(instance, peer, f"application-{application.name}"),
                )

    verify = shlex.join(_interactive_smoke_verify_command(runtime, target, instance, peer_ips))
    status_watch = shlex.join(_interactive_smoke_status_command(runtime, target, instance))
    verify_script = (
        f"echo '[INFO] starting interactive smoke verification for {target.name}'; "
        f"{verify}; rc=$?; echo; echo '[INFO] one-shot verification exited with status' \"$rc\"; "
        "echo '[INFO] verification log remains in this pane'; exec bash"
    )
    verification = subprocess.run(
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
            "verification",
            _host_shell(verify_script),
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    verification_pane = verification.stdout.strip()
    subprocess.run(_tmux_command(runtime, "select-pane", "-t", verification_pane, "-T", "verification"), check=True)
    _attach_tmux_pipe(runtime, verification_pane, _interactive_smoke_log_path(instance, None, "verification"))
    status_script = (
        f"echo '[INFO] starting live status watch for {target.name}'; "
        f"echo '[INFO] waiting for status artifacts from instance {instance.instance_id}'; "
        f"exec {status_watch}"
    )
    _create_tmux_split_below(
        runtime,
        verification_pane,
        "status",
        _host_shell(status_script),
        log_path=_interactive_smoke_log_path(instance, None, "status-watch"),
    )
    subprocess.run(
        _tmux_command(runtime, "select-layout", "-t", f"{session_name}:verification", "even-vertical"),
        check=True,
    )
    subprocess.run(_tmux_command(runtime, "select-window", "-t", f"{session_name}:verification"), check=True)
    return session_name


def _find_latest_interactive_smoke_instance(
    runtime: RuntimeConfig,
    target_type: str,
    target_name: str,
    instance_id: str | None = None,
) -> Path | None:
    root = _session_instances_root(runtime)
    run_key = _interactive_smoke_run_key(target_type, target_name)
    candidates = sorted(root.glob("*/*/manifest.yaml"), reverse=True)
    for manifest_path in candidates:
        manifest = _load_yaml_file(manifest_path)
        if instance_id and manifest.get("instance_id") != _safe_path_token(instance_id):
            continue
        smoke_runs = manifest.get("interactive_smoke_runs") or {}
        if run_key in smoke_runs:
            return manifest_path.parent.resolve()
    return None


def _interactive_smoke_verify(args: argparse.Namespace) -> int:
    runtime = _load_runtime_config(args)
    target = _resolve_interactive_smoke_target(
        getattr(args, "session_dir", None),
        runtime,
        getattr(args, "target_type", "auto"),
    )
    instance_id = getattr(args, "instance_id", None)
    if not instance_id:
        # Container names are instance-scoped, so verification must join the
        # active run instead of allocating a fresh instance.
        active = _infer_active_interactive_smoke_run(runtime, target.name, target.target_type)
        instance_id = active.instance_id
    instance = _resolve_session_instance(runtime, target.session, instance_id)
    smoke_log = instance.logs_host_dir / "interactive-smoke-verification.log"

    def log_line(message: str) -> None:
        print(message)
        _append_log(smoke_log, message)

    peers = _require_two_peer_smoke_cfg(target.cfg, target.name)
    containers = {
        peer: _container_name(_remote_peer_name(target.cfg, peer), runtime, instance.instance_id) for peer in peers
    }
    log_line(f"Interactive smoke verification starting: {target.name} ({target.target_type})")
    log_line(f"Verification artifacts: {instance.host_dir}")
    for peer, container in containers.items():
        log_line(f"Waiting for communication container {peer}: {container}")
        _wait_for_container_ready(container, timeout_s=360)
    ros_setups = {peer: _smoke_ros_setup(instance.config_container_dir, target.cfg, peer) for peer in peers}

    if target.target_type == "session":
        log_line("Starting synthetic publishers for session target")
        _start_smoke_topic_publishers(
            containers,
            ros_setups,
            target.cfg,
            log_line=log_line,
            duration=SMOKE_PUBLISHER_DURATION_S,
        )

    errors: list[str] = []
    for peer in peers:
        log_line(f"Checking crossed topic delivery for receiver {peer}")
        errors += _verify_received_topics(
            containers[peer],
            ros_setups[peer],
            target.cfg,
            peer,
            log_line=log_line,
            detail_log=lambda message: _append_log(smoke_log, message),
        )
    errors += _verify_isolation(
        containers[peers[0]],
        ros_setups[peers[0]],
        containers[peers[1]],
        ros_setups[peers[1]],
        ISOLATION_PROBE_TOPIC,
        log_line=log_line,
    )
    log_line("Running rosotacom test against status reports")
    test_rc = test_command(
        argparse.Namespace(
            rosotacom_config=args.rosotacom_config,
            ros2docker_config=args.ros2docker_config,
            session_configs_dir=args.session_configs_dir,
            scenario_configs_dir=getattr(args, "scenario_configs_dir", None),
            session_instances_dir=getattr(args, "session_instances_dir", None),
            deployment=args.deployment,
            session_dir=_interactive_smoke_session_arg(target),
            instance_id=instance.instance_id,
            timeout=120.0,
            interval=2.0,
        )
    )
    if test_rc != 0:
        errors.append("rosotacom test failed for the session self-report")
    if errors:
        for error in errors:
            log_line(f"ERROR: {error}")
        return 1
    log_line("INTERACTIVE SMOKE OK")
    return 0


def _start_interactive_smoke(args: argparse.Namespace) -> int:
    _require_ros2docker()
    _require_tmux()
    runtime = _load_runtime_config(args)
    target = _resolve_interactive_smoke_target(
        getattr(args, "session_dir", None),
        runtime,
        getattr(args, "target_type", "auto"),
    )
    tmux_session = _interactive_smoke_tmux_session(target.target_type, target.name)
    mode = _resolve_mode(getattr(args, "mode", "auto"))
    if _tmux_session_exists(runtime, tmux_session):
        print(f"Interactive smoke already running: {target.name} ({target.target_type})")
        if mode == "attach":
            subprocess.run(_tmux_command(runtime, "attach-session", "-t", tmux_session), check=True)
        else:
            print(f"Attach with: rosotacom smoke {shlex.quote(target.name)} --interactive")
        return 0

    target_key = _smoke_target_key(target.target_type, target.name)
    _abort_on_active_smoke_run(runtime, target_key, target.name, skip=bool(getattr(args, "skip_conflict_check", False)))
    instance = _resolve_session_instance(
        runtime,
        target.session,
        getattr(args, "instance_id", None) or _new_instance_id(),
    )
    network_name, network_subnet = _interactive_smoke_network_config(
        runtime, target.target_type, target.name, instance.instance_id
    )
    peer_ips = _smoke_peer_ips_for_subnet(_require_two_peer_smoke_cfg(target.cfg, target.name), network_subnet)
    _ensure_smoke_network(network_name, network_subnet, labels=_smoke_network_labels(runtime, target_key))
    _write_interactive_smoke_manifest(
        instance,
        target,
        runtime,
        peer_ips=peer_ips,
        network_name=network_name,
        network_subnet=network_subnet,
        tmux_session=tmux_session,
    )
    created_session = _create_interactive_smoke_tmux(
        runtime,
        target,
        instance,
        peer_ips,
        network_name,
        link_trace_parts=_link_trace_parts_from_args(args),
    )
    print(f"rosotacom interactive smoke instance: {instance.host_dir}")
    print(f"rosotacom interactive smoke started: {target.name} ({target.target_type})")
    print(f"Smoke peers isolated on docker network {network_name} ({network_subnet})")
    print("Outer tmux prefix: Ctrl-b; send the inner catmux prefix with Ctrl-b Ctrl-b.")
    if mode == "attach":
        subprocess.run(_tmux_command(runtime, "attach-session", "-t", created_session), check=True)
    else:
        print(f"Attach with: rosotacom smoke {shlex.quote(target.name)} --interactive")
        print(f"Stop with: rosotacom smoke {shlex.quote(target.name)} --interactive --stop")
    return 0


def _stop_interactive_smoke(args: argparse.Namespace) -> int:
    _require_ros2docker()
    runtime = _load_runtime_config(args)
    target_arg = getattr(args, "session_dir", None)
    target_type = getattr(args, "target_type", "auto")
    active: ActiveInteractiveSmokeRun | None
    instance_id: str | None
    try:
        active = _infer_active_interactive_smoke_run(runtime, target_arg, target_type)
        target = _resolve_interactive_smoke_target(active.target, runtime, active.target_type)
        network_name = active.network_name
        tmux_session = active.tmux_session
        instance_id = active.instance_id
    except RuntimeError:
        if not target_arg:
            raise
        active = None
        target = _resolve_interactive_smoke_target(target_arg, runtime, target_type)
        network_name = None
        tmux_session = _interactive_smoke_tmux_session(target.target_type, target.name)
        instance_id = getattr(args, "instance_id", None)

    peers = _require_two_peer_smoke_cfg(target.cfg, target.name)
    if instance_id:
        peer_containers = {
            peer: [_container_name(_remote_peer_name(target.cfg, peer), runtime, instance_id)] for peer in peers
        }
    else:
        # Without run metadata, fall back to discovering every instance of this
        # workspace's communication containers for the smoke peers.
        peer_containers = {
            peer: _matching_com_containers(runtime, _remote_peer_name(target.cfg, peer), all_states=True)
            for peer in peers
        }
    if target.target_type == "session":
        publisher_containers = {peer: names[0] for peer, names in peer_containers.items() if names}
        if publisher_containers:
            _stop_smoke_topic_publishers(publisher_containers, _smoke_publish_specs(target.cfg))
    if target.target_type == "scenario" and target.scenario is not None and target.scenario_definition is not None:
        for peer in peers:
            for application in target.scenario_definition.applications.get(peer, ()):
                if _stop_scenario_application(runtime, target.scenario, peer, application, instance_id):
                    print(f"Stopped scenario application: {peer}/{application.name}")
    for peer, names in peer_containers.items():
        for container in names:
            if _stop_container_name(container, runtime, quiet_missing=True):
                print(f"Stopped communication container: {peer} ({container})")
    if _kill_scenario_tmux(runtime, tmux_session):
        print(f"Stopped interactive smoke tmux session: {tmux_session}")
    if network_name:
        _remove_smoke_network(network_name)
    else:
        for leftover in _matching_smoke_networks(runtime, _smoke_target_key(target.target_type, target.name)):
            _remove_smoke_network(leftover)
    instance_dir = _find_latest_interactive_smoke_instance(
        runtime,
        target.target_type,
        target.name,
        instance_id,
    )
    if instance_dir:
        _mark_interactive_smoke_stopped(instance_dir, target.target_type, target.name)
    if active is None:
        print(f"Interactive smoke cleanup attempted for: {target.name} ({target.target_type})")
    return 0


def _list_interactive_smoke(args: argparse.Namespace) -> int:
    runtime = _load_runtime_config(args)
    print(_format_active_interactive_smoke_runs(_active_interactive_smoke_runs(runtime)))
    return 0


def smoke(args: argparse.Namespace) -> int:
    if getattr(args, "verify_only", False):
        return _interactive_smoke_verify(args)
    if getattr(args, "interactive_list", False):
        return _list_interactive_smoke(args)
    if getattr(args, "interactive_stop", False):
        return _stop_interactive_smoke(args)
    if getattr(args, "interactive", False):
        return _start_interactive_smoke(args)
    if not args.local:
        raise RuntimeError("Only --local smoke mode is implemented.")
    session_dir = args.session_dir or DEFAULT_SMOKE_SESSION
    runtime = _load_runtime_config(args)
    session = _resolve_session(session_dir, runtime)
    smoke_target_key = _smoke_target_key("session", session.host_dir.name)
    _abort_on_active_smoke_run(
        runtime, smoke_target_key, session.host_dir.name, skip=bool(getattr(args, "skip_conflict_check", False))
    )
    instance_id = getattr(args, "instance_id", None) or _new_instance_id()
    smoke_instance = _resolve_session_instance(runtime, session, instance_id)
    smoke_network = _noninteractive_smoke_network_config(runtime, session, smoke_instance.instance_id)
    smoke_log = smoke_instance.logs_host_dir / "smoke-verification.log"

    def log_line(message: str) -> None:
        print(message)
        _append_log(smoke_log, message)

    peer_address_args = _smoke_peer_address_args(smoke_network.peer_ips)
    cfg = _effective_session_config(session.host_dir, runtime)
    common = {
        "rosotacom_config": args.rosotacom_config,
        "ros2docker_config": args.ros2docker_config,
        "session_configs_dir": args.session_configs_dir,
        "session_instances_dir": getattr(args, "session_instances_dir", None),
        "deployment": args.deployment,
        "session_dir": session_dir,
        "mode": "detached",
        # Names are instance-scoped, so there is nothing to force-replace; a
        # parallel run's containers must never be stopped from here.
        "force": False,
        "rewrite_formatting": False,
        "peer": [],
        "peer_address": peer_address_args,
        "instance_id": smoke_instance.instance_id,
        "network_name": smoke_network.name,
        "link_trace": getattr(args, "link_trace", None),
        "link_trace_interval_s": getattr(args, "link_trace_interval_s", None),
        "link_trace_modem_command": getattr(args, "link_trace_modem_command", None),
    }

    log_line(f"Starting local smoke test with PEER_ADDRESSES={', '.join(peer_address_args)}")
    log_line(f"Smoke peers isolated on docker network {smoke_network.name} ({smoke_network.subnet})")
    log_line(f"Smoke artifacts: {smoke_instance.host_dir}")
    a_container = None
    b_container = None
    smoke_publishers: list[SmokeTopicSpec] = []
    try:
        _ensure_smoke_network(
            smoke_network.name, smoke_network.subnet, labels=_smoke_network_labels(runtime, smoke_target_key)
        )
        a_container = start_session(
            argparse.Namespace(**common, identity="a", auto_identity=True, network_ip=smoke_network.peer_ips["a"])
        )
        b_container = start_session(
            argparse.Namespace(**common, identity="b", auto_identity=True, network_ip=smoke_network.peer_ips["b"])
        )

        plugin_text = "\n".join(
            (smoke_instance.config_host_dir / peer / "plugin.yaml").read_text(encoding="utf-8") for peer in ("a", "b")
        )
        expected_addresses = {arg.split("=", 1)[1] for arg in peer_address_args}
        if any(address not in plugin_text for address in expected_addresses):
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
        log_line("Running rosotacom test against status reports")
        test_rc = test_command(
            argparse.Namespace(
                rosotacom_config=args.rosotacom_config,
                ros2docker_config=args.ros2docker_config,
                session_configs_dir=args.session_configs_dir,
                session_instances_dir=getattr(args, "session_instances_dir", None),
                deployment=args.deployment,
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
            for cleanup_container in [a_container, b_container]:
                if cleanup_container:
                    _stop_container_name(cleanup_container, runtime)
            _remove_smoke_network(smoke_network.name)
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


def metrics_command(args: argparse.Namespace) -> int:
    from rosotacom.transit import (
        join_transit_records,
        load_transit_records,
        summarize_transit_records,
    )

    paths = [Path(path).expanduser().resolve() for path in args.events]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise RuntimeError(f"events file not found: {', '.join(missing)}")
    records = load_transit_records(paths)
    payload = join_transit_records(records) if args.records else summarize_transit_records(records)
    print(json.dumps(payload, indent=2))
    return 0


def geomap_command(args: argparse.Namespace) -> int:
    from rosotacom.geomap import (
        join_metric_samples,
        load_gps_csv,
        load_gps_from_bag,
        load_metrics_for_inputs,
        write_geo_csv,
        write_geomap_html,
        write_manifest,
    )

    gps_csv = cast(str | None, getattr(args, "gps_csv", None))
    bag = cast(str | None, getattr(args, "bag", None))
    if bool(gps_csv) == bool(bag):
        raise RuntimeError("provide exactly one GPS source: --gps-csv or --bag")
    if bag and not getattr(args, "gps_topic", None):
        raise RuntimeError("--bag requires --gps-topic")

    if gps_csv:
        gps_samples = load_gps_csv(gps_csv)
    else:
        assert bag is not None
        gps_samples = load_gps_from_bag(
            bag,
            topic=args.gps_topic,
            storage_id=args.storage_id,
            time_source=args.gps_time_source,
            origin_latitude=args.origin_lat,
            origin_longitude=args.origin_lon,
        )
    metric_samples = load_metrics_for_inputs(
        traces=tuple(args.trace or ()),
        events=tuple(args.events or ()),
        event_bin_s=args.event_bin_s,
        event_time_field=args.event_time_field,
        event_topic=args.event_topic,
    )
    if not metric_samples:
        raise RuntimeError("no metric samples found; provide at least one non-empty --trace or --events input")

    joined = join_metric_samples(
        gps_samples,
        metric_samples,
        metric=args.metric,
        trace_to_gps_offset_s=args.trace_to_gps_offset_s,
        max_gap_s=args.max_gap_s,
    )
    if not joined:
        raise RuntimeError(
            "no georeferenced samples after timestamp join; check --trace-to-gps-offset-s, --max-gap-s, and --metric"
        )

    write_geo_csv(joined, args.out_csv)
    write_geomap_html(joined, args.out_html, metric=args.metric, title=args.title)
    if args.manifest:
        write_manifest(args.manifest, csv_path=args.out_csv, html_path=args.out_html, sample_count=len(joined))

    print(f"Wrote {len(joined)} georeferenced samples to {args.out_csv}")
    print(f"Wrote geo link-quality map to {args.out_html}")
    return 0


def profile_from_trace_command(args: argparse.Namespace) -> int:
    from rosotacom.trace_profiles import TraceProfileConfig, parse_window, write_trace_profile

    window_start_s, window_end_s = parse_window(getattr(args, "window", None))
    directions: tuple[str, ...]
    if args.directions == "both":
        directions = ("uplink", "downlink")
    else:
        directions = (str(args.directions),)
    mode = str(args.mode)
    config = TraceProfileConfig(
        name=args.name or ("trace_static" if mode == "static" else "trace_replay"),
        directions=directions,
        min_segment_s=float(args.min_segment_s),
        change_sensitivity=float(args.change_sensitivity),
        gap_outage_after_s=args.gap_outage_after_s,
        loss_outage_min_s=args.loss_outage_min_s,
        window_start_s=window_start_s,
        window_end_s=window_end_s,
        rate_percentile=float(args.rate_percentile),
        delay_percentile=float(args.delay_percentile),
        jitter_spread_percentile=float(args.jitter_spread_percentile),
    )
    text = write_trace_profile(args.trace, args.out, mode=mode, config=config)
    if args.out:
        print(f"Wrote trace-derived profile {config.name!r} to {args.out}")
    else:
        print(text, end="")
    return 0


def report_command(args: argparse.Namespace) -> int:
    from rosotacom.forensics import DetectionConfig, build_report, parse_timeline_anchor, render_markdown, write_report

    instance_dir = Path(args.instance_dir).expanduser().resolve()
    config = DetectionConfig(
        bin_s=args.bin_s,
        loss_burst_min=args.loss_burst_min,
        latency_baseline_window=args.latency_baseline_window,
        latency_baseline_min=args.latency_baseline_min,
        latency_ratio=args.latency_ratio,
        latency_min_delta_ms=args.latency_min_delta_ms,
        latency_min_run=args.latency_min_run,
        rate_collapse_fraction=args.rate_collapse_fraction,
        rate_collapse_min_bins=args.rate_collapse_min_bins,
    )
    report = build_report(
        instance_dir,
        config=config,
        peers=tuple(args.peer or ()),
        profile=args.profile,
        profiles_file=args.profiles_file,
        timeline_anchor=parse_timeline_anchor(args.timeline_anchor),
    )
    out_dir = Path(args.out).expanduser().resolve() if args.out else instance_dir / "report"
    written = write_report(report, out_dir, figures=not args.no_figures)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_markdown(report))
    print(f"report: {written['json']}", file=sys.stderr)
    print(f"summary: {written['markdown']}", file=sys.stderr)
    for figure in written["figures"]:
        print(f"figure: {figure}", file=sys.stderr)
    if written["figures_note"]:
        print(written["figures_note"], file=sys.stderr)
    return 0


def stream_stats_command(args: argparse.Namespace) -> int:
    from rosotacom.stream_stats import build_report, load_sources, render_markdown, write_report

    sources = load_sources(
        bag_specs=tuple(args.bag or ()),
        events_specs=tuple(args.events or ()),
        storage_id=args.storage_id,
    )
    report = build_report(sources, argv=["rosotacom", "stream-stats", *sys.argv[2:]])
    written: dict[str, Path] = {}
    if args.out:
        written = write_report(report, Path(args.out).expanduser().resolve())
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_markdown(report), end="")
    for kind, path in written.items():
        print(f"{kind}: {path}", file=sys.stderr)
    return 0


def _add_common_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--rosotacom-config",
        "--project",
        dest="rosotacom_config",
        help="Path to rosotacom.yaml (overrides cwd discovery and the global default).",
    )
    parser.add_argument("-f", "--ros2docker-config", help="Path to ros2docker JSON config.")
    parser.add_argument(
        "--session-configs-dir",
        action="append",
        help="Host directory containing named session configs. Repeat to add more search paths.",
    )
    parser.add_argument(
        "--scenario-configs-dir",
        action="append",
        help="Host directory containing named scenario configs. Repeat to add more search paths.",
    )
    parser.add_argument("--session-instances-dir", help="Host directory for generated session instances and logs.")
    deployment = parser.add_argument("--deployment", help="Deployment YAML containing named hosts and values.")
    cast(Any, deployment).completer = DirectoriesCompleter()
    profiles = parser.add_argument(
        "--profiles-file",
        dest="profiles_file",
        help="Path to the project's network-profiles YAML (RFC 0004 emulated conditions).",
    )
    cast(Any, profiles).completer = DirectoriesCompleter()


def _add_profile_arg(parser: argparse.ArgumentParser) -> None:
    """Select an emulated network profile (RFC 0004); 'none' / omitted is unshaped."""
    parser.add_argument(
        "--profile",
        dest="profile",
        metavar="NAME",
        help="Emulated network profile to run under (a name from the profiles file, or 'none').",
    )


def _add_link_trace_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--link-trace",
        action="store_true",
        default=None,
        help="Record logs/<peer>/status/link_trace.jsonl for this run.",
    )
    parser.add_argument(
        "--link-trace-interval",
        dest="link_trace_interval_s",
        type=float,
        help="Link trace sample interval in seconds; enables link tracing.",
    )
    parser.add_argument(
        "--link-trace-modem-command",
        dest="link_trace_modem_command",
        help="Shell command returning a JSON object to merge under each link trace sample.",
    )


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


def _add_peer_address_arg(parser: argparse.ArgumentParser) -> None:
    action = parser.add_argument("--peer-address", action="append", default=[], metavar="PEER=ADDRESS")
    cast(Any, action).completer = _peer_address_completer


def _add_peer_arg(parser: argparse.ArgumentParser) -> None:
    action = parser.add_argument(
        "--peer",
        action="append",
        default=[],
        metavar="PEER=HOST",
        help="Map a logical session peer to a named host from deployment.yaml.",
    )
    cast(Any, action).completer = _peer_host_completer


def _add_peer_ssh_arg(parser: argparse.ArgumentParser) -> None:
    action = parser.add_argument(
        "--peer-ssh",
        action="append",
        default=[],
        metavar="PEER=SSH",
        help="Override an OTA peer's SSH target; use PEER=local for the orchestrator host.",
    )
    cast(Any, action).completer = _peer_ssh_completer


def _add_scenario_identity_args(parser: argparse.ArgumentParser) -> None:
    _add_identity_arg(parser)
    parser.add_argument("--no-auto-identity", dest="auto_identity", action="store_false")
    _add_peer_arg(parser)
    _add_peer_address_arg(parser)
    parser.set_defaults(auto_identity=True)


def _add_start_args(parser: argparse.ArgumentParser) -> None:
    _add_common_config_args(parser)
    _add_link_trace_args(parser)
    _add_session_arg(parser, "session_dir_positional", nargs="?")
    _add_session_arg(parser, "-s", "--session-dir", dest="session_dir")
    _add_identity_arg(parser)
    parser.add_argument("--mode", choices=["auto", "attach", "detached"], default="auto")
    parser.add_argument("--no-auto-identity", dest="auto_identity", action="store_false")
    parser.add_argument("--no-force", dest="force", action="store_false")
    parser.add_argument("--force", dest="force", action="store_true")
    parser.add_argument("--rewrite-formatting", action="store_true")
    _add_peer_arg(parser)
    _add_peer_address_arg(parser)
    parser.add_argument("--instance-id", help="Join or create a named runtime session instance.")
    parser.add_argument(
        "--skip-conflict-check",
        action="store_true",
        help="Start even if another session container for this identity is running.",
    )
    parser.add_argument("--scenario-managed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-managed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--network-name", help=argparse.SUPPRESS)
    parser.add_argument("--network-ip", help=argparse.SUPPRESS)
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


def anonymize_command(args: argparse.Namespace) -> int:
    runtime = _load_runtime_config(args)

    if args.scenario_dir:
        try:
            resolved_scenario = _resolve_scenario(args.scenario_dir, runtime)
            scenario_def = _load_scenario_definition(resolved_scenario)
            session_name = scenario_def.session
        except Exception as exc:
            print(f"Error: failed to resolve scenario: {exc}", file=sys.stderr)
            return 1
    elif args.session_dir:
        session_name = args.session_dir
    else:
        print("Error: either --session-dir (-s) or --scenario-dir (-c) must be specified.", file=sys.stderr)
        return 1

    try:
        resolved_session = _resolve_session(session_name, runtime)
    except Exception as exc:
        print(f"Error: failed to resolve session: {exc}", file=sys.stderr)
        return 1

    session_cfg = _effective_session_config(resolved_session.host_dir, runtime)
    original_session_name = resolved_session.host_dir.name
    output_name = args.output_name or f"anonymized_{original_session_name}"

    try:
        handoff_plan = anonymize_lib.plan_handoff_topics(session_cfg, session_gen)
    except Exception as exc:
        print(f"Error: failed to resolve processed handoff topics: {exc}", file=sys.stderr)
        return 1

    input_bag_path = Path(args.bag_path).resolve()
    try:
        meta_doc = anonymize_lib.load_bag_metadata(input_bag_path)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    bag_info = anonymize_lib.bag_topics_info(meta_doc)
    missing_topics = anonymize_lib.missing_handoff_topics(handoff_plan, bag_info)
    if missing_topics:
        print("Error: input bag is missing processed handoff topic(s):", file=sys.stderr)
        for item in missing_topics:
            print(
                "  "
                f"{item.direction} {item.source_topic} -> {item.handoff_topic} "
                f"({item.handoff_type or 'unknown type'})",
                file=sys.stderr,
            )
        print(
            "The anonymizer expects a processed trace bag recorded from the session pipeline, "
            "not the raw source bag when processing changes the OTA payload.",
            file=sys.stderr,
        )
        return 1

    storage_id = anonymize_lib.bag_storage_id(meta_doc)
    topics_map = anonymize_lib.topics_map(handoff_plan)

    output_base_path = Path(args.output_dir).resolve()
    output_session_dir = output_base_path / "sessions" / output_name
    output_scenario_dir = output_base_path / "scenarios" / output_name
    output_session_dir.mkdir(parents=True, exist_ok=True)
    output_scenario_dir.mkdir(parents=True, exist_ok=True)

    output_bag_dir = output_scenario_dir / "anonymized_bag"
    if output_bag_dir.exists():
        shutil.rmtree(output_bag_dir)

    anon_session_cfg = anonymize_lib.build_replay_session_config(session_cfg, handoff_plan)

    session_def_path = output_session_dir / "session-definition.yaml"
    with open(session_def_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(anon_session_cfg, f, default_flow_style=False, sort_keys=False)

    qos_overrides = anonymize_lib.playback_qos_overrides(handoff_plan, bag_info)
    qos_overrides_path = output_scenario_dir / "qos-overrides.yaml"
    if qos_overrides:
        with open(qos_overrides_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(qos_overrides, f, default_flow_style=False, sort_keys=False)

    manifest_path = output_scenario_dir / "anonymization-manifest.yaml"
    with open(manifest_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            anonymize_lib.anonymization_manifest(handoff_plan, bag_info),
            f,
            default_flow_style=False,
            sort_keys=False,
        )

    _require_ros2docker()
    ros2docker_cfg = load_config(runtime.ros2docker_config)
    image_name = ros2docker_cfg.get("image_name", "ros2docker")

    input_parent = input_bag_path.parent.resolve()
    input_name = input_bag_path.name

    extra_run_args = [
        "-v",
        f"{input_parent}:/input_parent_dir:ro",
        "-v",
        f"{output_scenario_dir.resolve()}:/output_scenario_dir",
        "-v",
        f"{WS_DIR.resolve()}:/ws",
    ]

    container_command = [
        "python3",
        "/ws/session/creation/anonymize_bag.py",
        "--input-bag",
        f"/input_parent_dir/{input_name}",
        "--output-bag",
        "/output_scenario_dir/anonymized_bag",
        "--topics-map",
        shlex.quote(json.dumps(topics_map)),
        "--storage-id",
        storage_id,
    ]

    container_name = "rosotacom_anonymizer"
    _stop_container_name(container_name, runtime, quiet_missing=True)

    print("Running anonymization inside ROS 2 docker container...")
    try:
        ros2docker_run(
            config_file=runtime.ros2docker_config,
            override={
                "container_name": container_name,
                "image_name": image_name,
                "run_type": "command",
                "command": " ".join(container_command),
                # Batch job: a TTY would make headless runs (CI, agents) abort
                # with "cannot attach stdin to a TTY-enabled container".
                "tty": False,
                "stdin_open": False,
            },
            extra_run_args=extra_run_args,
        )
    except Exception as exc:
        print(f"Error: ros2docker run failed: {exc}", file=sys.stderr)
        return 1

    peer_settings = session_cfg.get("peer_settings", {})
    applications_cfg = {}
    try:
        playback_topics_by_peer = anonymize_lib.playback_topics_by_peer(anon_session_cfg, handoff_plan, session_gen)
    except Exception as exc:
        print(f"Error: failed to resolve replay publish topics: {exc}", file=sys.stderr)
        return 1

    for peer, replay_topics in playback_topics_by_peer.items():
        domain_id = peer_settings.get(peer, {}).get("domain_id", 0)

        play_bag_name = f"play_bag_{peer}.ros2docker.json"
        play_bag_path = output_scenario_dir / play_bag_name
        play_command_parts = ["ros2 bag play --loop /bag/anonymized_bag"]
        if qos_overrides:
            play_command_parts.append("--qos-profile-overrides-path /scenario/qos-overrides.yaml")
        play_command_parts.append("--topics " + " ".join(shlex.quote(topic.bag_topic) for topic in replay_topics))
        remaps = [
            f"{topic.bag_topic}:={topic.publish_topic}"
            for topic in replay_topics
            if topic.bag_topic != topic.publish_topic
        ]
        if remaps:
            play_command_parts.append("--remap " + " ".join(shlex.quote(remap) for remap in remaps))
        play_run_args = [
            "--network",
            "host",
            "-v",
            "./anonymized_bag:/bag/anonymized_bag",
            "-e",
            f"ROS_DOMAIN_ID={domain_id}",
        ]
        if qos_overrides:
            play_run_args.extend(["-v", "./qos-overrides.yaml:/scenario/qos-overrides.yaml:ro"])
        play_bag_cfg = {
            "container_name": f"play_bag_{peer}",
            "image_name": image_name,
            "run_type": "command",
            "command": " ".join(play_command_parts),
            "run_args": play_run_args,
        }
        with open(play_bag_path, "w", encoding="utf-8") as f:
            json.dump(play_bag_cfg, f, indent=2)

        applications_cfg[peer] = [{"name": "play_bag", "ros2docker_config": f"./{play_bag_name}"}]

    scenario_cfg = {"schema_version": 1, "session": output_name, "applications": applications_cfg}
    scenario_def_path = output_scenario_dir / "scenario-definition.yaml"
    with open(scenario_def_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(scenario_cfg, f, default_flow_style=False, sort_keys=False)

    shutil.copy2(runtime.ros2docker_config, output_base_path / "ros2docker.json")

    rosotacom_yaml_content = (
        "ros2docker_config: ros2docker.json\n"
        "session_configs_dir:\n"
        "  - sessions\n"
        "scenario_configs_dir:\n"
        "  - scenarios\n"
        "session_instances_dir: session-instances\n"
    )
    with open(output_base_path / "rosotacom.yaml", "w", encoding="utf-8") as f:
        f.write(rosotacom_yaml_content)

    print("\nSuccessfully generated anonymized scenario:")
    print(f"  Session:  {output_session_dir}")
    print(f"  Scenario: {output_scenario_dir}")
    print(f"  Bag:      {output_bag_dir}")
    print(f"  Manifest: {manifest_path}")
    return 0


def bundle_check_command(args: argparse.Namespace) -> int:
    configs: list[BundleCheckConfig] = []
    if args.manifest:
        configs.append(load_bundle_manifest(args.manifest))

    files = tuple(ExpectedPath(path=path, required=True) for path in args.required_file or ())
    files += tuple(ExpectedPath(path=path, required=False) for path in args.optional_file or ())
    bags = tuple(ExpectedPath(path=path, required=True) for path in args.required_bag or ())
    bags += tuple(ExpectedPath(path=path, required=False) for path in args.optional_bag or ())
    configs.append(BundleCheckConfig(peers=tuple(args.peer or ()), files=files, bags=bags))

    report = check_bundle(args.directory, merge_bundle_configs(*configs))
    print(format_bundle_report(report))
    return 0 if report.complete else 1


def _videoquality_summary_line(report: dict[str, Any]) -> str:
    delivery = report.get("delivery") or {}
    summary = report.get("summary") or {}
    psnr = (summary.get("psnr_db") or {}).get("mean")
    quality = (summary.get("ssim") or {}).get("mean")
    return (
        "VIDEOQUALITY "
        f"compared={delivery.get('compared_frames', 0)}/{delivery.get('reference_frames', 0)} "
        f"lost={delivery.get('lost_frames', 0)} loss={delivery.get('loss_pct', 0)}% "
        f"mean_psnr_db={psnr} mean_ssim={quality}"
    )


def videoquality_command(args: argparse.Namespace) -> int:
    from . import video_quality

    if args.synthetic_out:
        if args.reference or args.degraded:
            raise RuntimeError("--make-synthetic cannot be combined with REF DEGRADED inputs.")
        ref_path, degraded_path = video_quality.write_synthetic_pair(
            args.synthetic_out,
            frames=args.synthetic_frames,
            width=args.synthetic_width,
            height=args.synthetic_height,
            channels=args.synthetic_channels,
            seed=args.synthetic_seed,
            quantization_step=args.synthetic_quantization_step,
            drop_every=args.synthetic_drop_every,
        )
        print(f"Synthetic reference manifest: {ref_path}")
        print(f"Synthetic degraded manifest: {degraded_path}")
        return 0

    if not args.reference or not args.degraded:
        raise RuntimeError("videoquality requires REF and DEGRADED inputs unless --make-synthetic is used.")

    report = video_quality.compare_inputs(
        args.reference,
        args.degraded,
        topic=args.topic,
        reference_topic=args.ref_topic,
        degraded_topic=args.degraded_topic,
        align=args.align,
    )
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Video quality report saved to {out_path}")
        print(_videoquality_summary_line(report))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))

    if args.plot:
        from .plots import plot_video_quality

        plot_path = plot_video_quality(report, out=args.plot)
        print(f"Video quality plot saved to {plot_path}")

    failures = video_quality.threshold_failures(
        report,
        min_mean_psnr=args.min_mean_psnr,
        min_mean_ssim=args.min_mean_ssim,
        max_loss_pct=args.max_loss_pct,
    )
    if failures:
        for failure in failures:
            print(f"VIDEOQUALITY FAIL: {failure}", file=sys.stderr)
        return 1
    if args.out or args.plot or args.min_mean_psnr is not None or args.min_mean_ssim is not None:
        print("VIDEOQUALITY OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {
        "start",
        "stop",
        "doctor",
        "ps",
        "smoke",
        "ota-smoke",
        "status",
        "metrics",
        "videoquality",
        "geomap",
        "report",
        "stream-stats",
        "test",
        "expect",
        "calibrate",
        "verify",  # retired; keep guarded so it is not rewritten as `start verify`.
        "probe-publish",
        "probe-check",
        "probe-content",
        "publish-test-topics",
        "list-sessions",
        "scenario",
        "examples",
        "config",
        "bundle",
        "profile",
        "completion",
        "benchmark",
        "ota-benchmark",
        "anonymize",
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
    _add_peer_arg(stop_parser)
    _add_peer_address_arg(stop_parser)
    stop_parser.set_defaults(func=stop_command)

    doctor_parser = subparsers.add_parser("doctor", help="Report rosotacom host readiness diagnostics.")
    _add_common_config_args(doctor_parser)
    doctor_parser.set_defaults(func=doctor)

    ps_parser = subparsers.add_parser(
        "ps",
        help="List this workspace's active rosotacom containers and whether they block new runs.",
    )
    _add_common_config_args(ps_parser)
    ps_parser.set_defaults(func=ps_command)

    smoke_parser = subparsers.add_parser("smoke", help="Run a local smoke test.")
    _add_common_config_args(smoke_parser)
    _add_profile_arg(smoke_parser)
    _add_link_trace_args(smoke_parser)
    smoke_target = smoke_parser.add_argument("session_dir", nargs="?")
    cast(Any, smoke_target).completer = _smoke_target_completer
    smoke_parser.add_argument("--local", action="store_true", default=True)
    smoke_parser.add_argument("--local-ip")
    smoke_parser.add_argument(
        "--interactive",
        action="store_true",
        help="Open an interactive local end-to-end tmux rig.",
    )
    smoke_parser.add_argument(
        "--stop",
        dest="interactive_stop",
        action="store_true",
        help="Stop an interactive smoke run.",
    )
    smoke_parser.add_argument(
        "--list",
        dest="interactive_list",
        action="store_true",
        help="List active interactive smoke runs.",
    )
    smoke_parser.add_argument("--target-type", choices=["auto", "session", "scenario"], default="auto")
    smoke_parser.add_argument("--mode", choices=["auto", "attach", "detached"], default="auto")
    smoke_parser.add_argument("--keep-running", action="store_true", help="Leave smoke-test containers running.")
    smoke_parser.add_argument("--instance-id", help="Join or create a named runtime session instance.")
    smoke_parser.add_argument(
        "--skip-conflict-check",
        action="store_true",
        help="Start even if a smoke run for the same target is already active in this workspace.",
    )
    _add_peer_address_arg(smoke_parser)
    smoke_parser.add_argument("--verify-only", action="store_true", help=argparse.SUPPRESS)
    smoke_parser.set_defaults(func=smoke)

    ota_smoke_parser = subparsers.add_parser("ota-smoke", help="Run a generic multi-machine OTA smoke test.")
    _add_common_config_args(ota_smoke_parser)
    _add_profile_arg(ota_smoke_parser)
    _add_link_trace_args(ota_smoke_parser)
    ota_target = ota_smoke_parser.add_argument("target", nargs="?")
    cast(Any, ota_target).completer = _smoke_target_completer
    _add_peer_arg(ota_smoke_parser)
    _add_peer_address_arg(ota_smoke_parser)
    _add_peer_ssh_arg(ota_smoke_parser)
    ota_smoke_parser.add_argument("--target-type", choices=["auto", "session", "scenario"], default="auto")
    ota_smoke_parser.add_argument("--interactive", action="store_true", help="Open a local control tmux UI.")
    ota_smoke_parser.add_argument("--stop", action="store_true", help="Stop an OTA smoke run.")
    ota_smoke_parser.add_argument("--list", action="store_true", help="List active interactive OTA smoke runs.")
    ota_smoke_parser.add_argument("--mode", choices=["auto", "attach", "detached"], default="auto")
    ota_smoke_parser.add_argument("--instance-id", help="Join or create a named runtime session instance.")
    ota_smoke_parser.add_argument(
        "--workdir",
        default="/tmp/rosotacom_ota",
        help="Remote staging directory (default: /tmp/rosotacom_ota).",
    )
    ota_smoke_parser.add_argument(
        "--reuse",
        action="store_true",
        help="Reuse an already installed rosotacom source tree in the workdir.",
    )
    ota_smoke_parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Keep staged source and project files after cleanup.",
    )
    ota_smoke_parser.add_argument("--skip-preflight", action="store_true", help="Skip SSH/Docker readiness checks.")
    ota_smoke_parser.add_argument(
        "--skip-conflict-check",
        action="store_true",
        help="Proceed even if the peers already run rosotacom containers or active network shaping.",
    )
    ota_smoke_parser.add_argument(
        "--check-peer-reachability",
        action="store_true",
        help="Also ping every peer address from every other peer during preflight.",
    )
    ota_smoke_parser.add_argument("--keep-running", action="store_true", help="Leave remote components running.")
    ota_smoke_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print remote commands without executing them.",
    )
    ota_smoke_parser.add_argument("--state-file", help=argparse.SUPPRESS)
    ota_smoke_parser.add_argument("--verify-only", action="store_true", help=argparse.SUPPRESS)
    ota_smoke_parser.set_defaults(func=ota_smoke)

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

    metrics_parser = subparsers.add_parser(
        "metrics", help="Join and summarize RFC 0003 transit records from events.jsonl files."
    )
    metrics_parser.add_argument("events", nargs="+", help="One or more status/events.jsonl files.")
    metrics_parser.add_argument(
        "--records", action="store_true", help="Emit joined per-(topic, seq) records instead of a summary."
    )
    metrics_parser.set_defaults(func=metrics_command)

    geomap_parser = subparsers.add_parser(
        "geomap",
        help="Join GPS/pose samples with link trace or transit metrics and render a geo-referenced HTML map.",
    )
    gps_group = geomap_parser.add_mutually_exclusive_group(required=True)
    gps_group.add_argument(
        "--gps-csv",
        help=(
            "Host-only GPS CSV fixture with time_s/latitude/longitude columns "
            "(aliases: stamp_s, timestamp_s, bag_time_s, lat, lon, lng)."
        ),
    )
    gps_group.add_argument("--bag", help="rosbag2 directory, metadata.yaml, or bag file containing the GPS topic.")
    geomap_parser.add_argument("--gps-topic", help="GPS, NavSatFix, PoseStamped, or Odometry topic to read from --bag.")
    geomap_parser.add_argument(
        "--gps-time-source",
        choices=["bag", "header"],
        default="bag",
        help="Timestamp source for --bag GPS samples: rosbag record time or message header.stamp (default: bag).",
    )
    geomap_parser.add_argument("--storage-id", help="rosbag2 storage id override (default: metadata storage id).")
    geomap_parser.add_argument(
        "--origin-lat",
        type=float,
        help="WGS84 latitude used to project local pose/odometry x/y meters; required with --origin-lon.",
    )
    geomap_parser.add_argument(
        "--origin-lon",
        type=float,
        help="WGS84 longitude used to project local pose/odometry x/y meters; required with --origin-lat.",
    )
    geomap_parser.add_argument("--trace", action="append", default=[], help="link_trace.jsonl input. Repeatable.")
    geomap_parser.add_argument("--events", action="append", default=[], help="events.jsonl input. Repeatable.")
    geomap_parser.add_argument(
        "--event-bin-s",
        type=float,
        default=1.0,
        help="Seconds per transit-metric bin when --events is used (default: 1).",
    )
    geomap_parser.add_argument(
        "--event-time-field",
        choices=["t_wrap", "t_com_in"],
        default="t_wrap",
        help="Transit timestamp field to join when --events is used (default: t_wrap).",
    )
    geomap_parser.add_argument(
        "--event-topic",
        help="Only use this transit topic or source->target:/topic label from --events.",
    )
    geomap_parser.add_argument(
        "--metric",
        choices=[
            "observed_tx_kbps",
            "observed_rx_kbps",
            "rtt_ms",
            "loss_pct",
            "delivery_pct",
            "event_loss_pct",
            "ota_hop_ms",
        ],
        default="observed_tx_kbps",
        help="Metric used to color the route.",
    )
    geomap_parser.add_argument(
        "--trace-to-gps-offset-s",
        type=float,
        default=0.0,
        help=(
            "Seconds added to each trace/events timestamp before joining to GPS time. "
            "Use 0 only when both sources already share the same epoch."
        ),
    )
    geomap_parser.add_argument(
        "--max-gap-s",
        type=float,
        default=1.0,
        help="Maximum absolute nearest GPS/metric timestamp gap to keep (default: 1).",
    )
    geomap_parser.add_argument("--out-csv", required=True, help="Output CSV path for georeferenced samples.")
    geomap_parser.add_argument(
        "--out-html",
        required=True,
        help="Output HTML report; also writes a sibling .route.png.",
    )
    geomap_parser.add_argument("--manifest", help="Optional YAML sidecar with output paths and sample count.")
    geomap_parser.add_argument("--title", help="Optional HTML report title.")
    geomap_parser.set_defaults(func=geomap_command)

    profile_parser = subparsers.add_parser("profile", help="Generate and inspect network profiles.")
    profile_subparsers = profile_parser.add_subparsers(dest="profile_command", required=True)
    profile_from_trace_parser = profile_subparsers.add_parser(
        "from-trace",
        help="Convert link_trace.jsonl into an RFC 0004 profiles YAML entry.",
    )
    profile_from_trace_parser.add_argument("trace", help="Path to link_trace.jsonl.")
    profile_from_trace_parser.add_argument("--out", help="Write generated YAML to this path instead of stdout.")
    profile_from_trace_parser.add_argument("--name", help="Profile name to emit.")
    profile_from_trace_parser.add_argument(
        "--mode",
        choices=["timeline", "static"],
        default="timeline",
        help="Emit a piecewise timeline profile or one distilled static profile.",
    )
    profile_from_trace_parser.add_argument(
        "--directions",
        choices=["both", "uplink", "downlink"],
        default="both",
        help="Profile directions to populate from the trace peer's tx/rx counters.",
    )
    profile_from_trace_parser.add_argument(
        "--window",
        metavar="START:END",
        help="Only use samples in this relative seconds window; either side may be omitted.",
    )
    profile_from_trace_parser.add_argument(
        "--min-segment-s",
        type=float,
        default=5.0,
        help="Minimum normal timeline segment duration before a change point may split it.",
    )
    profile_from_trace_parser.add_argument(
        "--change-sensitivity",
        type=float,
        default=0.25,
        help="Relative sensitivity for timeline change points; lower values split more readily.",
    )
    profile_from_trace_parser.add_argument(
        "--gap-outage-after",
        dest="gap_outage_after_s",
        type=float,
        help="Sample gap duration that becomes a reconnect outage; default is 3x the trace interval.",
    )
    profile_from_trace_parser.add_argument(
        "--loss-outage-min",
        dest="loss_outage_min_s",
        type=float,
        help="Minimum sustained 100%% probe-loss duration emitted as a catchup outage.",
    )
    profile_from_trace_parser.add_argument(
        "--rate-percentile",
        type=float,
        default=50.0,
        help="Percentile for valid saturated/probed rate samples (default: median).",
    )
    profile_from_trace_parser.add_argument(
        "--delay-percentile",
        type=float,
        default=90.0,
        help="Static-profile delay percentile; timeline segments use median delay.",
    )
    profile_from_trace_parser.add_argument(
        "--jitter-spread-percentile",
        type=float,
        default=90.0,
        help="Jitter is this delay percentile minus median delay.",
    )
    profile_from_trace_parser.set_defaults(func=profile_from_trace_command)

    videoquality_parser = subparsers.add_parser(
        "videoquality",
        help="Compute offline PSNR/SSIM for decoded camera frames.",
    )
    videoquality_parser.add_argument(
        "reference",
        nargs="?",
        help="Reference rosbag2 bag/metadata.yaml or frame manifest.",
    )
    videoquality_parser.add_argument(
        "degraded",
        nargs="?",
        help="Degraded rosbag2 bag/metadata.yaml or frame manifest.",
    )
    videoquality_parser.add_argument(
        "--topic",
        help="sensor_msgs/msg/Image topic to read from rosbag2 inputs; manifests carry their own frames.",
    )
    videoquality_parser.add_argument("--ref-topic", help="Reference bag topic; overrides --topic for REF.")
    videoquality_parser.add_argument("--degraded-topic", help="Degraded bag topic; overrides --topic for DEGRADED.")
    videoquality_parser.add_argument(
        "--align",
        choices=["auto", "pts", "index"],
        default="auto",
        help="Frame alignment mode: pts when available, otherwise index (default: auto).",
    )
    videoquality_parser.add_argument("--out", help="Write the JSON report to this path instead of stdout-only output.")
    videoquality_parser.add_argument("--plot", help="Write a PNG plot using the optional rosotacom[plots] extra.")
    videoquality_parser.add_argument("--min-mean-psnr", type=float, help="Fail if mean PSNR is below this dB floor.")
    videoquality_parser.add_argument("--min-mean-ssim", type=float, help="Fail if mean SSIM is below this floor.")
    videoquality_parser.add_argument(
        "--max-loss-pct",
        type=float,
        help="Fail if reference-frame loss exceeds this percent.",
    )
    videoquality_parser.add_argument(
        "--make-synthetic",
        dest="synthetic_out",
        help="Write deterministic reference/degraded frame manifests to this directory.",
    )
    videoquality_parser.add_argument("--synthetic-frames", type=int, default=12)
    videoquality_parser.add_argument("--synthetic-width", type=int, default=32)
    videoquality_parser.add_argument("--synthetic-height", type=int, default=24)
    videoquality_parser.add_argument("--synthetic-channels", type=int, choices=[1, 3], default=1)
    videoquality_parser.add_argument("--synthetic-seed", type=int, default=0)
    videoquality_parser.add_argument("--synthetic-quantization-step", type=int, default=8)
    videoquality_parser.add_argument("--synthetic-drop-every", type=int)
    videoquality_parser.set_defaults(func=videoquality_command)

    report_parser = subparsers.add_parser(
        "report",
        help="Degradation forensics for a recorded session instance: localize and explain loss/latency events.",
    )
    report_parser.add_argument("instance_dir", help="Session-instance directory (contains logs/<peer>/status/).")
    report_parser.add_argument(
        "--peer", action="append", help="Analyze only this peer's artifacts. Repeat to add more (default: all found)."
    )
    report_parser.add_argument("--bin-s", type=float, default=1.0, help="Timeline bin width in seconds (default: 1).")
    report_parser.add_argument(
        "--loss-burst-min",
        type=int,
        default=3,
        help="Minimum consecutive lost sequence numbers to flag a loss burst (default: 3).",
    )
    report_parser.add_argument(
        "--latency-baseline-window",
        type=int,
        default=30,
        help="Rolling baseline size in delivered samples (default: 30).",
    )
    report_parser.add_argument(
        "--latency-baseline-min",
        type=int,
        default=10,
        help="Samples required before excursion detection starts (default: 10).",
    )
    report_parser.add_argument(
        "--latency-ratio",
        type=float,
        default=2.0,
        help="Excursion threshold as a multiple of the rolling baseline median (default: 2.0).",
    )
    report_parser.add_argument(
        "--latency-min-delta-ms",
        type=float,
        default=50.0,
        help="Excursion must also exceed baseline by this many ms (default: 50).",
    )
    report_parser.add_argument(
        "--latency-min-run",
        type=int,
        default=3,
        help="Consecutive excursive samples required to flag an event (default: 3).",
    )
    report_parser.add_argument(
        "--rate-collapse-fraction",
        type=float,
        default=0.5,
        help="Delivered rate below this fraction of nominal counts as collapsed (default: 0.5).",
    )
    report_parser.add_argument(
        "--rate-collapse-min-bins",
        type=int,
        default=2,
        help="Consecutive collapsed bins required to flag an event (default: 2).",
    )
    report_parser.add_argument(
        "--profile",
        metavar="NAME",
        help="Profile the run was recorded under (environment context only; RFC 0004 name).",
    )
    report_parser.add_argument("--profiles-file", help="profiles.yaml that defines --profile.")
    report_parser.add_argument(
        "--timeline-anchor",
        metavar="EPOCH_OR_ISO",
        help="Wall-clock start of a timeline profile (default: first observed publish, approximate).",
    )
    report_parser.add_argument("--out", help="Output directory (default: <instance-dir>/report).")
    report_parser.add_argument("--no-figures", action="store_true", help="Skip figure rendering.")
    report_parser.add_argument(
        "--json", action="store_true", help="Print report.json to stdout instead of the markdown summary."
    )
    report_parser.set_defaults(func=report_command)

    stream_stats_parser = subparsers.add_parser(
        "stream-stats",
        help="Compare stream sizes, rate, interval regularity, and FFMPEG GOP shape across recorded stages.",
    )
    stream_stats_parser.add_argument(
        "--bag",
        action="append",
        metavar="LABEL=PATH:/topic",
        help=(
            "Analyze one rosbag2/stage-bag topic. MCAP uses the Python mcap reader; other storage "
            "backends require rosbag2_py. Repeat to compare stages."
        ),
    )
    stream_stats_parser.add_argument(
        "--events",
        action="append",
        metavar="LABEL=PATH:/topic",
        help="Analyze one RFC 0003 events.jsonl transit topic. Repeat to compare stages.",
    )
    stream_stats_parser.add_argument(
        "--storage-id",
        default="mcap",
        help="rosbag2 storage id for --bag sources (default: mcap).",
    )
    stream_stats_parser.add_argument("--out", help="Output directory for stream-stats.json and stream-stats.md.")
    stream_stats_parser.add_argument(
        "--json", action="store_true", help="Print stream-stats.json to stdout instead of the markdown summary."
    )
    stream_stats_parser.set_defaults(func=stream_stats_command)

    test_parser = subparsers.add_parser(
        "test", help="Assert a running/recent session meets its status + per-topic expect contract."
    )
    _add_common_config_args(test_parser)
    _add_profile_arg(test_parser)
    _add_session_arg(test_parser, "session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    test_parser.add_argument("--instance-id", help="Evaluate a specific instance id (default: most recent).")
    test_parser.add_argument("--timeout", type=float, default=30.0, help="Seconds to wait for status to settle.")
    test_parser.add_argument("--interval", type=float, default=2.0, help="Polling interval while waiting (s).")
    test_parser.add_argument(
        "--bag",
        help="Replay bag dir/metadata.yaml: enables completeness.vs_bag_ratio assertions against the native rate.",
    )
    test_parser.add_argument(
        "--suggest",
        action="store_true",
        help="Print a suggested `expect` block per topic from the current run instead of asserting.",
    )
    test_parser.set_defaults(func=test_command)

    expect_parser = subparsers.add_parser("expect", help="Generate or inspect per-topic expect contracts.")
    expect_subparsers = expect_parser.add_subparsers(dest="expect_command", required=True)
    expect_from_bag_parser = expect_subparsers.add_parser(
        "from-bag",
        help="Generate a whole-bag expect YAML fragment from rosbag2 metadata.",
    )
    _add_common_config_args(expect_from_bag_parser)
    expect_from_bag_parser.add_argument("bag", help="Path to a rosbag2 bag directory or its metadata.yaml.")
    _add_session_arg(
        expect_from_bag_parser,
        "--session",
        dest="session_dir",
        required=True,
        help="Session name or directory whose carried topics should receive generated expect blocks.",
    )
    expect_from_bag_parser.add_argument("--out", help="Write the YAML fragment to this path instead of stdout.")
    expect_from_bag_parser.add_argument(
        "--min-ratio",
        type=float,
        default=0.9,
        help="Safety margin for generated min_count/completeness thresholds (default: 0.9).",
    )
    expect_from_bag_parser.add_argument(
        "--stream-min-hz",
        type=float,
        default=0.1,
        help="Minimum native rate classified as stream instead of existence (default: 0.1).",
    )
    expect_from_bag_parser.set_defaults(func=expect_from_bag_command)

    calibrate_parser = subparsers.add_parser(
        "calibrate",
        help="Report a replay bag's per-topic ground truth and validate a session's expect against it.",
    )
    _add_common_config_args(calibrate_parser)
    calibrate_parser.add_argument("--bag", required=True, help="Path to a rosbag2 bag directory or its metadata.yaml.")
    _add_session_arg(calibrate_parser, "session_dir", nargs="?", default=None)
    calibrate_parser.set_defaults(func=calibrate_command)

    probe_publish_parser = subparsers.add_parser(
        "probe-publish", help="Publish a local-only probe topic in a running peer's local domain (isolation)."
    )
    _add_common_config_args(probe_publish_parser)
    _add_session_arg(probe_publish_parser, "session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    _add_identity_arg(probe_publish_parser, required=True)
    _add_peer_address_arg(probe_publish_parser)
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
    _add_peer_address_arg(probe_check_parser)
    probe_check_parser.add_argument("--instance-id")
    probe_check_parser.add_argument("--topic", default=ISOLATION_PROBE_TOPIC)
    probe_check_parser.add_argument("--expect", choices=["present", "absent"], default="absent")
    probe_check_parser.set_defaults(func=probe_check_command)

    probe_content_parser = subparsers.add_parser(
        "probe-content",
        help="Content integrity: assert a delivered topic's field byte-equals an expected value.",
    )
    _add_common_config_args(probe_content_parser)
    _add_session_arg(probe_content_parser, "session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    _add_identity_arg(probe_content_parser, required=True)
    _add_peer_address_arg(probe_content_parser)
    probe_content_parser.add_argument("--instance-id")
    probe_content_parser.add_argument("--topic", required=True, help="Delivered topic to inspect on this peer.")
    probe_content_parser.add_argument("--type", required=True, help="Message type (e.g. std_msgs/msg/String).")
    probe_content_parser.add_argument("--field", default="data", help="Message field to compare (default: data).")
    probe_content_parser.add_argument("--expect", required=True, help="Expected byte value of the field.")
    probe_content_parser.set_defaults(func=probe_content_command)

    publish_test_topics_parser = subparsers.add_parser(
        "publish-test-topics",
        help="Start/stop synthetic local app publishers for OTA example verification.",
    )
    _add_common_config_args(publish_test_topics_parser)
    _add_session_arg(publish_test_topics_parser, "session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    _add_identity_arg(publish_test_topics_parser, required=True)
    _add_peer_address_arg(publish_test_topics_parser)
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
    _add_link_trace_args(scenario_start_parser)
    _add_scenario_arg(scenario_start_parser, "scenario")
    _add_scenario_identity_args(scenario_start_parser)
    scenario_start_parser.add_argument("--mode", choices=["auto", "attach", "detached"], default="auto")
    scenario_start_parser.add_argument("--no-force", dest="force", action="store_false")
    scenario_start_parser.add_argument("--force", dest="force", action="store_true")
    scenario_start_parser.add_argument("--rewrite-formatting", action="store_true")
    scenario_start_parser.add_argument("--instance-id", help="Join or create a named runtime session instance.")
    scenario_start_parser.add_argument("--network-name", help=argparse.SUPPRESS)
    scenario_start_parser.add_argument("--network-ip", help=argparse.SUPPRESS)
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
    scenario_run_application_parser.add_argument("--instance-id", help=argparse.SUPPRESS)
    scenario_run_application_parser.add_argument("--network-name", help=argparse.SUPPRESS)
    scenario_run_application_parser.add_argument("--network-ip", help=argparse.SUPPRESS)
    scenario_run_application_parser.set_defaults(func=run_scenario_application)

    examples_parser = subparsers.add_parser("examples", help="Manage packaged rosotacom examples.")
    examples_subparsers = examples_parser.add_subparsers(dest="examples_command", required=True)
    examples_create_parser = examples_subparsers.add_parser("create", help="Copy the packaged example project.")
    examples_create_parser.add_argument("target", help="Directory to create.")
    examples_create_parser.add_argument("--force", action="store_true", help="Replace the target if it exists.")
    examples_create_parser.set_defaults(func=examples_create_command)

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

    bundle_parser = subparsers.add_parser("bundle", help="Inspect session-instance artifact bundles.")
    bundle_subparsers = bundle_parser.add_subparsers(dest="bundle_command", required=True)
    bundle_check_parser = bundle_subparsers.add_parser(
        "check",
        help="Validate a session-instance artifact bundle.",
    )
    bundle_check_parser.add_argument("directory", help="Session-instance directory to validate.")
    bundle_check_parser.add_argument(
        "--manifest",
        help="YAML manifest listing expected peers, files, and bags relative to the session instance.",
    )
    bundle_check_parser.add_argument(
        "--peer",
        action="append",
        default=[],
        help="Expected peer key with logs/<peer>/status/status.json and events.jsonl. Repeat as needed.",
    )
    bundle_check_parser.add_argument(
        "--file",
        dest="required_file",
        action="append",
        default=[],
        help="Required non-empty file relative to the session instance. Repeat as needed.",
    )
    bundle_check_parser.add_argument(
        "--optional-file",
        action="append",
        default=[],
        help="Optional file to validate when present. Repeat as needed.",
    )
    bundle_check_parser.add_argument(
        "--bag",
        dest="required_bag",
        action="append",
        default=[],
        help="Required rosbag2 bag directory or metadata.yaml path. Repeat as needed.",
    )
    bundle_check_parser.add_argument(
        "--optional-bag",
        action="append",
        default=[],
        help="Optional rosbag2 bag directory or metadata.yaml path to validate when present. Repeat as needed.",
    )
    bundle_check_parser.set_defaults(func=bundle_check_command)

    from .cli_benchmark import register_benchmark_parser

    register_benchmark_parser(subparsers)

    anonymize_parser = subparsers.add_parser(
        "anonymize",
        help="Anonymize a rosbag and create a scenario out of it.",
    )
    _add_common_config_args(anonymize_parser)
    anonymize_parser.add_argument("bag_path", help="Path to processed handoff trace rosbag.")
    anonymize_parser.add_argument(
        "-s",
        "--session-dir",
        dest="session_dir",
        help="Path or name of the base session.",
    )
    anonymize_parser.add_argument(
        "-c",
        "--scenario-dir",
        dest="scenario_dir",
        help="Path or name of the base scenario (resolves session).",
    )
    anonymize_parser.add_argument(
        "-o",
        "--output-dir",
        required=True,
        help="Destination directory for anonymized files.",
    )
    anonymize_parser.add_argument(
        "--output-name",
        help="Name of the generated session/scenario (default: anonymized_<original_name>).",
    )
    anonymize_parser.set_defaults(func=anonymize_command)

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
