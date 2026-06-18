#!/usr/bin/env python3
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
from typing import Any

import yaml

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
DEFAULT_SMOKE_SESSION = "1_heartbeat_cyclone-ota"

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


@dataclass(frozen=True)
class ResolvedSession:
    host_dir: Path
    container_dir: str
    source: str


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
    base = str(config.get("image_name") or "ros-communication")
    suffix = runtime.install_id
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


# --- Shared verification primitives (used by `smoke` and the `verify`/`probe-*`
# CLI verbs that the external multi-machine runner calls over SSH) --------------
# Single source of truth for the heartbeat delivery bounds and the isolation
# probe topic, so both test tiers assert the same thing. See docs/testing.md.
VERIFY_HZ_MIN = 5.0
VERIFY_HZ_MAX = 20.0
VERIFY_MAX_DELAY_S = 1.0
ISOLATION_PROBE_TOPIC = "/local_only"


def _other_peer_key(cfg: dict[str, Any], identity: str) -> str:
    peers = cfg.get("peers") or {}
    if identity not in peers:
        raise RuntimeError(f"--identity must be one of peers={sorted(peers)}")
    return str(next(k for k in peers if k != identity))


def _received_crossed_topics(cfg: dict[str, Any], receiver_peer_key: str) -> list[tuple[str, str, bool]]:
    """(topic, label, enforce_bounds) the receiver must get from the other peer:
    the inbound-bridge topic (presence only) and the final remapped heartbeat
    (presence + rate/latency bounds)."""
    source = _other_peer_key(cfg, receiver_peer_key)
    return [
        (_smoke_inbound_bridge_topic(cfg, source), f"{source}->{receiver_peer_key} inbound bridge heartbeat", False),
        (_smoke_heartbeat_topic(cfg, source), f"{source}->{receiver_peer_key} final heartbeat", True),
    ]


def _verify_received_topics(
    container_name: str,
    ros_setup: str,
    cfg: dict[str, Any],
    receiver_peer_key: str,
    *,
    hz_min: float = VERIFY_HZ_MIN,
    hz_max: float = VERIFY_HZ_MAX,
    max_delay_s: float = VERIFY_MAX_DELAY_S,
    log_line: Callable[[str], None] = print,
    detail_log: Callable[[str], None] | None = None,
) -> list[str]:
    """Assert the receiver's crossed topics publish (and the final heartbeat does
    so within rate/latency bounds). Emits the OK/SMOKE_METRIC lines smoke has
    always produced; returns a list of failures (empty == all good)."""
    errors: list[str] = []
    for topic, label, enforce_bounds in _received_crossed_topics(cfg, receiver_peer_key):
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
        if enforce_bounds:
            if hz is None or not (hz_min <= hz <= hz_max):
                errors.append(f"{label} ({topic}) rate {hz} Hz outside [{hz_min}, {hz_max}] in {container_name}")
            if delay_s is None or delay_s >= max_delay_s:
                errors.append(f"{label} ({topic}) latency {delay_s}s >= {max_delay_s}s in {container_name}")
    return errors


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


# --- Test-tier capability markers (single source of truth for both tiers) -----
_VALID_SINGLE_MACHINE = {"ok", "na"}
_VALID_MULTI_MACHINE = {"ok", "required", "na"}


def _session_markers(session_host_dir: Path) -> dict[str, str]:
    name = session_host_dir.name
    cfg = _load_session_config_from_host(session_host_dir)
    tiers = cfg.get("test_tiers")
    if not isinstance(tiers, dict):
        raise RuntimeError(f"{name}: missing 'test_tiers' mapping (see docs/testing.md)")
    single, multi = tiers.get("single_machine"), tiers.get("multi_machine")
    if single not in _VALID_SINGLE_MACHINE:
        raise RuntimeError(f"{name}: test_tiers.single_machine={single!r} not in {sorted(_VALID_SINGLE_MACHINE)}")
    if multi not in _VALID_MULTI_MACHINE:
        raise RuntimeError(f"{name}: test_tiers.multi_machine={multi!r} not in {sorted(_VALID_MULTI_MACHINE)}")
    return {"single_machine": single, "multi_machine": multi}


