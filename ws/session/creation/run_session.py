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

import argparse
import copy
import re
import yaml
import subprocess
import sys
import os
from typing import Dict, Optional, Tuple

ws_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.append(ws_path)

from session.creation.create_session_yaml import main as create_session_yaml
from session.creation import generate_session_files as session_gen
from session.content.address_resolution import (
    find_data_dict_leaf,
    format_data_reference,
    load_data_dict,
    parse_data_reference,
)


_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_CATMUX_NO_SERVER_RE = re.compile(
    r"^error connecting to /tmp/tmux-\d+/catmux \(No such file or directory\)$"
)


def _get_local_ipv4s() -> list:
    try:
        out = subprocess.check_output(["ip", "-o", "-4", "addr", "show"], text=True)
    except Exception:
        return []
    return _IPV4_RE.findall(out)


def _parse_remote_peer_override(override: str) -> Tuple[str, str]:
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


def _run_catmux(full_command, attach: bool) -> None:
    if attach:
        subprocess.run(full_command, check=True)
        return

    result = subprocess.run(full_command, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="")

    stderr_lines = [
        line
        for line in (result.stderr or "").splitlines()
        if not _CATMUX_NO_SERVER_RE.match(line.strip())
    ]
    if stderr_lines:
        print("\n".join(stderr_lines), file=sys.stderr)

    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, full_command)


def _parse_peer_address_overrides(overrides) -> Dict[str, str]:
    result: Dict[str, str] = {}
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


def _load_session_config_input(session_config_yaml: str) -> Dict:
    param_dir = os.path.dirname(session_config_yaml)
    param = session_gen._load_yaml(session_config_yaml)
    if not isinstance(param, dict):
        raise RuntimeError(
            f"Session config input YAML must be a mapping, got {type(param)} for '{session_config_yaml}'"
        )

    if "load_template" in param:
        session_template_fs, provided_params = session_gen._parse_session_config_template_spec(param, param_dir)
        cfg_raw = session_gen._load_yaml(session_template_fs)
        vars_map = session_gen._build_vars_map_from_template(cfg_raw, provided_params)
        cfg = session_gen._substitute(cfg_raw, vars_map) or {}
    else:
        cfg = dict(param)

    session_gen._validate_session_template_cfg(cfg)
    return cfg


def _apply_peer_address_overrides_to_cfg(cfg: Dict, peer_address_overrides: Dict[str, str]) -> Dict:
    if not peer_address_overrides:
        return cfg

    cfg = copy.deepcopy(cfg)
    peers = (cfg or {}).get("peers")
    if not isinstance(peers, dict):
        raise RuntimeError("Peer address override requires a session config with a 'peers' mapping.")

    unknown = sorted([peer for peer in peer_address_overrides if peer not in peers])
    if unknown:
        raise RuntimeError(
            f"--peer-address references unknown peer(s) {unknown}. "
            f"Known peers: {sorted(peers.keys())}"
        )

    for peer_key, address_value in peer_address_overrides.items():
        peers[peer_key] = dict(peers[peer_key])
        peers[peer_key]["address"] = address_value

    session_gen._validate_session_template_cfg(cfg)
    return cfg


def _apply_remote_peer_override(session_config_yaml: str, override: Optional[str]) -> Optional[Dict]:
    if not override:
        return None

    cfg = copy.deepcopy(_load_session_config_input(session_config_yaml))
    peers = (cfg or {}).get("peers")
    if not isinstance(peers, dict) or len(peers) != 2:
        raise RuntimeError(
            "Peer override currently requires exactly 2 peers in the session config."
        )

    remote_peer_key, remote_data_key = _parse_remote_peer_override(override)
    if remote_peer_key not in peers:
        raise RuntimeError(
            f"Peer override references unknown session peer '{remote_peer_key}'. "
            f"Known peers: {sorted(peers.keys())}"
        )

    local_peer_key = next(k for k in peers.keys() if k != remote_peer_key)
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

    local_ips = set(_get_local_ipv4s())
    if not local_ips:
        raise RuntimeError(
            "Peer override failed: could not determine local IPv4 addresses."
        )

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
    peers[remote_peer_key]["address"] = format_data_reference(remote_data_key)
    peers[local_peer_key]["address"] = format_data_reference(local_data_key)
    return cfg


def _apply_session_overrides(
    session_config_yaml: str,
    overwrite_peers_via_remote_peer: Optional[str],
    peer_address_overrides_raw,
) -> Optional[Dict]:
    peer_address_overrides = _parse_peer_address_overrides(peer_address_overrides_raw)
    cfg = _apply_remote_peer_override(session_config_yaml, overwrite_peers_via_remote_peer)
    if peer_address_overrides:
        if cfg is None:
            cfg = _load_session_config_input(session_config_yaml)
        cfg = _apply_peer_address_overrides_to_cfg(cfg, peer_address_overrides)
    return cfg


