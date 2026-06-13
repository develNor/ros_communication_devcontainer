#!/usr/bin/env python3

# -- BEGIN LICENSE BLOCK ----------------------------------------------
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
#    * Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
# 
#    * Redistributions in binary form must reproduce the above copyright
#      notice, this list of conditions and the following disclaimer in the
#      documentation and/or other materials provided with the distribution.
# 
#    * Neither the name of the {copyright_holder} nor the names of its
#      contributors may be used to endorse or promote products derived from
#      this software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.                                                
# -- END LICENSE BLOCK ------------------------------------------------
#
# ---------------------------------------------------------------------
# !\file
#
# \author  Martin Gontscharow <gontscharow@fzi.de>
# \date    2024-11-13
#
#
# ---------------------------------------------------------------------

import os
import sys
import argparse
import re
import yaml
import subprocess
import importlib.util
import difflib
import shlex

project_dir = os.path.dirname(os.path.realpath(__file__))
CONFIG_PATH = os.path.join(project_dir, "ros2docker.json")

from ros2docker.api import build_run
from ros2docker.config import get_config_dir, load_config

ws_creation_dir = os.path.join(project_dir, "ws", "session", "creation")
session_gen_path = os.path.join(ws_creation_dir, "generate_session_files.py")
spec = importlib.util.spec_from_file_location("session_gen", session_gen_path)
session_gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session_gen)

ws_dir = os.path.join(project_dir, "ws")
sys.path.append(ws_dir)
from session.content.address_resolution import (  # noqa: E402
    find_data_dict_leaf,
    format_data_reference,
    load_data_dict,
    parse_data_reference,
    resolve_address_expressions,
)

# hotfix where usage of robot folders leads to problems
# unwanted_path = "/home/carpc/robot_folders/src/robot_folders"
# if unwanted_path in sys.path: 
#     sys.path.remove(unwanted_path)

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _parse_remote_peer_override(override: str):
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


def _parse_peer_address_overrides(overrides) -> dict:
    result = {}
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


def _apply_remote_peer_override_to_cfg(cfg: dict, override: str, local_ips=None) -> dict:
    if not override:
        return cfg

    peers = (cfg or {}).get("peers")
    if not isinstance(peers, dict) or len(peers) != 2:
        raise RuntimeError("Peer override currently requires exactly 2 peers in the session config.")

    remote_peer_key, remote_data_key = _parse_remote_peer_override(override)
    if remote_peer_key not in peers:
        raise RuntimeError(
            f"Peer override references unknown session peer '{remote_peer_key}'. "
            f"Known peers: {sorted(peers.keys())}"
        )

    data_dict = load_data_dict()
    group_name, remote_ip = find_data_dict_leaf(data_dict, remote_data_key)
    if not group_name:
        raise RuntimeError(
            f"Peer override key '{remote_data_key}' is not inside a data_dict group. "
            "Automatic local peer inference requires grouped entries."
        )

    group = data_dict.get(group_name)
    if not isinstance(group, dict):
        raise RuntimeError(f"data_dict group '{group_name}' must be a mapping.")

    if local_ips is None:
        local_ips = set(_get_local_ipv4s())
    else:
        local_ips = set(local_ips)
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


def _apply_peer_address_overrides_to_cfg(cfg: dict, peer_address_overrides: dict) -> dict:
    if not peer_address_overrides:
        return cfg

    peers = (cfg or {}).get("peers")
    if not isinstance(peers, dict):
        raise RuntimeError("Peer address override requires a session config with a 'peers' mapping.")

    unknown = sorted([peer for peer in peer_address_overrides if peer not in peers])
    if unknown:
        raise RuntimeError(
            f"--peer-address references unknown peer(s) {unknown}. "
            f"Known peers: {sorted(peers.keys())}"
        )

    cfg = dict(cfg)
    cfg["peers"] = dict(peers)
    for peer_key, address_value in peer_address_overrides.items():
        cfg["peers"][peer_key] = dict(cfg["peers"][peer_key])
        cfg["peers"][peer_key]["address"] = address_value
    return cfg


def _get_local_ipv4s() -> list:
    try:
        out = subprocess.check_output(["ip", "-o", "-4", "addr", "show"], text=True)
    except Exception:
        return []
    return _IPV4_RE.findall(out)