def session_test_markers(sessions_dir: Path | None = None) -> dict[str, dict[str, str]]:
    """Map session name -> {single_machine, multi_machine} for every session.
    Defaults to the packaged example sessions (the repo's source of truth)."""
    base = sessions_dir or (EXAMPLE_PROJECT_DIR / "sessions")
    out: dict[str, dict[str, str]] = {}
    for child in sorted(base.iterdir()):
        if child.is_dir() and _is_session_dir(child):
            out[child.name] = _session_markers(child)
    return out


def sessions_in_tier(tier: str, values: set[str], sessions_dir: Path | None = None) -> list[str]:
    return [name for name, m in session_test_markers(sessions_dir).items() if m.get(tier) in values]


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


def verify_command(args: argparse.Namespace) -> int:
    """Assert an already-running peer receives its crossed topics within bounds."""
    container, ros_setup, cfg = _resolve_running_peer(args, args.identity)
    errors = _verify_received_topics(
        container, ros_setup, cfg, args.identity, hz_min=args.hz_min, hz_max=args.hz_max, max_delay_s=args.max_delay
    )
    for err in errors:
        print(f"VERIFY FAIL: {err}", file=sys.stderr)
    if errors:
        return 1
    print(f"VERIFY OK: identity {args.identity} receives its crossed topics within bounds")
    return 0


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

    reports: dict[str, Any] = {}
    for peer_dir in sorted(p for p in logs_dir.iterdir() if p.is_dir()):
        status_json = peer_dir / "status" / "status.json"
        if status_json.exists():
            reports[peer_dir.name] = json.loads(status_json.read_text(encoding="utf-8"))
    if not reports:
        print(
            f"rosotacom test: no status.json under {logs_dir}. Start the session with "
            "shared.use_status_overview=true first.",
            file=sys.stderr,
        )
        return 1

    failures = status_eval.evaluate_reports(reports, status_eval.expectations_from_cfg(cfg))
    for failure in failures:
        print(f"TEST FAIL: {failure}", file=sys.stderr)
    if failures:
        return 1
    topic_count = sum(len(r.get("topics", [])) for r in reports.values())
    print(f"TEST OK: {len(reports)} peer(s), {topic_count} topic(s) meet status + expectations")
    return 0


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
        if errors:
            raise RuntimeError("Smoke verification failed:\n  - " + "\n  - ".join(errors))
    except Exception as exc:
        _append_log(smoke_log, f"ERROR: {exc}")
        raise
    finally:
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
    parser.add_argument("--session-instances-dir", help="Host directory for generated session instances and logs.")
    parser.add_argument("--data-dict", help="Host data_dict.json path for data:<key> address expressions.")


