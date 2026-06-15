#!/usr/bin/env python3
"""rosotacom host CLI.

This module owns rosotacom-specific concepts such as session config
directories, data_dict wiring, multi-checkout names, and local smoke tests.
ros2docker remains the generic Docker runner underneath.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.resources as importlib_resources
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ResolvedSession:
    host_dir: Path
    container_dir: str
    source: str


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


def _load_runtime_config(args: argparse.Namespace | None = None) -> RuntimeConfig:
    args = args or argparse.Namespace()
    rosotacom_config_raw = _first_value(
        getattr(args, "rosotacom_config", None),
        os.environ.get("ROSOTACOM_CONFIG"),
    )
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
    data_dict_raw = _first_value(
        getattr(args, "data_dict", None),
        os.environ.get("ROSOTACOM_DATA_DICT"),
        cfg.get("data_dict"),
    )

    ros2docker_config = _resolve_path(ros2docker_config_raw, config_base, must_exist=True)
    if ros2docker_config is None:
        raise RuntimeError("ros2docker config could not be resolved.")

    return RuntimeConfig(
        rosotacom_config=rosotacom_config,
        ros2docker_config=ros2docker_config,
        session_configs_dir=_resolve_path(session_configs_dir_raw, config_base, must_exist=True),
        data_dict=_resolve_path(data_dict_raw, config_base, must_exist=True),
        install_id=_install_id(rosotacom_config),
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
            "for example 'b=tks-leitstand-02_tks' or 'b=data:tks-leitstand-02_tks'."
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
                        f"{SESSION_CONFIG_CONTAINER_DIR}/{cfg_rel.as_posix()}",
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


def _base_extra_run_args(runtime: RuntimeConfig, session: ResolvedSession, cfg: dict[str, Any]) -> list[str]:
    args: list[str] = []
    ros2docker_cfg = load_config(runtime.ros2docker_config)

    if not ros2docker_cfg.get("mount_ws"):
        args.extend(["-v", f"{WS_DIR.resolve()}:/ws", "-w", "/ws"])
    if runtime.session_configs_dir:
        args.extend(
            [
                "-v",
                f"{runtime.session_configs_dir}:{SESSION_CONFIG_CONTAINER_DIR}",
                "-e",
                f"SESSION_CONFIGS_DIR={SESSION_CONFIG_CONTAINER_DIR}",
            ]
        )
    if session.container_dir == EXTERNAL_SESSION_CONTAINER_DIR:
        args.extend(["-v", f"{session.host_dir}:{EXTERNAL_SESSION_CONTAINER_DIR}"])
    if _contains_data_ref(cfg):
        data_dict = _load_host_data_dict(runtime)
        if not isinstance(data_dict, dict):
            raise RuntimeError("data_dict.json must contain a JSON object.")
        args.extend(["-v", f"{runtime.data_dict}:{CONTAINER_DATA_DICT_PATH}:ro"])
    return args


def _session_command(
    session: ResolvedSession,
    identity: str,
    *,
    force: bool,
    rewrite_formatting: bool,
    overwrite_peers_via_remote_peer: str | None,
    peer_address_overrides: dict[str, str],
    attach_mode: str,
) -> list[str]:
    parts = [RUN_SESSION_CONTAINER_PATH, "--session-dir", session.container_dir, "--identity", identity]
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
    image_name = _scoped_image_name(runtime)
    extra_run_args = _base_extra_run_args(runtime, session, cfg)
    mode = _resolve_mode(args.mode)
    command = _session_command(
        session,
        identity,
        force=args.force,
        rewrite_formatting=args.rewrite_formatting,
        overwrite_peers_via_remote_peer=args.overwrite_peers_via_remote_peer,
        peer_address_overrides=peer_overrides,
        attach_mode=mode,
    )

    print("Command which will be run in container: " + shlex.join(command))
    if args.force:
        _stop_container_name(container_name, runtime, quiet_missing=True)

    common_override = {"container_name": container_name, "image_name": image_name}
    if mode == "detached":
        ros2docker_build(config_file=runtime.ros2docker_config, override=common_override)
        ros2docker_run(
            config_file=runtime.ros2docker_config,
            override={**common_override, "run_type": "up"},
            extra_run_args=extra_run_args,
        )
        _wait_for_container_ready(container_name)
        ros2docker_exec(
            config_file=runtime.ros2docker_config,
            override=common_override,
            command=command,
            interactive=False,
        )
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
            },
            extra_run_args=extra_run_args,
        )

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
    print(f"Copied rosotacom examples to: {target}")
    print(f'Wire this shell with: eval "$(rosotacom setup-env {shlex.quote(str(target / "rosotacom.yaml"))})"')
    return 0


def setup_env_command(args: argparse.Namespace) -> int:
    config = _resolve_path(args.rosotacom_config, Path.cwd(), must_exist=True)
    if not config or not config.is_file():
        raise RuntimeError(f"rosotacom config must be a file: {args.rosotacom_config}")
    print(f"export ROSOTACOM_CONFIG={shlex.quote(str(config))}")
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
            line("OK", "rosotacom config", str(runtime.rosotacom_config))
        else:
            line("INFO", "rosotacom config", "not configured; use --rosotacom-config or ROSOTACOM_CONFIG")
        line("OK", "ros2docker config", str(runtime.ros2docker_config))
        line("OK", "install id", runtime.install_id)
        line("OK", "workspace mount", f"{WS_DIR} -> /ws")
        if runtime.session_configs_dir:
            line("OK", "session configs", f"{runtime.session_configs_dir} -> {SESSION_CONFIG_CONTAINER_DIR}")
        else:
            line("INFO", "session configs", "not configured")
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


def _run_container_shell(container_name: str, command: str, timeout_s: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", container_name, "bash", "-lc", command],
        text=True,
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )


def _wait_for_topic_hz(container_name: str, ros_setup: str, topic: str, *, timeout_s: int = 60) -> str:
    deadline = time.time() + timeout_s
    last_output = ""
    while time.time() < deadline:
        result = _run_container_shell(
            container_name,
            f"{ros_setup} && timeout 8 ros2 topic hz {shlex.quote(topic)} || true",
            timeout_s=12,
        )
        last_output = (result.stdout or "") + (result.stderr or "")
        if "average rate" in last_output:
            return last_output
        time.sleep(2)
    return last_output


def _alternate_loopback_ip(local_ip: str) -> str:
    if local_ip == "127.0.0.2":
        return "127.0.0.3"
    return "127.0.0.2"


def _smoke_needs_distinct_peer_addresses(session: ResolvedSession) -> bool:
    cfg = _load_session_config_from_host(session.host_dir)
    peers = cfg.get("peers")
    if not isinstance(peers, dict):
        return False
    shared = cfg.get("shared", {}) or {}
    if not isinstance(shared, dict):
        return False
    rmw_spec = session_gen._parse_rmw_block(shared.get("rmw"), list(peers.keys()))
    return bool(session_gen._is_native_zenoh_ota(rmw_spec.ota.impl))


def _smoke_peer_address_args(session: ResolvedSession, local_ip: str) -> list[str]:
    if _smoke_needs_distinct_peer_addresses(session):
        return [f"a={local_ip}", f"b={_alternate_loopback_ip(local_ip)}"]
    return [f"a={local_ip}", f"b={local_ip}"]


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
    return session_gen.RMW_ALIASES.get(local_impl, local_impl)


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


def _smoke_local_config_commands(session: ResolvedSession, cfg: dict[str, Any], receiver_peer_key: str) -> list[str]:
    local = _smoke_rmw_spec(cfg).local
    if not local.dds_config:
        return []
    config_file = f"{session.container_dir}/{receiver_peer_key}/local_dds.xml"
    if local.impl == "cyclone":
        return [f"export CYCLONEDDS_URI={shlex.quote(f'file://{config_file}')}"]
    if local.impl == "fastdds":
        return [f"export FASTDDS_DEFAULT_PROFILES_FILE={shlex.quote(config_file)}"]
    return []


def _smoke_ros_setup(session: ResolvedSession, cfg: dict[str, Any], receiver_peer_key: str) -> str:
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
    commands.extend(_smoke_local_config_commands(session, cfg, receiver_peer_key))
    return " && ".join(commands)


def smoke(args: argparse.Namespace) -> int:
    if not args.local:
        raise RuntimeError("Only --local smoke mode is implemented.")
    local_ip = args.local_ip or _default_local_ip()
    session_dir = args.session_dir or DEFAULT_SMOKE_SESSION
    runtime = _load_runtime_config(args)
    session = _resolve_session(session_dir, runtime)
    peer_address_args = _smoke_peer_address_args(session, local_ip)
    peer_overrides = _parse_peer_address_overrides(peer_address_args)
    cfg = _effective_session_config(session.host_dir, runtime, peer_address_overrides=peer_overrides)
    common = {
        "rosotacom_config": args.rosotacom_config,
        "ros2docker_config": args.ros2docker_config,
        "session_configs_dir": args.session_configs_dir,
        "data_dict": args.data_dict,
        "session_dir": session_dir,
        "mode": "detached",
        "force": True,
        "rewrite_formatting": False,
        "overwrite_peers_via_remote_peer": None,
        "peer_address": peer_address_args,
    }

    print(f"Starting local smoke test with PEER_ADDRESSES={', '.join(peer_address_args)}")
    a_container = None
    b_container = None
    try:
        a_container = start_session(argparse.Namespace(**common, identity="a", auto_identity=True))
        b_container = start_session(argparse.Namespace(**common, identity="b", auto_identity=True))

        plugin_text = "\n".join(
            (session.host_dir / peer / "plugin.yaml").read_text(encoding="utf-8") for peer in ("a", "b")
        )
        expected_addresses = {arg.split("=", 1)[1] for arg in peer_address_args}
        if any(address not in plugin_text for address in expected_addresses) or "data:" in plugin_text:
            raise RuntimeError("Smoke verification failed: generated plugin.yaml did not use literal CLI addresses.")
        print("OK: generated plugin.yaml files use literal CLI addresses")

        checks = [
            (b_container, _smoke_inbound_bridge_topic(cfg, "a"), "a->b inbound bridge heartbeat", "b"),
            (b_container, _smoke_heartbeat_topic(cfg, "a"), "a->b final heartbeat", "b"),
            (a_container, _smoke_inbound_bridge_topic(cfg, "b"), "b->a inbound bridge heartbeat", "a"),
            (a_container, _smoke_heartbeat_topic(cfg, "b"), "b->a final heartbeat", "a"),
        ]
        for container_name, topic, label, receiver_peer_key in checks:
            ros_setup = _smoke_ros_setup(session, cfg, receiver_peer_key)
            output = _wait_for_topic_hz(container_name, ros_setup, topic)
            if "average rate" not in output:
                raise RuntimeError(f"Smoke verification failed for {label} ({topic}) in {container_name}:\n{output}")
            print(f"OK: {label} ({topic}) is publishing in {container_name}")
    finally:
        if not args.keep_running:
            runtime = _load_runtime_config(args)
            for started_container in [a_container, b_container]:
                if started_container:
                    _stop_container_name(started_container, runtime)
    return 0


def _add_common_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rosotacom-config", help="Path to rosotacom.yaml.")
    parser.add_argument("-f", "--ros2docker-config", help="Path to ros2docker JSON config.")
    parser.add_argument("--session-configs-dir", help="Host directory containing named session configs.")
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
    commands = {"start", "stop", "doctor", "smoke", "list-sessions", "examples", "setup-env"}
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
    smoke_parser.set_defaults(func=smoke)

    list_parser = subparsers.add_parser("list-sessions", help="List configured sessions.")
    _add_common_config_args(list_parser)
    list_parser.set_defaults(func=list_sessions_command)

    examples_parser = subparsers.add_parser("examples", help="Manage packaged rosotacom examples.")
    examples_subparsers = examples_parser.add_subparsers(dest="examples_command", required=True)
    examples_create_parser = examples_subparsers.add_parser("create", help="Copy the packaged example project.")
    examples_create_parser.add_argument("target", help="Directory to create.")
    examples_create_parser.add_argument("--force", action="store_true", help="Replace the target if it exists.")
    examples_create_parser.set_defaults(func=examples_create_command)

    setup_env_parser = subparsers.add_parser("setup-env", help="Print shell exports for a rosotacom setup file.")
    setup_env_parser.add_argument("rosotacom_config", help="Path to rosotacom.yaml.")
    setup_env_parser.set_defaults(func=setup_env_command)

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