def _resolve_session_configs_base() -> str:
    local_config = load_config(CONFIG_PATH)
    session_configs_dir = local_config.get("session_configs_dir")
    if not session_configs_dir:
        return None
    config_dir = get_config_dir(CONFIG_PATH)
    if os.path.isabs(session_configs_dir):
        if os.path.isdir(session_configs_dir):
            return session_configs_dir
        run_args = local_config.get("run_args", [])
        mapped = _map_container_path_to_host(session_configs_dir, run_args, config_dir)
        return mapped if mapped else session_configs_dir
    return os.path.abspath(os.path.join(config_dir, session_configs_dir))


def _list_session_configs() -> list:
    base = _resolve_session_configs_base()
    if not base or not os.path.isdir(base):
        return []
    try:
        entries = os.listdir(base)
    except OSError:
        return []
    session_dirs = []
    for name in sorted(entries):
        p = os.path.join(base, name)
        if os.path.isdir(p):
            session_dirs.append(name)
    return session_dirs


def _list_example_sessions() -> list:
    example_dir = os.path.join(project_dir, "ws", "example")
    if not os.path.isdir(example_dir):
        return []
    try:
        entries = os.listdir(example_dir)
    except OSError:
        return []
    sessions = []
    for name in sorted(entries):
        p = os.path.join(example_dir, name)
        if os.path.isdir(p):
            sessions.append(f"example/{name}")
    return sessions


def _format_available_sessions() -> str:
    sessions = _list_session_configs()
    examples = _list_example_sessions()
    if not sessions and not examples:
        return "No session directories found."
    parts = []
    if sessions:
        joined = "\n  - ".join(sessions)
        parts.append(f"Available sessions:\n  - {joined}")
    if examples:
        joined = "\n  - ".join(examples)
        parts.append(f"Built-in examples:\n  - {joined}")
    return "\n".join(parts)


def _suggest_sessions(session_dir: str) -> list:
    sessions = _list_session_configs() + _list_example_sessions()
    if not sessions or not session_dir:
        return []
    return difflib.get_close_matches(session_dir, sessions, n=5, cutoff=0.6)


def _auto_identity(
    session_dir: str,
    overwrite_peers_via_remote_peer: str = None,
    peer_address_overrides: dict = None,
) -> str:
    cfg = _load_session_config(session_dir)
    local_ips = set(_get_local_ipv4s())
    if overwrite_peers_via_remote_peer:
        cfg = _apply_remote_peer_override_to_cfg(
            cfg,
            overwrite_peers_via_remote_peer,
            local_ips=local_ips,
        )
    cfg = _apply_peer_address_overrides_to_cfg(cfg, peer_address_overrides or {})
    session_gen._validate_session_template_cfg(cfg)
    peers = (cfg or {}).get("peers")
    if not isinstance(peers, dict):
        raise RuntimeError("session-config must define a mapping 'peers: { <peer_key>: { ... } }'.")

    peer_keys = list(peers.keys())
    if not peer_keys:
        raise RuntimeError("session-config must define at least one peer.")

    if not local_ips:
        raise RuntimeError("Auto identity failed: could not determine local IPv4 addresses. Use --identity.")

    matches = []
    for peer_key in peer_keys:
        address = (peers.get(peer_key) or {}).get("address")
        if not address:
            continue
        resolved = resolve_address_expressions(str(address))
        peer_ips = set()
        for value in resolved:
            peer_ips.update(_extract_ipv4s(value))
        if local_ips.intersection(peer_ips):
            matches.append(peer_key)

    if len(matches) == 1:
        return matches[0]
    if len(matches) == 0:
        raise RuntimeError(
            f"Auto identity failed: no peer address matched local IPv4s={sorted(local_ips)}. "
            "Use --identity."
        )
    raise RuntimeError(
        f"Auto identity ambiguous: matched peers={matches} for local IPv4s={sorted(local_ips)}. "
        "Use --identity."
    )