def _add_start_args(parser: argparse.ArgumentParser) -> None:
    _add_common_config_args(parser)
    parser.add_argument("session_dir_positional", nargs="?")
    parser.add_argument("-s", "--session-dir", dest="session_dir")
    parser.add_argument("--identity")
    parser.add_argument("--mode", choices=["auto", "attach", "detached"], default="auto")
    parser.add_argument("--no-auto-identity", dest="auto_identity", action="store_false")
    parser.add_argument("--no-force", dest="force", action="store_false")
    parser.add_argument("--force", dest="force", action="store_true")
    parser.add_argument("--rewrite-formatting", action="store_true")
    parser.add_argument("--overwrite-peers-via-remote-peer")
    parser.add_argument("--peer-address", action="append", default=[], metavar="PEER=ADDRESS_EXPR")
    parser.add_argument("--instance-id", help="Join or create a named runtime session instance.")
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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {
        "start",
        "stop",
        "doctor",
        "smoke",
        "status",
        "test",
        "verify",
        "probe-publish",
        "probe-check",
        "list-sessions",
        "examples",
        "setup-env",
        "config",
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
    stop_parser.add_argument("session_dir_positional", nargs="?")
    stop_parser.add_argument("-s", "--session-dir", dest="session_dir")
    stop_parser.add_argument("--identity")
    stop_parser.add_argument("--auto-identity", action="store_true")
    stop_parser.add_argument("--peer-address", action="append", default=[], metavar="PEER=ADDRESS_EXPR")
    stop_parser.add_argument("--overwrite-peers-via-remote-peer")
    stop_parser.set_defaults(func=stop_command)

    doctor_parser = subparsers.add_parser("doctor", help="Report rosotacom host readiness diagnostics.")
    _add_common_config_args(doctor_parser)
    doctor_parser.set_defaults(func=doctor)

    smoke_parser = subparsers.add_parser("smoke", help="Run a local smoke test.")
    _add_common_config_args(smoke_parser)
    smoke_parser.add_argument("session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    smoke_parser.add_argument("--local", action="store_true", default=True)
    smoke_parser.add_argument("--local-ip")
    smoke_parser.add_argument("--keep-running", action="store_true", help="Leave smoke-test containers running.")
    smoke_parser.add_argument("--instance-id", help="Join or create a named runtime session instance.")
    smoke_parser.set_defaults(func=smoke)

    status_parser = subparsers.add_parser(
        "status", help="Show the live per-topic pipeline status for a session instance."
    )
    _add_common_config_args(status_parser)
    status_parser.add_argument("session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    status_parser.add_argument("--identity", help="Show only this peer identity (default: all available).")
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
    test_parser.add_argument("session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    test_parser.add_argument("--instance-id", help="Evaluate a specific instance id (default: most recent).")
    test_parser.set_defaults(func=test_command)

    # Verification verbs operate on an already-running session per identity, so the
    # external multi-machine runner can call them over SSH (one peer per host) with
    # the exact same logic the local smoke test uses.
    verify_parser = subparsers.add_parser(
        "verify", help="Assert a running peer receives its crossed topics within rate/latency bounds."
    )
    _add_common_config_args(verify_parser)
    verify_parser.add_argument("session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    verify_parser.add_argument("--identity", required=True)
    verify_parser.add_argument("--peer-address", action="append", default=[], metavar="PEER=ADDRESS_EXPR")
    verify_parser.add_argument("--instance-id", help="Inspect a specific instance id (default: most recent).")
    verify_parser.add_argument("--hz-min", type=float, default=VERIFY_HZ_MIN)
    verify_parser.add_argument("--hz-max", type=float, default=VERIFY_HZ_MAX)
    verify_parser.add_argument("--max-delay", type=float, default=VERIFY_MAX_DELAY_S)
    verify_parser.set_defaults(func=verify_command)

    probe_publish_parser = subparsers.add_parser(
        "probe-publish", help="Publish a local-only probe topic in a running peer's local domain (isolation)."
    )
    _add_common_config_args(probe_publish_parser)
    probe_publish_parser.add_argument("session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    probe_publish_parser.add_argument("--identity", required=True)
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
    probe_check_parser.add_argument("session_dir", nargs="?", default=DEFAULT_SMOKE_SESSION)
    probe_check_parser.add_argument("--identity", required=True)
    probe_check_parser.add_argument("--peer-address", action="append", default=[], metavar="PEER=ADDRESS_EXPR")
    probe_check_parser.add_argument("--instance-id")
    probe_check_parser.add_argument("--topic", default=ISOLATION_PROBE_TOPIC)
    probe_check_parser.add_argument("--expect", choices=["present", "absent"], default="absent")
    probe_check_parser.set_defaults(func=probe_check_command)

    list_parser = subparsers.add_parser("list-sessions", help="List configured sessions.")
    _add_common_config_args(list_parser)
    list_parser.set_defaults(func=list_sessions_command)

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