def _resolve_peer_dir(
    session_dir: str,
    identity: Optional[str],
    force: bool,
    rewrite_formatting: bool,
    overwrite_peers_via_remote_peer: Optional[str] = None,
    peer_address=None,
) -> str:
    """
    Resolve a runnable session directory (the directory containing session_specification.yaml).

    Supported input:
    - session directory that contains one of:
      - session-definition.yaml (self-contained)
      - session-parametrization.yaml (template + parameters)
      plus: --identity <peer_key>
    """
    if not os.path.isabs(session_dir):
        base = os.environ.get("SESSION_CONFIGS_DIR")
        if base:
            candidate = os.path.join(base, session_dir)
            if os.path.isdir(candidate):
                session_dir = candidate

    p = os.path.abspath(session_dir)
    if not os.path.isdir(p):
        raise RuntimeError(f"--session-dir must be a directory, got: {p}")

    if not identity:
        raise RuntimeError(
            "Missing --identity. Provide --identity <name of the peer>."
        )

    # Resolve which session config input file to use.
    #
    # Precedence:
    # - session-parametrization.yaml (if present): treated as the "primary" input even if a generated
    #   session-definition.yaml also exists.
    # - session-definition.yaml
    candidates = [
        "session-parametrization.yaml",
        "session-definition.yaml",
    ]
    param_yaml = None
    for name in candidates:
        fp = os.path.join(p, name)
        if os.path.exists(fp):
            param_yaml = fp
            break
    if not param_yaml:
        raise RuntimeError(
            "Missing session config input file in session dir. Expected one of: "
            f"{candidates}. Got dir: {p}"
        )

    cfg_override = _apply_session_overrides(
        param_yaml,
        overwrite_peers_via_remote_peer,
        peer_address,
    )
    session_gen.func(
        session_config_yaml=param_yaml,
        force=force,
        rewrite_formatting=rewrite_formatting,
        session_config_obj=cfg_override,
        output_dir=p,
        write_resolved_definition=False if cfg_override is not None else None,
    )
    peer_dir = os.path.join(p, identity)
    spec_file = os.path.join(peer_dir, "session_specification.yaml")
    if not os.path.exists(spec_file):
        # Try to help with a quick "what identities exist" hint.
        try:
            subdirs = sorted(
                d for d in os.listdir(p) if os.path.isdir(os.path.join(p, d)) and not d.startswith(".")
            )
        except Exception:
            subdirs = []
        hint = f" Available subdirs: {subdirs}" if subdirs else ""
        raise RuntimeError(f"Identity '{identity}' did not resolve to a runnable peer dir: missing {spec_file}.{hint}")
    return peer_dir


def main(
    session_dir: str,
    identity: Optional[str] = None,
    force: bool = False,
    rewrite_formatting: bool = False,
    overwrite_peers_via_remote_peer: Optional[str] = None,
    peer_address=None,
    attach: Optional[bool] = None,
):

    peer_dir = _resolve_peer_dir(
        session_dir,
        identity,
        force,
        rewrite_formatting,
        overwrite_peers_via_remote_peer=overwrite_peers_via_remote_peer,
        peer_address=peer_address,
    )

    # Ensure merged .session_readonly.yaml exists for catmux
    create_session_yaml(peer_dir)

    # Define the command and arguments
    command = "catmux_create_session"
    yaml_file_path = f"{peer_dir}/.session_readonly.yaml"
    session_name_arg = "--session_name"

    # get name
    spec_file = os.path.join(peer_dir, "session_specification.yaml")
    with open(spec_file, "r") as f:
        spec = yaml.safe_load(f)
    session_name = (spec or {}).get("name", "ros_communication")

    # Combine them into a single command
    full_command = [
        command,
        yaml_file_path,
        session_name_arg,
        session_name,
        "--overwrite",
        f"dir_path={peer_dir}",
    ]
    if attach is None:
        attach = sys.stdin.isatty() and sys.stdout.isatty()
    if not attach:
        full_command.append("--detach")

    # Execute the command
    try:
        _run_catmux(full_command, attach)
        print("Command executed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"An error occurred while executing the command: {e}")
        raise SystemExit(e.returncode)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate peer session files from a session definition/parametrization and launch catmux for one identity."
    )
    parser.add_argument(
        "-s",
        "--session-dir",
        required=True,
        help="Directory containing session-definition.yaml or session-parametrization.yaml.",
    )
    parser.add_argument(
        "--identity",
        required=True,
        type=str,
        help="Which peer to launch.",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="If generating session files: overwrite existing files even if they differ semantically.",
    )
    parser.add_argument(
        "--rewrite-formatting",
        action="store_true",
        help="If generating session files: rewrite files even when semantically equal (format-only differences).",
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
    attach_group = parser.add_mutually_exclusive_group()
    attach_group.add_argument(
        "--attach",
        dest="attach",
        action="store_true",
        help="Attach to the catmux/tmux session after creating it.",
    )
    attach_group.add_argument(
        "--detach",
        dest="attach",
        action="store_false",
        help="Create the catmux/tmux session and return without attaching.",
    )
    parser.set_defaults(attach=None)
    args = parser.parse_args()

    # Use **vars(args) to convert argparse.Namespace to a dict, filtering out None values
    main(**{k: v for k, v in vars(args).items() if v is not None})