def _resolve_host_session_dir(session_dir: str) -> str:
    p = os.path.abspath(session_dir)
    if os.path.isdir(p):
        return p

    local_config = load_config(CONFIG_PATH)
    run_args = local_config.get("run_args", [])
    config_dir = get_config_dir(CONFIG_PATH)

    session_configs_dir = local_config.get("session_configs_dir")
    if session_configs_dir and not os.path.isabs(session_dir):
        base = (
            session_configs_dir
            if os.path.isabs(session_configs_dir)
            else os.path.abspath(os.path.join(config_dir, session_configs_dir))
        )
        candidate = os.path.join(base, session_dir)
        if os.path.isdir(candidate):
            return candidate
        resolved = _map_container_path_to_host(candidate, run_args, config_dir)
        if resolved and os.path.isdir(resolved):
            return resolved

    if not os.path.isabs(session_dir):
        ws_candidate = os.path.join(project_dir, "ws", session_dir)
        if os.path.isdir(ws_candidate):
            return ws_candidate

    resolved = _map_container_path_to_host(p, run_args, config_dir)
    if resolved and os.path.isdir(resolved):
        return resolved

    hints = []
    suggestions = _suggest_sessions(os.path.basename(session_dir))
    if suggestions:
        hints.append("Did you mean: " + ", ".join(suggestions))
    hints.append(_format_available_sessions())
    hint_text = "\n".join(hints) if hints else ""
    raise RuntimeError(
        f"--session-dir must be a directory, got: {p} "
        "and could not map container path via config run_args."
        + (f"\n{hint_text}" if hint_text else "")
    )


def _map_container_path_to_host(container_path: str, run_args: list, config_dir: str):
    i = 0
    while i < len(run_args):
        arg = run_args[i]
        if arg == "-v" and i + 1 < len(run_args):
            volume = run_args[i + 1]
            host_path, container_mount = volume.split(":", 1)
            if host_path.startswith("../") or host_path.startswith("./"):
                host_path = os.path.realpath(os.path.join(config_dir, host_path))

            if container_path == container_mount or container_path.startswith(container_mount + "/"):
                suffix = container_path[len(container_mount):]
                return os.path.join(host_path, suffix.lstrip("/"))
            i += 2
        else:
            i += 1
    return None


def _load_session_config(session_dir: str) -> dict:
    p = _resolve_host_session_dir(session_dir)

    candidates = [
        "session-parametrization.yaml",
        "session-definition.yaml",
    ]
    for name in candidates:
        fp = os.path.join(p, name)
        if os.path.exists(fp):
            with open(fp, "r") as f:
                param = yaml.safe_load(f) or {}

            # If it's a parametrization, resolve the template so we can access peers.
            if isinstance(param, dict) and "load_template" in param:
                param_dir = os.path.dirname(fp)
                session_template_fs, provided_params = session_gen._parse_session_config_template_spec(
                    param, param_dir
                )
                cfg_raw = session_gen._load_yaml(session_template_fs)
                vars_map = session_gen._build_vars_map_from_template(cfg_raw, provided_params)
                return session_gen._substitute(cfg_raw, vars_map) or {}

            return param

    raise RuntimeError(
        "Missing session config input file in session dir. Expected one of: "
        f"{candidates}. Got dir: {p}"
    )


def _resolve_remote_peer_name(
    session_dir: str,
    identity: str,
    overwrite_peers_via_remote_peer: str = None,
    peer_address_overrides: dict = None,
) -> str:
    cfg = _load_session_config(session_dir)
    if overwrite_peers_via_remote_peer:
        cfg = _apply_remote_peer_override_to_cfg(cfg, overwrite_peers_via_remote_peer)
    cfg = _apply_peer_address_overrides_to_cfg(cfg, peer_address_overrides or {})
    session_gen._validate_session_template_cfg(cfg)
    peers = (cfg or {}).get("peers")
    if not isinstance(peers, dict):
        raise RuntimeError("session-config must define a mapping 'peers: { <peer_key>: { ... } }'.")

    peer_keys = list(peers.keys())
    if len(peer_keys) != 2:
        raise RuntimeError(f"Expected exactly 2 peers, got peers={peer_keys}")
    if identity not in peer_keys:
        raise RuntimeError(f"--identity must be one of peers={peer_keys}")

    def _peer_com_name(peer_key: str) -> str:
        v = None
        try:
            v = (peers[peer_key] or {}).get("com-name")
        except Exception:
            v = None
        if v is None:
            return peer_key
        if isinstance(v, str):
            s = v.strip()
            return s if s else peer_key
        return str(v) if v else peer_key

    remote_peer_key = next(k for k in peer_keys if k != identity)
    return _peer_com_name(remote_peer_key)


def _sanitize_container_name(name: str) -> str:
    # Docker container name: allow [a-zA-Z0-9_.-]; replace others with "_"
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", name)


def main(
    session_dir,
    identity=None,
    force=True,
    rewrite_formatting=False,
    auto_identity=True,
    overwrite_peers_via_remote_peer=None,
    peer_address=None,
):
    peer_address_overrides = _parse_peer_address_overrides(peer_address)
    if not session_dir:
        print("ERROR: --session-dir is required.\n", file=sys.stderr)
        print(_format_available_sessions(), file=sys.stderr)
        raise SystemExit(2)
    if not identity:
        if auto_identity:
            identity = _auto_identity(
                session_dir,
                overwrite_peers_via_remote_peer=overwrite_peers_via_remote_peer,
                peer_address_overrides=peer_address_overrides,
            )
            print(f"Auto-selected identity: {identity}")
        else:
            raise RuntimeError(
                "Missing --identity. Provide --identity <name of the peer> or use --auto-identity."
            )

    script_path = f"/ws/session/creation/run_session.py"
    docker_command_parts = [script_path, "--session-dir", session_dir]
    if identity is not None:
        docker_command_parts.extend(["--identity", identity])
    if force:
        docker_command_parts.append("--force")
    if rewrite_formatting:
        docker_command_parts.append("--rewrite-formatting")
    if overwrite_peers_via_remote_peer:
        docker_command_parts.extend(["--overwrite-peers-via-remote-peer", overwrite_peers_via_remote_peer])
    for peer_key, address_value in peer_address_overrides.items():
        docker_command_parts.extend(["--peer-address", f"{peer_key}={address_value}"])
    docker_command = " ".join(shlex.quote(str(part)) for part in docker_command_parts)

    print(f"Command which will be run in container: {docker_command}")
    remote_peer_name = _resolve_remote_peer_name(
        session_dir,
        identity,
        overwrite_peers_via_remote_peer=overwrite_peers_via_remote_peer,
        peer_address_overrides=peer_address_overrides,
    )
    container_name = _sanitize_container_name(f"com_to_{remote_peer_name}")
    local_config = load_config(CONFIG_PATH)
    override = {
        "run_type": "command",
        "command": docker_command,
        "container_name": container_name,
    }
    session_configs_dir = local_config.get("session_configs_dir")
    if session_configs_dir:
        run_args = list(local_config.get("run_args", []))
        run_args.extend(["-e", f"SESSION_CONFIGS_DIR={session_configs_dir}"])
        override["run_args"] = run_args

    build_run(config_file=CONFIG_PATH, override=override)

    print("Script execution in container completed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument(
        "session_dir_positional",
        nargs="?",
        help="Directory containing a session config input file (session-definition.yaml / session-parametrization.yaml) and where generated files will be written.",
    )
    parser.add_argument(
        "-s",
        "--session-dir",
        required=False,
        help="Directory containing a session config input file (session-definition.yaml / session-parametrization.yaml) and where generated files will be written.",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="List available session directories from session_configs_dir and exit.",
    )
    parser.add_argument(
        "--identity",
        required=False,
        help="Which peer to launch (optional if --auto-identity is set).",
    )
    parser.add_argument(
        "--no-auto-identity",
        dest="auto_identity",
        action="store_false",
        help="Disable identity auto-detection.",
    )
    parser.add_argument(
        "--no-force",
        dest="force",
        action="store_false",
        help="Do not overwrite existing generated files.",
    )
    parser.add_argument(
        "--overwrite-peers-via-remote-peer",
        type=str,
        help=(
            "Ephemerally override peer addresses via '<remote_peer_key>=<data_dict_key>' "
            "and infer the local peer address from the same data_dict group."
        ),
    )
    parser.add_argument(
        "--peer-address",
        action="append",
        default=[],
        metavar="PEER=ADDRESS_EXPR",
        help=(
            "Ephemerally override peers.<peer>.address for this launch. "
            "May be used more than once, e.g. --peer-address a=192.168.1.10 "
            "--peer-address b=data:machine_b_ip."
        ),
    )
    parser.add_argument("--rewrite-formatting", action="store_true")
    parser.set_defaults(force=True, auto_identity=True)
    args = parser.parse_args()
    if args.list_sessions:
        print(_format_available_sessions())
        raise SystemExit(0)
    if args.session_dir and args.session_dir_positional and args.session_dir != args.session_dir_positional:
        raise SystemExit(
            f"ERROR: conflicting session dirs: --session-dir={args.session_dir} "
            f"and positional={args.session_dir_positional}"
        )
    if not args.session_dir and args.session_dir_positional:
        args.session_dir = args.session_dir_positional
    main(
        **{
            k: v
            for k, v in vars(args).items()
            if v is not None and k not in {"list_sessions", "session_dir_positional"}
        }
    )
