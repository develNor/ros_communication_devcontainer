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
# \date    2026-01-09
#
#
# ---------------------------------------------------------------------

import argparse
import difflib
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import yaml


BASE_PLUGIN_PATH_DEFAULT = "/ws/session/content/base/session_plugin_base.yaml"

VAR_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
FULL_VAR_PATTERN = re.compile(r"^\$\{([A-Za-z0-9_]+)\}$")

# Compression algorithms supported by com_py universal_{de}compressor nodes
ALLOWED_COMPRESSION_ALGORITHMS = {"bz2", "zlib", "lz4", "zstd"}
RMW_ALIASES = {
    "cyclone": "rmw_cyclonedds_cpp",
    "fastdds": "rmw_fastrtps_cpp",
    "zenoh": "rmw_zenoh_cpp",
}
HEARTBEAT_MSG_TYPE = "com_msgs/msg/Heartbeat"
COMPRESSED_MSG_TYPE = "com_msgs/msg/CompressedData"
OTA_STAMPED_MSG_TYPE = "com_msgs/msg/OtaStamped"
TRANSPORT_OUTPUT_TYPES = {
    "compressed": "sensor_msgs/msg/CompressedImage",
    "ffmpeg": "ffmpeg_image_transport_msgs/msg/FFMPEGPacket",
    "foxglove": "foxglove_msgs/msg/CompressedVideo",
}


@dataclass
class TransportSpec:
    type: str
    params: Dict[str, Any]
    local_republish: bool = False


@dataclass
class TopicEntry:
    base: str
    msg_type: Optional[str]
    processing: Dict[str, Any]
    qos: Optional[Dict[str, Any]]
    zen_qos: Optional[Dict[str, Any]]
    index: int
    # Declared behavioral contract (hz/latency_ms/loss_pct), surfaced into the
    # status pipeline spec so the live status overview classifies against it.
    expect: Optional[Dict[str, Any]] = None


@dataclass
class PluginBlock:
    """
    A semantic block of plugin.yaml parameters.

    Rendering rule:
    - Within a block: parameters are contiguous (no extra blank lines).
    - Between consecutive non-empty blocks: exactly one blank line.
    This makes formatting stable even when blocks are optional.
    """

    name: str
    items: List[Tuple[str, Any]]


@dataclass
class YamlBlockScalar:
    """
    Represent a YAML block scalar value (e.g. "|1") that must be rendered verbatim.
    """

    header: str
    content: str


# -----------------------------------------------------------------------------
# RMW schema
#
# Configs use a single `shared.rmw` block. Accepted forms:
#
#   shared.rmw: <short-alias>
#       Short-alias for {local: <x>, ota: <x>}. Must be cyclone | fastdds | zenoh
#       (zenoh_ros2dds cannot be used as a shortcut — it is OTA-only).
#
#   shared.rmw:
#     local: <side-spec>
#     ota:   <side-spec>
#
# Each <side-spec> is either:
#   - a string: cyclone | fastdds | zenoh_connect_endpoints | zenoh_ros2dds | <raw rmw string>
#     (zenoh_ros2dds is allowed for ota only; raw rmw strings are for local only)
#   - a tagged-union mapping with exactly one key (the RMW name):
#       {cyclone: {config?: <fname.xml>, easy_mode_ip_key?: <key>}}
#       {fastdds: {config?: <fname.xml>, easy_mode_ip_key?: <key>}}
#       {zenoh_connect_endpoints: {main_peer?: <peer_key>, main_port?: 7447}}
#       {zenoh_ros2dds: {transport?: udp|tcp, main_peer?: <peer_key>, main_port?: 7447}}
# -----------------------------------------------------------------------------

_DDS_SHORTS = {"cyclone", "fastdds"}
_ZENOH_CONNECT_ENDPOINTS = "zenoh_connect_endpoints"
_NATIVE_ZENOH_OTA_SHORTS = {"zenoh", _ZENOH_CONNECT_ENDPOINTS}
_OTA_SHORTS = {"cyclone", "fastdds", *_NATIVE_ZENOH_OTA_SHORTS, "zenoh_ros2dds"}
_LOCAL_SHORTS = {"cyclone", "fastdds", "zenoh"}
_SHORTCUT_ALLOWED = {"cyclone", "fastdds", "zenoh"}

_DDS_CFG_KEYS = {"config", "easy_mode_ip_key"}
_ZEN_OTA_CFG_KEYS = {"main_peer", "main_port"}
_ZEN_R2D_CFG_KEYS = {"transport", "main_peer", "main_port"}


def _is_native_zenoh_ota(impl: Optional[str]) -> bool:
    return impl in _NATIVE_ZENOH_OTA_SHORTS


def _rmw_ota_runtime_impl(impl: Optional[str]) -> Optional[str]:
    """Map user-facing OTA modes to the RMW implementation used by the base plugin."""
    if _is_native_zenoh_ota(impl):
        return "zenoh"
    return impl


@dataclass
class RmwSideSpec:
    """One side (local or ota) of the shared.rmw block."""

    impl: Optional[str] = None
    # DDS-specific (cyclone, fastdds)
    dds_config: Optional[str] = None
    dds_easy_mode_ip_key: Optional[str] = None
    # zenoh / zenoh_ros2dds
    zen_main_peer: Optional[str] = None
    # zenoh_ros2dds only
    zen_transport: Optional[str] = None
    zen_main_port: Optional[int] = None


@dataclass
class RmwSpec:
    local: RmwSideSpec = field(default_factory=RmwSideSpec)
    ota: RmwSideSpec = field(default_factory=RmwSideSpec)


def _parse_rmw_side(value: Any, ctx: str, *, is_local: bool) -> RmwSideSpec:
    if value is None:
        return RmwSideSpec()

    if isinstance(value, str):
        impl = value.strip()
        if not impl:
            return RmwSideSpec()
        _validate_rmw_impl(impl, ctx, is_local=is_local)
        return RmwSideSpec(impl=impl)

    if not isinstance(value, dict):
        raise RuntimeError(
            f"{ctx} must be a string or a single-key mapping {{<rmw_name>: {{...}}}}, got {type(value)}."
        )

    if len(value) != 1:
        raise RuntimeError(f"{ctx} mapping must have exactly one key (the RMW name); got {sorted(value.keys())}.")

    ((impl_raw, cfg_raw),) = value.items()
    if not isinstance(impl_raw, str) or not impl_raw.strip():
        raise RuntimeError(f"{ctx} key must be a non-empty string (the RMW name).")
    impl = impl_raw.strip()
    _validate_rmw_impl(impl, ctx, is_local=is_local)

    cfg: Dict[str, Any] = {}
    if cfg_raw is not None:
        if not isinstance(cfg_raw, dict):
            raise RuntimeError(f"{ctx}.{impl} must be a mapping (or null).")
        cfg = cfg_raw

    spec = RmwSideSpec(impl=impl)
    if impl in _DDS_SHORTS:
        extra = set(cfg.keys()) - _DDS_CFG_KEYS
        if extra:
            raise RuntimeError(
                f"{ctx}.{impl} contains unsupported keys {sorted(extra)}. Allowed: {sorted(_DDS_CFG_KEYS)}."
            )
        if cfg.get("config") is not None:
            if not isinstance(cfg["config"], str) or not cfg["config"].strip():
                raise RuntimeError(f"{ctx}.{impl}.config must be a non-empty string if provided.")
            spec.dds_config = cfg["config"].strip()
        if cfg.get("easy_mode_ip_key") is not None:
            if not isinstance(cfg["easy_mode_ip_key"], str) or not cfg["easy_mode_ip_key"].strip():
                raise RuntimeError(f"{ctx}.{impl}.easy_mode_ip_key must be a non-empty string if provided.")
            spec.dds_easy_mode_ip_key = cfg["easy_mode_ip_key"].strip()
    elif _is_native_zenoh_ota(impl):
        extra = set(cfg.keys()) - _ZEN_OTA_CFG_KEYS
        if extra:
            raise RuntimeError(
                f"{ctx}.{impl} contains unsupported keys {sorted(extra)}. Allowed: {sorted(_ZEN_OTA_CFG_KEYS)}."
            )
        if cfg.get("main_peer") is not None:
            if not isinstance(cfg["main_peer"], str) or not cfg["main_peer"].strip():
                raise RuntimeError(f"{ctx}.{impl}.main_peer must be a non-empty string if provided.")
            spec.zen_main_peer = cfg["main_peer"].strip()
        if cfg.get("main_port") is not None:
            mp = cfg["main_port"]
            if isinstance(mp, bool):
                raise RuntimeError(f"{ctx}.{impl}.main_port must be an integer.")
            try:
                spec.zen_main_port = int(mp)
            except Exception as e:
                raise RuntimeError(f"{ctx}.{impl}.main_port must be an integer.") from e
    elif impl == "zenoh_ros2dds":
        extra = set(cfg.keys()) - _ZEN_R2D_CFG_KEYS
        if extra:
            raise RuntimeError(
                f"{ctx}.zenoh_ros2dds contains unsupported keys {sorted(extra)}. Allowed: {sorted(_ZEN_R2D_CFG_KEYS)}."
            )
        if cfg.get("transport") is not None:
            if not isinstance(cfg["transport"], str) or not cfg["transport"].strip():
                raise RuntimeError(f"{ctx}.zenoh_ros2dds.transport must be a non-empty string.")
            spec.zen_transport = cfg["transport"].strip()
        if cfg.get("main_peer") is not None:
            if not isinstance(cfg["main_peer"], str) or not cfg["main_peer"].strip():
                raise RuntimeError(f"{ctx}.zenoh_ros2dds.main_peer must be a non-empty string.")
            spec.zen_main_peer = cfg["main_peer"].strip()
        if cfg.get("main_port") is not None:
            mp = cfg["main_port"]
            if isinstance(mp, bool):
                raise RuntimeError(f"{ctx}.zenoh_ros2dds.main_port must be an integer.")
            try:
                spec.zen_main_port = int(mp)
            except Exception as e:
                raise RuntimeError(f"{ctx}.zenoh_ros2dds.main_port must be an integer.") from e
    else:
        # Raw RMW string (local only) — no config keys allowed.
        if cfg:
            raise RuntimeError(
                f"{ctx}.{impl} does not accept any config keys (raw RMW string has no templated config)."
            )

    return spec


def _validate_rmw_impl(impl: str, ctx: str, *, is_local: bool) -> None:
    if is_local:
        if impl == "zenoh_ros2dds":
            raise RuntimeError(f"{ctx}: 'zenoh_ros2dds' is OTA-only and cannot be used as the local RMW.")
        if impl == _ZENOH_CONNECT_ENDPOINTS:
            raise RuntimeError(
                f"{ctx}: '{_ZENOH_CONNECT_ENDPOINTS}' is OTA-only; use local: zenoh for native rmw_zenoh_cpp."
            )
        # Everything else (including raw rmw strings) is permitted for local.
    else:
        if impl not in _OTA_SHORTS:
            raise RuntimeError(f"{ctx} must be one of {sorted(_OTA_SHORTS)}; got {impl!r}.")


def _parse_rmw_block(value: Any, peer_keys: List[str]) -> RmwSpec:
    """Parse `shared.rmw`. Returns an RmwSpec with both sides filled in (each
    side may have impl=None if absent).
    """
    if value is None:
        return RmwSpec()

    if isinstance(value, str):
        short = value.strip()
        if not short:
            return RmwSpec()
        if short not in _SHORTCUT_ALLOWED:
            raise RuntimeError(
                f"shared.rmw string shortcut must be one of {sorted(_SHORTCUT_ALLOWED)}; got {short!r}. "
                "Use the {local, ota} mapping form for other combinations."
            )
        side = RmwSideSpec(impl=short)
        return RmwSpec(local=RmwSideSpec(impl=short), ota=RmwSideSpec(impl=short))

    if not isinstance(value, dict):
        raise RuntimeError(f"shared.rmw must be a string or a mapping with {{local, ota}} keys; got {type(value)}.")

    extra = set(value.keys()) - {"local", "ota"}
    if extra:
        raise RuntimeError(f"shared.rmw contains unsupported keys {sorted(extra)}. Allowed: ['local', 'ota'].")

    spec = RmwSpec(
        local=_parse_rmw_side(value.get("local"), "shared.rmw.local", is_local=True),
        ota=_parse_rmw_side(value.get("ota"), "shared.rmw.ota", is_local=False),
    )

    # main_peer references must be known peers.
    for side, side_name in [(spec.local, "local"), (spec.ota, "ota")]:
        if side.zen_main_peer is not None and side.zen_main_peer not in peer_keys:
            raise RuntimeError(
                f"shared.rmw.{side_name}.{side.impl}.main_peer must be one of the declared peers "
                f"{peer_keys}, got {side.zen_main_peer!r}."
            )
    return spec


def _validate_rmw_spec_combination(spec: RmwSpec, *, ota_domain_id: Optional[int]) -> None:
    """Cross-side validation of an RmwSpec after parsing."""
    local_impl, ota_impl = spec.local.impl, spec.ota.impl
    if _is_native_zenoh_ota(ota_impl):
        # Native rmw_zenoh_cpp: local must also be zenoh (rmw_zenoh_cpp is not
        # interoperable with DDS-based RMWs).
        if local_impl not in (None, "zenoh", RMW_ALIASES["zenoh"]):
            raise RuntimeError(
                f"shared.rmw.ota={ota_impl} requires shared.rmw.local=zenoh "
                "(rmw_zenoh_cpp is not interoperable with DDS-based RMWs)."
            )
        if ota_domain_id is None:
            raise RuntimeError(
                f"shared.rmw.ota={ota_impl} requires shared.ota_domain_id to be set. "
                "Native rmw_zenoh_cpp's router forwards across ROS domains, so the OTA bridge "
                "processes must run on a dedicated ROS_DOMAIN_ID that differs from every peer's "
                "peer_settings.<peer>.domain_id (all three — both peer domains and ota_domain_id — "
                "must be set and distinct)."
            )
    elif ota_impl == "zenoh_ros2dds":
        # Bridge mode: local must be DDS (cyclone/fastdds). Raw RMW strings are
        # rejected here to keep the `RMW_IMPLEMENTATION` switch in the plugin base deterministic.
        if local_impl not in (None,) and local_impl not in _DDS_SHORTS:
            raise RuntimeError(
                f"shared.rmw.ota=zenoh_ros2dds requires shared.rmw.local in {sorted(_DDS_SHORTS)}; "
                f"got {local_impl!r}. zenoh_ros2dds bridges DDS ↔ zenoh, so local processes must use DDS."
            )
    # DDS-on-DDS / cross-DDS combinations are unconstrained.


def _parse_optional_domain_id(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise RuntimeError(f"{field_name} must be an integer if provided.")
    try:
        out = int(value)
    except Exception as e:
        raise RuntimeError(f"{field_name} must be an integer if provided.") from e
    if out < 0:
        raise RuntimeError(f"{field_name} must be >= 0, got {out}.")
    return out


# ---------------------------
# IO + normalization helpers
# ---------------------------


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_ensure_trailing_newline(content))


def _ensure_trailing_newline(s: str) -> str:
    return s if s.endswith("\n") else s + "\n"


def _normalize_topic_list_text(s: str) -> List[str]:
    # Ignore trailing whitespace + trailing newline differences, keep stable order
    out: List[str] = []
    for ln in s.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        out.append(ln)
    return out


def _safe_load_yaml_text(text: str, path_hint: str) -> Any:
    try:
        return yaml.safe_load(text) if text.strip() else None
    except Exception as e:
        raise RuntimeError(f"Failed to parse YAML for {path_hint}: {e}") from e


def _yaml_canonical_dump(obj: Any) -> str:
    # Stable, readable, diff-friendly
    dumped = yaml.safe_dump(
        obj,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
        width=1000,
    )
    return _ensure_trailing_newline(dumped)


def _split_csv(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    items = [x.strip() for x in value.split(",")]
    items = [x for x in items if x]
    return items


def _normalize_csv_set(value: Any) -> Any:
    items = _split_csv(value)
    if items is None:
        return value
    return ",".join(sorted(set(items)))


def _normalize_regex_rule_yaml_obj(obj: Any) -> Any:
    # Expected:
    # compression:
    #   - topic_regex: "^/foo$"
    # Keep order stable by sorting by topic_regex
    if not isinstance(obj, dict):
        return obj
    for key in ("compression", "decompression", "ota_wrapper", "ota_unwrapper"):
        if key in obj and isinstance(obj[key], list):
            entries = obj[key]
            norm_entries = []
            for it in entries:
                if isinstance(it, dict) and "topic_regex" in it:
                    norm_entries.append({"topic_regex": str(it["topic_regex"])})
                else:
                    norm_entries.append(it)
            norm_entries.sort(key=lambda d: d.get("topic_regex", "") if isinstance(d, dict) else str(d))
            obj = dict(obj)
            obj[key] = norm_entries
    return obj


def _normalize_plugin_yaml_obj(obj: Any) -> Any:
    # plugin.yaml structure:
    # parameters: { key: value, ... }
    # Normalize known CSV "set-like" parameters so ordering doesn't create diffs.
    if not isinstance(obj, dict):
        return obj
    if "parameters" not in obj or not isinstance(obj["parameters"], dict):
        return obj

    params = dict(obj["parameters"])

    csv_set_keys = {
        "rs_restamp_topics",
        "lat_topics",
        "trickle_topics",
        "fb_local_to_global_topics",
        "fb_global_to_local_topics",
        "fb_prefix_exclude_frames",
        "fb_tf_filter_frames",
        "fb_tf_throttle_links",
    }
    for k in csv_set_keys:
        if k in params:
            params[k] = _normalize_csv_set(params[k])

    # Also normalize any accidental whitespace in string scalar values
    for k, v in list(params.items()):
        if isinstance(v, str):
            params[k] = v.strip()

    out = dict(obj)
    out["parameters"] = params
    return out


def _normalize_session_spec_yaml_obj(obj: Any) -> Any:
    # session_specification.yaml:
    # session_plugins:
    #   - ./plugin.yaml
    #   - /ws/session/content/base/session_plugin_base.yaml
    if not isinstance(obj, dict):
        return obj
    if "session_plugins" in obj and isinstance(obj["session_plugins"], list):
        # Strip accidental whitespace around strings
        plugins = []
        for p in obj["session_plugins"]:
            plugins.append(p.strip() if isinstance(p, str) else p)
        out = dict(obj)
        out["session_plugins"] = plugins
        return out
    return obj


def _semantic_equal(path: str, existing_text: str, generated_text: str) -> Tuple[bool, str, str]:
    """
    Returns (equal, diff_from_text, diff_to_text)
    diff_* are canonicalized representations used for unified diff when unequal.
    """
    lower = path.lower()

    # Topic lists (.txt)
    if lower.endswith("_topics.txt") or lower.endswith("topics.txt"):
        a = _normalize_topic_list_text(existing_text)
        b = _normalize_topic_list_text(generated_text)
        if a == b:
            return True, "", ""
        # diff as normalized lines (one per line)
        return False, _ensure_trailing_newline("\n".join(a)), _ensure_trailing_newline("\n".join(b))

    # YAML files
    if lower.endswith(".yaml") or lower.endswith(".yml"):
        a_obj = _safe_load_yaml_text(existing_text, path)
        b_obj = _safe_load_yaml_text(generated_text, path)

        # File-specific normalization
        base = os.path.basename(lower)
        if base == "plugin.yaml":
            a_obj = _normalize_plugin_yaml_obj(a_obj)
            b_obj = _normalize_plugin_yaml_obj(b_obj)
        elif base in ("compression.yaml", "decompression.yaml", "ota_wrapper.yaml", "ota_unwrapper.yaml"):
            a_obj = _normalize_regex_rule_yaml_obj(a_obj)
            b_obj = _normalize_regex_rule_yaml_obj(b_obj)
        elif base == "session_specification.yaml":
            a_obj = _normalize_session_spec_yaml_obj(a_obj)
            b_obj = _normalize_session_spec_yaml_obj(b_obj)

        if a_obj == b_obj:
            return True, "", ""

        return False, _yaml_canonical_dump(a_obj), _yaml_canonical_dump(b_obj)

    # Fallback: text compare with trailing newline normalized
    a = _ensure_trailing_newline(existing_text)
    b = _ensure_trailing_newline(generated_text)
    return (a == b), a, b


def _print_unified_diff(path: str, from_text: str, to_text: str) -> None:
    diff = difflib.unified_diff(
        from_text.splitlines(True),
        to_text.splitlines(True),
        fromfile=f"{path} (existing, canonical)",
        tofile=f"{path} (generated, canonical)",
    )
    sys.stdout.writelines(diff)


def _write_generated_files(generated: List[Tuple[str, str]], force: bool, rewrite_formatting: bool) -> None:
    """
    Write generated files with semantic diff handling.
    - Formatting-only differences can be rewritten with --rewrite-formatting.
    - Semantic mismatches require --force.
    """
    semantic_mismatches = 0
    for path, gen_content in generated:
        if os.path.exists(path):
            existing_content = _read_text(path)

            equal, from_text, to_text = _semantic_equal(path, existing_content, gen_content)
            if equal:
                if rewrite_formatting and existing_content != gen_content:
                    _write_text(path, gen_content)
                    print(f"[REWRITE] {path}")
                else:
                    print(f"[OK]   {path}")
                continue

            semantic_mismatches += 1
            print(f"[DIFF] {path}")
            _print_unified_diff(path, from_text, to_text)

            if not force:
                print("\nRefusing to overwrite due to semantic mismatch.")
                print("Fix session-template/session-config, or rerun with --force.\n")
                continue

        if (not os.path.exists(path)) or force:
            _write_text(path, gen_content)
            print(f"[WRITE] {path}")

    if semantic_mismatches and not force:
        sys.exit(2)


# ---------------------------
# Session config logic
# ---------------------------


def _resolve_session_template_path(param_dir: str, template_path: str) -> str:
    """
    Resolve a session template path:
    - absolute path: used as-is (must exist)
    - relative path: resolved relative to the directory containing the session config input file
    """
    if not isinstance(template_path, str) or not template_path.strip():
        raise RuntimeError("load_template.filepath must be a non-empty string.")
    p = template_path.strip()

    # Support container-style logical paths when running on the host.
    # Many configs refer to templates as "/session/..." (inside container) which maps to "<repo>/session/..." on host.
    if p.startswith("/session/") or p == "/session":
        # find the nearest ".../session" ancestor of param_dir
        d = os.path.abspath(param_dir)
        session_root = None
        while True:
            if os.path.basename(d) == "session":
                session_root = d
                break
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        if session_root:
            rel = p[len("/session/") :] if p.startswith("/session/") else ""
            mapped = os.path.join(session_root, rel)
            if os.path.exists(mapped):
                return mapped

        # If we're actually in the container, the literal path may exist.
        if os.path.exists(p):
            return p

        raise FileNotFoundError(
            f"load_template points to missing file '{p}'. "
            f"Tried mapping it to the repo's session dir but couldn't find it. "
            f"Provide an existing path (absolute, /session/..., or relative to '{param_dir}')."
        )

    if p.startswith("/"):
        if not os.path.exists(p):
            raise FileNotFoundError(
                f"load_template points to missing file '{p}'. "
                f"Provide an existing absolute path, or a path relative to '{param_dir}'."
            )
        return p
    return os.path.abspath(os.path.join(param_dir, p))


def _parse_session_config_template_spec(param: Dict[str, Any], param_dir: str) -> Tuple[str, Dict[str, Any]]:
    """
    Parse a session *parametrization* into:
    - session_template_fs: resolved filesystem path to the session template YAML
    - provided_params: mapping of parameters explicitly set in the parametrization

    Supported formats:
      load_template:
        filepath: /path/to/template.yaml
        parameters:
          ... template input parameters ...
    """
    spec = (param or {}).get("load_template")
    if spec is None:
        raise RuntimeError(
            "Missing 'load_template'. "
            "If you want a self-contained definition (no template), omit load_template and provide "
            "a session-definition-like structure directly (peers/shared/topics/peer_settings)."
        )

    if not isinstance(spec, dict):
        raise RuntimeError("load_template must be a mapping with keys {filepath, parameters}. ")

    filepath = spec.get("filepath")
    session_template_fs = _resolve_session_template_path(param_dir, filepath)

    provided_params = dict(spec.get("parameters", {}) or {})
    if not isinstance(provided_params, dict):
        raise RuntimeError("load_template.parameters must be a mapping.")

    return session_template_fs, provided_params


def _build_vars_map_from_template(cfg_raw: Dict[str, Any], provided_params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build vars_map for template substitution from cfg_raw.input_parameters.

    Rules:
    - Extra parameters in session-config that are not in template input_parameters => error
    - Missing required template parameters (no default) => error
    - Optional template parameters (default provided) may be omitted; default is used
    """
    input_params = (cfg_raw or {}).get("input_parameters", {}) or {}
    if not isinstance(input_params, dict):
        raise RuntimeError("session-template input_parameters must be a mapping.")

    extra = sorted([k for k in (provided_params or {}).keys() if k not in input_params])
    if extra:
        raise RuntimeError(
            f"session-config provides unknown parameters not declared in the session template input_parameters: {extra}"
        )

    missing_required: List[str] = []
    vars_map: Dict[str, Any] = {}
    for k, spec in input_params.items():
        # spec should be a mapping like: {type: str, default: ..., description: ...}
        if k in (provided_params or {}):
            vars_map[k] = provided_params[k]
            continue
        if isinstance(spec, dict) and "default" in spec:
            vars_map[k] = spec.get("default")
            continue
        missing_required.append(k)

    if missing_required:
        raise RuntimeError(
            "Missing required session parameters (declared in template input_parameters without a default): "
            f"{sorted(missing_required)}"
        )

    return vars_map


def _render_session_dir(param_dir: str) -> str:
    """
    Render the session directory path that gets written into generated YAML.

    - In-container, session dirs live under /session/...
    - On-host, we still want generated files to reference /session/... so the same
      session directory works when mounted into the container.
    """
    d = os.path.abspath(param_dir)

    session_configs_root = os.environ.get("SESSION_CONFIGS_DIR")
    if session_configs_root:
        root = os.path.abspath(session_configs_root)
        rel = os.path.relpath(d, root)
        if rel == ".":
            return "/session/configs"
        if not rel.startswith(f"..{os.sep}") and rel != "..":
            return f"/session/configs/{rel}"

    session_root = None
    cur = d
    while True:
        if os.path.basename(cur) == "session":
            session_root = cur
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    if session_root:
        rel = os.path.relpath(d, session_root)
        rel = "" if rel == "." else rel
        return "/session" if not rel else f"/session/{rel}"

    # Support ros_communication_devcontainer workspace mapping:
    # On-host, session configs often live under "<repo>/ros_communication_devcontainer/ws/...".
    # In-container, that workspace is mounted at "/ws/...".
    cur = d
    ws_root = None
    while True:
        if os.path.basename(cur) == "ws" and os.path.basename(os.path.dirname(cur)) == "ros_communication_devcontainer":
            ws_root = cur
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    if ws_root:
        rel = os.path.relpath(d, ws_root)
        rel = "" if rel == "." else rel
        return "/ws" if not rel else f"/ws/{rel}"
    return d


def _substitute(obj: Any, vars_map: Dict[str, Any]) -> Any:
    if isinstance(obj, dict):
        # Substitute both keys and values.
        # This enables templates to parameterize peer names / topic direction keys via ${...}.
        out: Dict[Any, Any] = {}
        for k, v in obj.items():
            nk = k
            if isinstance(k, str):
                nk_sub = _substitute(k, vars_map)
                # YAML mapping keys must be hashable; we only support string keys here.
                nk = nk_sub if isinstance(nk_sub, str) else str(nk_sub)
                if not nk:
                    raise ValueError("Template substitution produced an empty mapping key, which is not allowed.")
            if nk in out:
                raise ValueError(f"Template substitution produced duplicate mapping key '{nk}'.")
            out[nk] = _substitute(v, vars_map)
        return out
    if isinstance(obj, list):
        return [_substitute(v, vars_map) for v in obj]
    if isinstance(obj, str):
        m = FULL_VAR_PATTERN.match(obj.strip())
        if m:
            key = m.group(1)
            if key not in vars_map:
                raise KeyError(f"Unknown variable '{key}' in '{obj}'")
            return vars_map[key]

        def repl(match: re.Match) -> str:
            key = match.group(1)
            if key not in vars_map:
                raise KeyError(f"Unknown variable '{key}' in '{obj}'")
            return str(vars_map[key])

        return VAR_PATTERN.sub(repl, obj)

    return obj


def _parse_condition(cond: Any, vars_map: Dict[str, Any]) -> bool:
    if cond is None:
        return False
    if isinstance(cond, bool):
        return cond
    if isinstance(cond, (int, float)):
        return bool(cond)
    if isinstance(cond, str):
        s = cond.strip()
        if s.lower() in ("true", "yes", "on"):
            return True
        if s.lower() in ("false", "no", "off", ""):
            return False
        if s in vars_map:
            return bool(vars_map[s])
        raise ValueError(f"Unsupported condition string '{cond}' (expected bool/true/false or a variable name)")
    raise TypeError(f"Unsupported condition type {type(cond)}")


def _load_yaml(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _assert_mapping(obj: Any, ctx: str) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        raise RuntimeError(f"{ctx} must be a mapping, got {type(obj)}")
    return obj


def _assert_allowed_keys(ctx: str, obj: Any, allowed: set) -> None:
    if obj is None:
        return
    if not isinstance(obj, dict):
        raise RuntimeError(f"{ctx} must be a mapping to validate keys, got {type(obj)}")
    extra = sorted([k for k in obj.keys() if k not in allowed])
    if extra:
        raise RuntimeError(f"Unsupported keys in {ctx}: {extra}. Allowed keys: {sorted(allowed)}")


def _validate_session_template_cfg(cfg: Dict[str, Any]) -> None:
    """
    Validate that the substituted session-template only contains supported keys and shapes.
    This is intentionally strict: unknown keys are treated as errors so configs don't silently do nothing.
    """
    cfg = _assert_mapping(cfg, "session-template root")

    _assert_allowed_keys(
        "session-template root",
        cfg,
        {
            "input_parameters",
            "peers",
            "shared",
            "peer_settings",
            "topics",
            # Test-tier capability markers (single_machine/multi_machine). Consumed
            # by the test suites (see docs/testing.md); ignored by generation.
            "test_tiers",
        },
    )

    peers = _assert_mapping(cfg.get("peers"), "peers")
    if not peers:
        raise RuntimeError("peers must be a non-empty mapping.")
    for peer_key, peer_obj in peers.items():
        if isinstance(peer_obj, dict) and "ip_key" in peer_obj:
            raise RuntimeError(
                f"peers.{peer_key}.ip_key is no longer supported. "
                f"Use peers.{peer_key}.address instead. "
                "For data_dict references, use address: 'data:<key>'."
            )
        _assert_allowed_keys(f"peers.{peer_key}", peer_obj, {"address", "com-name"})
        peer_obj = _assert_mapping(peer_obj, f"peers.{peer_key}")
        if "address" not in peer_obj:
            raise RuntimeError(f"peers.{peer_key}.address is required")
        if not isinstance(peer_obj["address"], str) or not peer_obj["address"].strip():
            raise RuntimeError(f"peers.{peer_key}.address must be a non-empty string")
        if (
            "com-name" in peer_obj
            and peer_obj["com-name"] is not None
            and not isinstance(peer_obj["com-name"], (str, int, float, bool))
        ):
            raise RuntimeError(f"peers.{peer_key}.com-name must be a scalar if provided")

    # shared is optional
    if "shared" in cfg and cfg["shared"] is not None:
        shared = _assert_mapping(cfg.get("shared"), "shared")
        # Reject legacy keys with a clear message so configs that predate the
        # shared.rmw / peer_settings.<peer>.domain_id schema fail loudly.
        _LEGACY_SHARED_KEYS = {
            "rmw_ota": "shared.rmw.ota",
            "rmw_local": "shared.rmw.local",
            "zenoh": "shared.rmw.ota.zenoh_ros2dds (tagged-union form)",
        }
        for legacy, replacement in _LEGACY_SHARED_KEYS.items():
            if legacy in shared:
                raise RuntimeError(f"shared.{legacy} is no longer supported. Use {replacement} instead.")

        _assert_allowed_keys(
            "shared",
            shared,
            {
                "use_topic_monitor",
                "use_status_overview",
                "use_heartbeat",
                "heartbeat",
                "use_in",
                "use_out",
                "rmw",
                "ota_domain_id",
                "local_domain_id",
                "heartbeat_position",
                "processing_suffixes",
                "compression",
                "qos",
            },
        )
        if (
            "use_status_overview" in shared
            and shared["use_status_overview"] is not None
            and not isinstance(shared["use_status_overview"], bool)
        ):
            raise RuntimeError("shared.use_status_overview must be boolean if provided.")
        if "use_in" in shared and shared["use_in"] is not None and not isinstance(shared["use_in"], bool):
            raise RuntimeError("shared.use_in must be boolean if provided.")
        if "use_out" in shared and shared["use_out"] is not None and not isinstance(shared["use_out"], bool):
            raise RuntimeError("shared.use_out must be boolean if provided.")
        if "rmw" in shared and shared["rmw"] is not None:
            # Full parse (exercises all sub-validators). The cross-side
            # combination check runs later in build time once peer settings
            # are known.
            _parse_rmw_block(shared.get("rmw"), list(peers.keys()))
        if "ota_domain_id" in shared and shared["ota_domain_id"] is not None:
            _parse_optional_domain_id(shared["ota_domain_id"], "shared.ota_domain_id")
        if "local_domain_id" in shared and shared["local_domain_id"] is not None:
            _parse_optional_domain_id(shared["local_domain_id"], "shared.local_domain_id")
        if "processing_suffixes" in shared and shared["processing_suffixes"] is not None:
            suffixes = _assert_mapping(shared.get("processing_suffixes"), "shared.processing_suffixes")
            _assert_allowed_keys(
                "shared.processing_suffixes",
                suffixes,
                {"restamped", "latched", "framebridge_global", "ota_stamped"},
            )
        if "compression" in shared and shared["compression"] is not None:
            comp = _assert_mapping(shared.get("compression"), "shared.compression")
            _assert_allowed_keys("shared.compression", comp, {"algorithm", "remove_algorithm_suffix_on_decompression"})
        if "qos" in shared and shared["qos"] is not None:
            qos = _assert_mapping(shared.get("qos"), "shared.qos")
            _assert_allowed_keys("shared.qos", qos, {"defaults", "for_role"})
        if "heartbeat" in shared and shared["heartbeat"] is not None:
            hb = _assert_mapping(shared.get("heartbeat"), "shared.heartbeat")
            _assert_allowed_keys("shared.heartbeat", hb, {"expect"})
            if "expect" in hb and hb["expect"] is not None and not isinstance(hb["expect"], dict):
                raise RuntimeError("shared.heartbeat.expect must be a mapping.")

    # peer_settings is optional
    if "peer_settings" in cfg and cfg["peer_settings"] is not None:
        peer_settings = _assert_mapping(cfg.get("peer_settings"), "peer_settings")
        peer_keys = list(peers.keys())
        extra_peers = sorted([k for k in peer_settings.keys() if k not in peer_keys])
        if extra_peers:
            raise RuntimeError(f"peer_settings contains unsupported peer keys {extra_peers}. Known peers: {peer_keys}")
        for p, ps in peer_settings.items():
            ps = _assert_mapping(ps, f"peer_settings.{p}")
            _assert_allowed_keys(
                f"peer_settings.{p}",
                ps,
                {"heartbeat_topic", "inbound", "outbound", "framebridge", "domain_id"},
            )
            if "domain_id" in ps and ps["domain_id"] is not None:
                _parse_optional_domain_id(ps["domain_id"], f"peer_settings.{p}.domain_id")
            if "framebridge" in ps and ps["framebridge"] is not None:
                fb_ps = _assert_mapping(ps["framebridge"], f"peer_settings.{p}.framebridge")
                _assert_allowed_keys(
                    f"peer_settings.{p}.framebridge",
                    fb_ps,
                    {"global_frame_prefix", "prefix_exclude_frames", "tf_filter_frames", "tf_throttle_links"},
                )

    # topics is optional
    if "topics" in cfg and cfg["topics"] is not None:
        topics = _assert_mapping(cfg.get("topics"), "topics")
        for dir_key, entries in topics.items():
            if not isinstance(dir_key, str):
                raise RuntimeError("topics keys must be strings like '<src>_to_<dst>'.")
            if not isinstance(entries, list):
                raise RuntimeError(f"topics.{dir_key} must be a list.")
            for i, item in enumerate(entries):
                if isinstance(item, str):
                    continue
                if isinstance(item, dict):
                    # `expect` declares the topic's intended delivered behavior
                    # (hz/latency/loss). It is consumed by the status overview and
                    # by `rosotacom test`; generation otherwise ignores it.
                    _assert_allowed_keys(
                        f"topics.{dir_key}[{i}]", item, {"topic", "type", "processing", "qos", "zen_qos", "expect"}
                    )
                    if "topic" not in item or not isinstance(item["topic"], str) or not item["topic"].strip():
                        raise RuntimeError(f"topics.{dir_key}[{i}].topic must be a non-empty string.")
                    if "type" in item and item["type"] is not None:
                        if not isinstance(item["type"], str) or not item["type"].strip():
                            raise RuntimeError(f"topics.{dir_key}[{i}].type must be a non-empty string if provided.")
                    if (
                        "processing" in item
                        and item["processing"] is not None
                        and not isinstance(item["processing"], dict)
                    ):
                        raise RuntimeError(f"topics.{dir_key}[{i}].processing must be a mapping.")
                    if "qos" in item and item["qos"] is not None and not isinstance(item["qos"], dict):
                        raise RuntimeError(f"topics.{dir_key}[{i}].qos must be a mapping.")
                    if "zen_qos" in item and item["zen_qos"] is not None and not isinstance(item["zen_qos"], dict):
                        raise RuntimeError(f"topics.{dir_key}[{i}].zen_qos must be a mapping.")
                    if "expect" in item and item["expect"] is not None and not isinstance(item["expect"], dict):
                        raise RuntimeError(f"topics.{dir_key}[{i}].expect must be a mapping.")
                    continue
                raise RuntimeError(f"Unsupported topic entry at topics.{dir_key}[{i}]: {type(item)}")


def _topic_entries(cfg: Dict[str, Any], direction: str) -> List[TopicEntry]:
    topics = (cfg.get("topics") or {}) if isinstance(cfg, dict) else {}
    if not isinstance(topics, dict):
        raise RuntimeError(f"topics must be a mapping if provided, got {type(topics)}")
    lst = topics.get(direction, [])
    if lst is None:
        lst = []
    if not isinstance(lst, list):
        raise RuntimeError(f"topics.{direction} must be a list, got {type(lst)}")
    out: List[TopicEntry] = []
    for i, item in enumerate(lst):
        if isinstance(item, str):
            out.append(TopicEntry(base=item, msg_type=None, processing={}, qos=None, zen_qos=None, index=i))
        elif isinstance(item, dict):
            out.append(
                TopicEntry(
                    base=item["topic"],
                    msg_type=(str(item.get("type")).strip() if item.get("type") is not None else None),
                    processing=item.get("processing", {}) or {},
                    qos=item.get("qos"),
                    zen_qos=item.get("zen_qos"),
                    index=i,
                    expect=item.get("expect"),
                )
            )
        else:
            raise TypeError(f"Unsupported topic entry at topics.{direction}[{i}]: {type(item)}")
    return out


def _dedup_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _format_yaml_kv(indent: int, k: str, v: Any) -> str:
    pad = " " * indent
    if isinstance(v, YamlBlockScalar):
        # Render a block scalar. We intentionally do not serialize via yaml.safe_dump
        # because plugin.yaml formatting is stable + hand-crafted in this generator.
        out = f"{pad}{k}: {v.header}\n"
        # Keep content readable and stable: indent by 9 spaces (matches existing examples)
        content_indent = 9
        for ln in (v.content or "").splitlines():
            out += (" " * content_indent) + ln + "\n"
        return out
    if isinstance(v, bool):
        vv = "true" if v else "false"
        return f"{pad}{k}: {vv}\n"
    if isinstance(v, (int, float)):
        return f"{pad}{k}: {v}\n"
    if v is None:
        return f"{pad}{k}:\n"
    return f"{pad}{k}: {v}\n"


def _render_plugin_yaml(blocks: List[PluginBlock]) -> str:
    out = "parameters:\n"
    first = True
    for blk in blocks:
        if not blk.items:
            continue
        if not first:
            out += "\n"
        first = False
        for k, v in blk.items:
            out += _format_yaml_kv(2, k, v)
    return _ensure_trailing_newline(out)


def _render_session_spec(base_plugin_path: str) -> str:
    return _ensure_trailing_newline(f"session_plugins:\n  - ./plugin.yaml\n  - {base_plugin_path}\n")


def _render_regex_list(key: str, topics: List[str]) -> str:
    lines = [f"{key}:\n"]
    for t in topics:
        lines.append(f'  - topic_regex: "^{t}$"\n')
    return _ensure_trailing_newline("".join(lines))


def _render_qos_yaml(shared_qos: Dict[str, Any], qos_overrides: Dict[str, Dict[str, Any]]) -> str:
    """
    Render QoS YAML in the schema consumed by `com_py/qos.py`:
      default: <fields>
      role_defaults: <role -> fields>
      topics:
        /topic:
          <topic-wide fields>        # apply to ALL roles
          roles:
            <role>:
              <fields>

    Backwards compatible input for topic overrides:
      - topic_cfg.for_role  (legacy template naming) is mapped to topic_cfg.roles.
    """

    default = shared_qos.get("defaults", {}) or {}
    role_defaults = shared_qos.get("for_role", {}) or {}

    # Normalize topic override shape:
    # - Accept both {roles: {...}} and {for_role: {...}} (template-facing)
    # - Keep any additional topic-level fields alongside roles.
    topics_out: Dict[str, Any] = {}
    for topic, cfg in (qos_overrides or {}).items():
        if not isinstance(cfg, dict):
            raise RuntimeError(f"qos override for topic '{topic}' must be a mapping, got {type(cfg)}")
        cfg_out: Dict[str, Any] = {}
        # copy all non-role keys
        for k, v in cfg.items():
            if k in ("for_role", "roles"):
                continue
            cfg_out[k] = v

        roles = None
        if "roles" in cfg and cfg.get("roles") is not None:
            roles = cfg.get("roles")
        elif "for_role" in cfg and cfg.get("for_role") is not None:
            roles = cfg.get("for_role")

        if roles is not None:
            if not isinstance(roles, dict):
                raise RuntimeError(f"qos override roles for topic '{topic}' must be a mapping, got {type(roles)}")
            cfg_out["roles"] = roles

        topics_out[topic] = cfg_out

    qos_obj = {
        "default": default,
        "role_defaults": role_defaults,
        "topics": topics_out,
    }
    # Use canonical YAML dump so nested dicts are valid YAML (not Python dict repr).
    return _yaml_canonical_dump(qos_obj)


def _final_topic_type(entry: TopicEntry, pipe: Dict[str, Any]) -> str:
    transport = pipe.get("transport")
    if transport is not None:
        assert isinstance(transport, TransportSpec)
        if transport.type not in TRANSPORT_OUTPUT_TYPES:
            raise RuntimeError(
                f"Unsupported transport type '{transport.type}' for topic '{entry.base}'. "
                f"Known output type mappings: {sorted(TRANSPORT_OUTPUT_TYPES)}"
            )
        return TRANSPORT_OUTPUT_TYPES[transport.type]

    if pipe.get("ota_wrap"):
        return OTA_STAMPED_MSG_TYPE

    if pipe.get("compress"):
        return COMPRESSED_MSG_TYPE

    msg_type = str(entry.msg_type or "").strip()
    if not msg_type:
        raise RuntimeError(
            f"Topic '{entry.base}' requires a 'type' when peer_settings.<peer>.domain_id/shared.ota_domain_id are used."
        )
    return msg_type


def _heartbeat_monitor_overrides(heartbeat_expect: Optional[Dict[str, Any]]) -> List[Tuple[str, Any]]:
    """Override the heartbeat_in_monitor status thresholds from a declared
    `shared.heartbeat.expect`. Only latency and loss map cleanly to the monitor's
    threshold model; the expected rate is the publish rate (heartbeat_out_hz) and
    `expect.hz` (min/max) is enforced separately by the status overview."""
    expect = heartbeat_expect if isinstance(heartbeat_expect, dict) else {}
    overrides: List[Tuple[str, Any]] = []
    latency = expect.get("latency_ms")
    if isinstance(latency, dict) and "max" in latency:
        overrides.append(("heartbeat_delay_bad_ms", float(latency["max"])))
    loss = expect.get("loss_pct")
    if isinstance(loss, dict) and "max" in loss:
        overrides.append(("heartbeat_loss3_bad_pct", float(loss["max"])))
    return overrides


def _build_status_pipeline_spec(
    *,
    local: str,
    remote: str,
    peer_name: Dict[str, str],
    out_entries: List["TopicEntry"],
    out_pipes: List[Dict[str, Any]],
    in_entries: List["TopicEntry"],
    in_pipes: List[Dict[str, Any]],
    use_target_prefix: bool,
    remote_uses_target_prefix: bool,
    native_have_source_prefix: bool,
    inbound_keep_source_prefix: bool,
    local_domain_id: Optional[int],
    ota_domain_id: Optional[int],
    uses_domain_bridge: bool,
    hb_topic: Dict[str, str],
    use_heartbeat: bool,
    heartbeat_expect: Optional[Dict[str, Any]],
    out_enabled: bool,
    in_enabled: bool,
    final_topic_type,
) -> str:
    """
    Build the per-peer pipeline_spec.yaml consumed by the status_overview node.

    For each configured topic it enumerates the ordered, locally-observable
    pipeline stages (Phase 1): outbound stages reach up to the /ota topic the
    local peer publishes ("sent"); inbound stages cover the /ota topic the local
    peer receives through to the republished application topic.

    Stage topic names mirror the runtime resolution in topic_resolution.py so the
    node can subscribe directly without recomputing names.
    """
    local_name = peer_name[local]
    remote_name = peer_name[remote]

    def _safe_type(entry: "TopicEntry", pipe: Dict[str, Any], fallback_base_type: bool = False) -> Optional[str]:
        if fallback_base_type:
            base_type = str(entry.msg_type or "").strip()
            if base_type:
                return base_type
        try:
            return final_topic_type(entry, pipe)
        except Exception:
            t = str(entry.msg_type or "").strip()
            return t or None

    def _expected_hz(pipe: Dict[str, Any]) -> Optional[float]:
        if pipe.get("throttle") is not None:
            return float(pipe["throttle"])
        if pipe.get("trickle_hz") is not None:
            return float(pipe["trickle_hz"])
        return None

    def _relay_in_local_topic(topic: str) -> str:
        if inbound_keep_source_prefix:
            return f"/{remote_name}{topic}"
        return topic

    topics: List[Dict[str, Any]] = []

    # --- Outbound: local peer is the source ---
    if out_enabled:
        outbound_items: List[Tuple["TopicEntry", Dict[str, Any]]] = []
        hb_local = hb_topic.get(local, "")
        for e, p in zip(out_entries, out_pipes):
            if use_heartbeat and hb_local and e.base == hb_local:
                continue
            outbound_items.append((e, p))
        if use_heartbeat and hb_local:
            hb_entry = TopicEntry(
                base=hb_local, msg_type=HEARTBEAT_MSG_TYPE, processing={}, qos=None, zen_qos=None, index=-1,
                expect=heartbeat_expect,
            )
            hb_pipe = {"final": hb_local}
            outbound_items.insert(0, (hb_entry, hb_pipe))

        for e, p in outbound_items:
            base = e.base
            final = p.get("final", base)
            forward_final = f"/to_{remote_name}{final}" if use_target_prefix else final
            forward_ns = forward_final.lstrip("/")
            app_native = f"/{local_name}{base}" if native_have_source_prefix else base
            app_processed = f"/{local_name}{final}" if native_have_source_prefix else final

            stages: List[Dict[str, Any]] = [
                {"stage": "native", "topic": app_native, "domain": "local", "produced_by": "application"},
            ]
            if final != base:
                stages.append(
                    {"stage": "processed", "topic": app_processed, "domain": "local", "produced_by": "preprocessing"}
                )
            stages.append(
                {
                    "stage": "com_out",
                    "topic": f"/com/out/{local_name}/{forward_ns}",
                    "domain": "local",
                    "produced_by": "relay_out",
                }
            )
            stages.append(
                {
                    "stage": "ota_sent",
                    "topic": f"/ota/{local_name}/{forward_ns}",
                    "domain": "ota",
                    "produced_by": "bridge_out",
                }
            )

            topics.append(
                {
                    "base": base,
                    "direction": "outbound",
                    "source": local_name,
                    "target": remote_name,
                    "type": _safe_type(e, p),
                    "expected_hz": _expected_hz(p),
                    "expect": e.expect,
                    "stages": stages,
                }
            )

    # --- Inbound: remote peer is the source, local peer is the target ---
    if in_enabled:
        inbound_items: List[Tuple["TopicEntry", Dict[str, Any]]] = []
        hb_remote = hb_topic.get(remote, "")
        for e, p in zip(in_entries, in_pipes):
            if use_heartbeat and hb_remote and e.base == hb_remote:
                continue
            inbound_items.append((e, p))
        if use_heartbeat and hb_remote:
            hb_entry = TopicEntry(
                base=hb_remote, msg_type=HEARTBEAT_MSG_TYPE, processing={}, qos=None, zen_qos=None, index=-1,
                expect=heartbeat_expect,
            )
            hb_pipe = {"final": hb_remote}
            inbound_items.insert(0, (hb_entry, hb_pipe))

        for e, p in inbound_items:
            base = e.base
            final = p.get("final", base)
            forward_final = f"/to_{local_name}{final}" if remote_uses_target_prefix else final
            forward_ns = forward_final.lstrip("/")
            app_in = _relay_in_local_topic(final)

            stages = [
                {
                    "stage": "ota_recv",
                    "topic": f"/ota/{remote_name}/{forward_ns}",
                    "domain": "ota",
                    "produced_by": "transport",
                },
                {
                    "stage": "com_in",
                    "topic": f"/com/in/{remote_name}/{forward_ns}",
                    "domain": "local",
                    "produced_by": "bridge_in",
                },
                {"stage": "app_in", "topic": app_in, "domain": "local", "produced_by": "relay_in"},
            ]
            if final != base:
                stages.append(
                    {
                        "stage": "native_in",
                        "topic": _relay_in_local_topic(base),
                        "domain": "local",
                        "produced_by": "postprocessing",
                    }
                )

            topics.append(
                {
                    "base": base,
                    "direction": "inbound",
                    "source": remote_name,
                    "target": local_name,
                    "type": _safe_type(e, p),
                    "expected_hz": _expected_hz(p),
                    "expect": e.expect,
                    "stages": stages,
                }
            )

    spec: Dict[str, Any] = {
        "schema_version": 1,
        "peer": local_name,
        "remote": remote_name,
        "local_domain_id": local_domain_id,
        "ota_domain_id": ota_domain_id,
        "uses_domain_bridge": uses_domain_bridge,
        "topics": topics,
    }
    return _yaml_canonical_dump(spec)


def _dedup_topic_type_items(items: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    seen: Dict[str, str] = {}
    out: List[Tuple[str, str]] = []
    for topic_name, msg_type in items:
        existing = seen.get(topic_name)
        if existing is not None:
            if existing != msg_type:
                raise RuntimeError(
                    f"Conflicting message types for generated topic '{topic_name}': '{existing}' vs '{msg_type}'."
                )
            continue
        seen[topic_name] = msg_type
        out.append((topic_name, msg_type))
    return out


def _render_domain_bridge_yaml(name: str, topics: Dict[str, Dict[str, Any]]) -> str:
    cfg: Dict[str, Any] = {"name": name, "topics": topics}
    return _yaml_canonical_dump(cfg)


def _compute_pipeline(
    entry: TopicEntry,
    vars_map: Dict[str, Any],
    restamped_suffix: str,
    latched_suffix: str,
    globalframe_suffix: str,
    comp_alg_suffix: str,
    ota_suffix: str,
) -> Dict[str, Any]:
    proc = entry.processing or {}

    known_keys = {
        "restamp_if",
        "latch",
        "trickle_hz",
        "drop",
        "framebridge",
        "normalize_on_target",
        "compress",
        "use_ota_wrapper",
        "throttle_hz",
        "pixel_cap_preset",
        "transport",
    }
    unknown = set(proc.keys()) - known_keys
    if unknown:
        raise ValueError(f"Unknown processing keys for topic '{entry.base}': {sorted(unknown)}")

    restamp = _parse_condition(proc.get("restamp_if"), vars_map) if "restamp_if" in proc else False

    framebridge = proc.get("framebridge")
    normalize = bool(proc.get("normalize_on_target")) if "normalize_on_target" in proc else False
    compress = bool(proc.get("compress")) if "compress" in proc else False
    ota_wrap = bool(proc.get("use_ota_wrapper")) if "use_ota_wrapper" in proc else False

    throttle = proc.get("throttle_hz")
    if throttle is not None:
        throttle = int(throttle)
        if throttle <= 0:
            raise ValueError(f"throttle_hz must be > 0 for topic '{entry.base}'")

    pixel = proc.get("pixel_cap_preset")

    transport: Optional[TransportSpec] = None
    if proc.get("transport") is not None:
        t = proc["transport"]
        if not isinstance(t, dict) or "type" not in t:
            raise ValueError(f"transport must be dict with 'type' for topic '{entry.base}'")
        ttype = t["type"]
        local_republish = bool(t.get("local_republish", False))
        params = {k: v for k, v in t.items() if k not in ("type", "local_republish")}
        transport = TransportSpec(type=ttype, params=params, local_republish=local_republish)

    topic = entry.base

    restamp_in = topic
    if restamp:
        topic = topic + restamped_suffix
    restamp_out = topic

    latch = bool(proc.get("latch")) if "latch" in proc else False
    lat_in = None
    if latch:
        lat_in = topic
        topic = topic + latched_suffix

    trickle_hz = proc.get("trickle_hz")
    if trickle_hz is not None:
        trickle_hz = float(trickle_hz)
        if trickle_hz <= 0:
            raise ValueError(f"trickle_hz must be > 0 for topic '{entry.base}'")

    drop_count = None
    window_size = None
    drp_in = None
    drp_out = None
    if proc.get("drop") is not None:
        drop_cfg = proc["drop"]
        if not isinstance(drop_cfg, dict):
            raise ValueError(f"drop must be a dict with 'drop_count' and 'window_size' for topic '{entry.base}'")
        drop_count = drop_cfg.get("drop_count")
        window_size = drop_cfg.get("window_size")
        if drop_count is None or window_size is None:
            raise ValueError(f"drop.drop_count and drop.window_size are required for topic '{entry.base}'")
        drop_count = int(drop_count)
        window_size = int(window_size)
        if drop_count < 0 or window_size <= 0:
            raise ValueError(f"drop.drop_count must be >= 0 and drop.window_size must be > 0 for topic '{entry.base}'")
        if drop_count >= window_size:
            raise ValueError(f"drop.drop_count must be < drop.window_size for topic '{entry.base}'")
        drp_in = topic
        drp_out = topic + f"/drop{drop_count}of{window_size}"
        topic = drp_out

    thr_in = None
    thr_out = None
    if throttle is not None:
        thr_in = topic
        thr_out = topic + f"/max{throttle}hz"
        topic = thr_out

    ipx_in = None
    ipx_out = None
    if pixel is not None:
        ipx_in = topic
        ipx_out = topic + f"/{pixel}"
        topic = ipx_out

    fb_l2g_in = None
    fb_g2l_base = None
    if framebridge:
        if framebridge not in ("local_to_global", "global_to_local"):
            raise ValueError(f"Unknown framebridge '{framebridge}' for topic '{entry.base}'")

        if framebridge == "local_to_global":
            fb_l2g_in = topic
            topic = topic + globalframe_suffix
        else:
            fb_g2l_base = entry.base
            topic = entry.base + globalframe_suffix

    comp_in = None
    if compress:
        comp_in = topic
        topic = topic + comp_alg_suffix

    ota_in = None
    if ota_wrap:
        ota_in = topic
        topic = topic + ota_suffix

    it_in = None
    irt_in = None
    if transport:
        it_in = topic
        topic = topic + f"/{transport.type}"
        if transport.local_republish:
            irt_in = topic

    return {
        "final": topic,
        "restamp": restamp,
        "restamp_in": restamp_in,
        "restamp_out": restamp_out,
        "latch": latch,
        "lat_in": lat_in,
        "trickle_hz": trickle_hz,
        "drop_count": drop_count,
        "window_size": window_size,
        "drp_in": drp_in,
        "drp_out": drp_out,
        "framebridge": framebridge,
        "fb_l2g_in": fb_l2g_in,
        "fb_g2l_base": fb_g2l_base,
        "normalize": normalize,
        "compress": compress,
        "comp_in": comp_in,
        "ota_wrap": ota_wrap,
        "ota_in": ota_in,
        "throttle": throttle,
        "thr_in": thr_in,
        "thr_out": thr_out,
        "pixel": pixel,
        "ipx_in": ipx_in,
        "ipx_out": ipx_out,
        "transport": transport,
        "it_in": it_in,
        "irt_in": irt_in,
    }


# ---------------------------
# Main
# ---------------------------


def func(
    session_config_yaml: str = "",
    force: bool = False,
    rewrite_formatting: bool = False,
    base_plugin_path: str = BASE_PLUGIN_PATH_DEFAULT,
    session_config_obj: Optional[Dict[str, Any]] = None,
    output_dir: Optional[str] = None,
    write_resolved_definition: Optional[bool] = None,
) -> None:
    session_config_yaml = os.path.abspath(session_config_yaml) if session_config_yaml else ""
    if output_dir:
        param_dir = os.path.abspath(output_dir)
    elif session_config_yaml:
        param_dir = os.path.dirname(session_config_yaml)
    else:
        raise RuntimeError("Either session_config_yaml or output_dir must be provided.")
    param_name = os.path.basename(param_dir)

    if session_config_obj is not None:
        if not isinstance(session_config_obj, dict):
            raise RuntimeError(f"session_config_obj must be a mapping, got {type(session_config_obj)}")
        vars_map = {}
        cfg = dict(session_config_obj)
        template_mode = False
    else:
        param = _load_yaml(session_config_yaml)

        if not isinstance(param, dict):
            raise RuntimeError(
                f"Session config input YAML must be a mapping, got {type(param)} for '{session_config_yaml}'"
            )

        # session-config supports template-driven configs, but the resulting cfg is validated+handled
        # with optional blocks: peers is required; shared/topics/peer_settings are optional.
        template_mode = "load_template" in param
        if template_mode:
            session_template_fs, provided_params = _parse_session_config_template_spec(param, param_dir)
            cfg_raw = _load_yaml(session_template_fs)
            vars_map = _build_vars_map_from_template(cfg_raw, provided_params)
            cfg = _substitute(cfg_raw, vars_map)
        else:
            vars_map = {}
            cfg = dict(param)

    _validate_session_template_cfg(cfg)

    # If the user provided a parametrization (template + parameters), write the resolved, self-contained
    # session definition next to the generated files so it can be inspected/versioned.
    session_definition_yaml: Optional[str] = None
    if write_resolved_definition is None:
        write_resolved_definition = template_mode
    if write_resolved_definition:
        cfg_definition = dict(cfg)
        cfg_definition.pop("input_parameters", None)
        session_definition_yaml = _yaml_canonical_dump(cfg_definition)

    # ---------------------------
    # Peer + direction discovery (NO role hardcoding)
    # ---------------------------

    if "peers" not in cfg or not isinstance(cfg["peers"], dict) or not cfg["peers"]:
        raise RuntimeError("session-config must define a mapping 'peers: { <peer_key>: {address: ...} }'.")

    peer_keys = list(cfg["peers"].keys())
    if len(peer_keys) != 2:
        raise RuntimeError(
            "This generator currently supports exactly 2 peers (because session_plugin_base.yaml is 1-remote). "
            f"Got peers={peer_keys}"
        )

    def _peer_address(peer_key: str) -> str:
        try:
            return str(cfg["peers"][peer_key]["address"]).strip()
        except Exception as e:
            raise RuntimeError(f"session-template must define peers.{peer_key}.address") from e

    def _peer_com_name(peer_key: str) -> str:
        """
        Resolve a peer's com-name for plugin.yaml:
        - peers.<peer>.com-name if provided and non-empty
        - else the peer key itself
        """
        v = None
        try:
            v = (cfg["peers"][peer_key] or {}).get("com-name")
        except Exception:
            v = None
        if v is None:
            return peer_key
        if isinstance(v, str):
            s = v.strip()
            return s if s else peer_key
        return str(v) if v else peer_key

    peer_ip = {p: _peer_address(p) for p in peer_keys}
    peer_name = {p: _peer_com_name(p) for p in peer_keys}

    # Optional blocks (may be absent in both template-driven and direct configs)
    shared = cfg.get("shared", {}) or {}
    if not isinstance(shared, dict):
        raise RuntimeError(f"shared must be a mapping if provided, got {type(shared)}")
    peer_settings_all = cfg.get("peer_settings", {}) or {}
    if not isinstance(peer_settings_all, dict):
        raise RuntimeError(f"peer_settings must be a mapping if provided, got {type(peer_settings_all)}")
    topics_cfg = cfg.get("topics", {}) or {}
    if not isinstance(topics_cfg, dict):
        raise RuntimeError(f"topics must be a mapping if provided, got {type(topics_cfg)}")

    # Optional shared toggles
    use_topic_monitor = bool(shared.get("use_topic_monitor", False))
    use_status_overview = bool(shared.get("use_status_overview", False))
    use_heartbeat = bool(shared.get("use_heartbeat", False))
    shared_use_in = shared.get("use_in", None)
    shared_use_out = shared.get("use_out", None)

    # RMW spec: the unified `shared.rmw` block. Either a string shortcut (e.g.
    # `rmw: zenoh`) or a mapping `{local, ota}` per side. See `_parse_rmw_block`
    # for the full schema.
    rmw_spec = _parse_rmw_block(shared.get("rmw"), peer_keys)

    ota_domain_id = _parse_optional_domain_id(shared.get("ota_domain_id"), "shared.ota_domain_id")
    # Per-peer local domain id. Resolution order:
    #   1. peer_settings.<peer>.domain_id (per-peer explicit)
    #   2. shared.local_domain_id (shortcut applied to every peer that did not set its own)
    # If both are set for the same peer and disagree, that's an error — either the
    # user meant the shortcut (drop the per-peer value) or they meant to override it
    # (drop the shared one), but silently letting them diverge would hide mistakes.
    shared_local_domain_id = _parse_optional_domain_id(shared.get("local_domain_id"), "shared.local_domain_id")
    peer_local_domain_id: Dict[str, Optional[int]] = {}
    for p in peer_keys:
        per_peer = _parse_optional_domain_id(
            (peer_settings_all.get(p) or {}).get("domain_id"),
            f"peer_settings.{p}.domain_id",
        )
        if per_peer is not None and shared_local_domain_id is not None and per_peer != shared_local_domain_id:
            raise RuntimeError(
                f"peer_settings.{p}.domain_id ({per_peer}) conflicts with "
                f"shared.local_domain_id ({shared_local_domain_id}). "
                f"Set only one, or make them agree."
            )
        peer_local_domain_id[p] = per_peer if per_peer is not None else shared_local_domain_id

    _validate_rmw_spec_combination(rmw_spec, ota_domain_id=ota_domain_id)

    # Native rmw_zenoh_cpp: require distinct peer domain_ids, a distinct
    # ota_domain_id, and distinct IP keys. rmw_zenoh_cpp's router forwards
    # across ROS domains, so the only way to keep each peer's local graph
    # isolated from the other peer's and from the OTA bridge traffic is to
    # pin every logical graph to its own domain.
    if _is_native_zenoh_ota(rmw_spec.ota.impl):
        ota_label = rmw_spec.ota.impl
        missing = [p for p in peer_keys if peer_local_domain_id[p] is None]
        if missing:
            raise RuntimeError(
                f"shared.rmw.ota={ota_label} requires peer_settings.<peer>.domain_id for every peer; "
                f"missing: {missing}."
            )
        # ota_domain_id presence is already enforced by _validate_rmw_spec_combination.
        assert ota_domain_id is not None
        seen_ids: Dict[int, str] = {}
        for p in peer_keys:
            d = peer_local_domain_id[p]
            assert d is not None
            if d == ota_domain_id:
                raise RuntimeError(
                    f"shared.rmw.ota={ota_label} requires shared.ota_domain_id ({ota_domain_id}) to differ "
                    f"from every peer_settings.<peer>.domain_id; peer '{p}' also uses {d}."
                )
            if d in seen_ids:
                raise RuntimeError(
                    f"shared.rmw.ota={ota_label} requires distinct peer_settings.<peer>.domain_id across peers; "
                    f"peers '{seen_ids[d]}' and '{p}' both use {d}."
                )
            seen_ids[d] = p
        seen_ips: Dict[str, str] = {}
        for p in peer_keys:
            ipk = peer_ip[p]
            if ipk in seen_ips:
                raise RuntimeError(
                    f"shared.rmw.ota={ota_label} requires distinct peers.<peer>.address across peers; "
                    f"peers '{seen_ips[ipk]}' and '{p}' both use {ipk!r}."
                )
            seen_ips[ipk] = p

    def _use_domain_bridge(peer: str) -> bool:
        d = peer_local_domain_id[peer]
        return d is not None and ota_domain_id is not None and d != ota_domain_id

    # Global "any peer uses the domain bridge" flag: domain-bridge-driven
    # topic lists (with types) are assembled once and shared across peers.
    use_domain_bridge = any(_use_domain_bridge(p) for p in peer_keys)

    # session_dir is the on-disk directory containing the session config input YAML
    # (session-definition.yaml / session-parametrization.yaml)
    session_dir = _render_session_dir(param_dir)

    def _build_local_rmw_items(peer: str) -> List[Tuple[str, Any]]:
        """Plugin.yaml block for local-side RMW (rmw_local + optional DDS config)."""
        side = rmw_spec.local
        # Effective impl: the stock `domain_bridge` binary needs a
        # deterministic DDS RMW. Fall back to cyclone when split-domain
        # bridging is in play on this peer but the user didn't pin a local RMW.
        effective_impl = side.impl
        if effective_impl is None and _use_domain_bridge(peer):
            effective_impl = "cyclone"
        items: List[Tuple[str, Any]] = []
        if effective_impl is not None:
            items.append(("rmw_local", effective_impl))
        if side.dds_config:
            items.append(("local_config_template", side.dds_config))
            items.append(("local_config_file", "${peer_dir}/local_dds.xml"))
            if side.impl == "fastdds" and side.dds_config == "fastdds_easy_mode.xml":
                items.append(
                    (
                        "local_easy_mode_ip_key",
                        side.dds_easy_mode_ip_key or peer_ip[peer_keys[0]],
                    )
                )
        return items

    def _build_ota_rmw_items() -> List[Tuple[str, Any]]:
        """Plugin.yaml block for OTA-side RMW + derived use_zenoh_* flags."""
        ota = rmw_spec.ota
        local = rmw_spec.local
        items: List[Tuple[str, Any]] = []
        runtime_impl = _rmw_ota_runtime_impl(ota.impl)
        if runtime_impl is not None:
            items.append(("rmw_ota", runtime_impl))
        # Native rmw_zenoh_cpp router (ZEN window): enabled if either side uses native zenoh.
        items.append(("use_zenoh_rmw", _is_native_zenoh_ota(ota.impl) or local.impl == "zenoh"))
        # zenoh_bridge_ros2dds router (Z2D window): enabled for OTA only.
        items.append(("use_zenoh_ros2dds", ota.impl == "zenoh_ros2dds"))
        if ota.dds_config:
            items.append(("ota_config_template", ota.dds_config))
            items.append(("ota_config_file", "${peer_dir}/ota_dds.xml"))
            if ota.impl == "fastdds" and ota.dds_config == "fastdds_easy_mode.xml":
                items.append(
                    (
                        "ota_easy_mode_ip_key",
                        ota.dds_easy_mode_ip_key or peer_ip[peer_keys[0]],
                    )
                )
        return items

    def _build_zenoh_block(
        local_peer: str,
        remote_peer: str,
        out_entries_pipes: Optional[List[Tuple[TopicEntry, Dict[str, Any]]]] = None,
    ) -> Optional[PluginBlock]:
        """Plugin.yaml `zenoh` block for native zenoh or zenoh_ros2dds. Returns
        None when no zenoh transport is configured on the OTA side.
        """
        ota = rmw_spec.ota
        if _is_native_zenoh_ota(ota.impl):
            main_peer = ota.zen_main_peer or peer_keys[0]
            main_port = ota.zen_main_port or 7447
            return PluginBlock(
                "zenoh",
                [
                    ("zen_main_ip", peer_ip[main_peer]),
                    ("zen_main_port", main_port),
                    # Only the non-main peer opens a TCP connect endpoint.
                    ("zen_connect", local_peer != main_peer),
                ],
            )
        if ota.impl == "zenoh_ros2dds":
            main_peer = ota.zen_main_peer or peer_keys[0]
            main_port = ota.zen_main_port or 7447
            transport = ota.zen_transport or "udp"
            endpoint_role = "listen" if local_peer == main_peer else "connect"
            items: List[Tuple[str, Any]] = [
                ("zen_pub_allow", f"/ota/{peer_name[local_peer]}/.*"),
                ("zen_sub_allow", f"/ota/{peer_name[remote_peer]}/.*"),
                ("zen_transport", transport),
                ("zen_mode", "router"),
                ("zen_endpoint_role", endpoint_role),
                ("zen_main_ip", peer_ip[main_peer]),
                ("zen_main_port", main_port),
            ]
            if out_entries_pipes is not None:
                qos_block = _zenoh_qos_pub_block(out_entries_pipes, publisher_peer=local_peer)
                if qos_block is not None:
                    items.append(("zen_qos_pub", qos_block))
            return PluginBlock("zenoh", items)
        return None

    # Heartbeat topics (only if enabled). If not configured, default to /heartbeat_<com-name>.
    hb_topic: Dict[str, str] = {}
    if use_heartbeat:
        for p in peer_keys:
            v = None
            try:
                v = (peer_settings_all.get(p) or {}).get("heartbeat_topic")
            except Exception:
                v = None
            if v is None or (isinstance(v, str) and not v.strip()):
                hb_topic[p] = f"/heartbeat_{peer_name[p]}"
            else:
                hb_topic[p] = str(v).strip()

    # ---------------------------------------------------------------------
    # Topics are optional. If no directions are defined, generate minimal plugin.yaml
    # (peers + optional heartbeat/topic_monitor), and always session_specification.yaml.
    # ---------------------------------------------------------------------
    direction_key_for: Dict[Tuple[str, str], str] = {}
    for k in topics_cfg.keys():
        if not isinstance(k, str):
            raise RuntimeError("topics keys must be strings like '<src>_to_<dst>'.")
        parts = k.split("_to_")
        if len(parts) != 2:
            raise RuntimeError(
                f"topics key '{k}' must match '<src>_to_<dst>' so the generator can derive peers generically."
            )
        src, dst = parts[0], parts[1]
        if src not in peer_ip or dst not in peer_ip:
            raise RuntimeError(f"topics key '{k}' refers to unknown peer(s) '{src}'/'{dst}'. Known peers: {peer_keys}")
        if (src, dst) in direction_key_for:
            raise RuntimeError(f"Duplicate topics direction for {src}_to_{dst}.")
        direction_key_for[(src, dst)] = k

    # Heartbeat implies bidirectional topic bridging:
    # - ensure both directions exist (even if empty), so we can generate topic lists containing heartbeat topics
    # - force in/out unless user explicitly disabled them (then error)
    if use_heartbeat:
        if shared_use_in is False or shared_use_out is False:
            raise RuntimeError(
                "shared.use_heartbeat=true requires in/out. Remove shared.use_in/use_out overrides or set both to true."
            )
        if shared_use_in is None:
            shared_use_in = True
        if shared_use_out is None:
            shared_use_out = True

        a, b = peer_keys[0], peer_keys[1]
        direction_key_for.setdefault((a, b), f"{a}_to_{b}")
        direction_key_for.setdefault((b, a), f"{b}_to_{a}")

    session_spec_yaml = _render_session_spec(base_plugin_path)

    if not direction_key_for:
        if shared_use_in is True or shared_use_out is True:
            raise RuntimeError("shared.use_in/use_out cannot be true without defining any topics directions.")

        generated: List[Tuple[str, str]] = []
        if session_definition_yaml is not None:
            generated.append((os.path.join(param_dir, "session-definition.yaml"), session_definition_yaml))
        for local in peer_keys:
            remote = peer_keys[0] if local == peer_keys[1] else peer_keys[1]
            blocks: List[PluginBlock] = []
            blocks.append(
                PluginBlock(
                    "paths",
                    [
                        ("session_dir", session_dir),
                        ("peer_dir", f"${{session_dir}}/{local}"),
                    ],
                )
            )
            blocks.append(
                PluginBlock(
                    "peers",
                    [
                        ("ip_local", peer_ip[local]),
                        ("local_name", peer_name[local]),
                        ("ip_remote", peer_ip[remote]),
                        ("remote_name", peer_name[remote]),
                    ],
                )
            )
            local_items = _build_local_rmw_items(local)
            if local_items:
                blocks.append(PluginBlock("rmw_local", local_items))
            zen_block = _build_zenoh_block(local, remote)
            if zen_block is not None:
                blocks.append(zen_block)
            if _use_domain_bridge(local):
                ldid = peer_local_domain_id[local]
                assert ldid is not None and ota_domain_id is not None
                blocks.append(
                    PluginBlock(
                        "domain_bridge",
                        [
                            ("use_domain_bridge", True),
                            ("local_domain_id", ldid),
                            ("ota_domain_id", ota_domain_id),
                        ],
                    )
                )
            elif peer_local_domain_id[local] is not None:
                # Still export ROS_DOMAIN_ID for the peer's local graph even
                # if no domain bridging is active.
                blocks.append(PluginBlock("domain", [("local_domain_id", peer_local_domain_id[local])]))
            blocks.append(PluginBlock("rmw", _build_ota_rmw_items()))
            if use_topic_monitor:
                blocks.append(PluginBlock("topic_monitor", [("topic_monitor", True)]))
            if use_heartbeat:
                blocks.append(
                    PluginBlock(
                        "heartbeat",
                        [
                            ("heartbeat", True),
                            ("heartbeat_out_topics", hb_topic[local]),
                            ("heartbeat_in_topic", hb_topic[remote]),
                            *_heartbeat_monitor_overrides((shared.get("heartbeat") or {}).get("expect")),
                        ],
                    )
                )
            generated.append((os.path.join(param_dir, local, "plugin.yaml"), _render_plugin_yaml(blocks)))
            generated.append((os.path.join(param_dir, local, "session_specification.yaml"), session_spec_yaml))
        _write_generated_files(generated, force=force, rewrite_formatting=rewrite_formatting)
        return

    # Topics exist. From here on we generate direction topic lists + per-peer plugin.yaml.
    # (Directions may be one-way; that's OK.)

    suffixes = (shared.get("processing_suffixes", {}) or {}) if isinstance(shared, dict) else {}
    if not isinstance(suffixes, dict):
        raise RuntimeError(f"shared.processing_suffixes must be a mapping if provided, got {type(suffixes)}")
    restamped_suffix = str(suffixes.get("restamped", "/restamped"))
    latched_suffix = str(suffixes.get("latched", "/latched"))
    globalframe_suffix = str(suffixes.get("framebridge_global", "/globalframe"))
    ota_suffix = str(suffixes.get("ota_stamped", "/ota_stamped"))

    comp_cfg = (shared.get("compression", {}) or {}) if isinstance(shared, dict) else {}
    if not isinstance(comp_cfg, dict):
        raise RuntimeError(f"shared.compression must be a mapping if provided, got {type(comp_cfg)}")
    comp_algorithm = str(comp_cfg.get("algorithm", "bz2") or "bz2").strip()
    if not comp_algorithm:
        comp_algorithm = "bz2"
    if comp_algorithm not in ALLOWED_COMPRESSION_ALGORITHMS:
        raise RuntimeError(
            "shared.compression.algorithm must be one of "
            f"{sorted(ALLOWED_COMPRESSION_ALGORITHMS)}, got '{comp_algorithm}'."
        )
    comp_alg_suffix = "/" + comp_algorithm

    # Compression behavior flag: only the default mode is implemented currently.
    # If false, we'd need to keep the algorithm suffix and introduce a new decompression suffix, which is not supported.
    if not bool(comp_cfg.get("remove_algorithm_suffix_on_decompression", True)):
        raise RuntimeError(
            "shared.compression.remove_algorithm_suffix_on_decompression=false is not implemented yet. "
            "Set it to true (default)."
        )

    # Heartbeat list position per direction (prepend/append).
    # Default is "prepend" for all directions unless configured in the config.
    hb_pos_cfg = (shared.get("heartbeat_position", {}) or {}) if isinstance(shared, dict) else {}
    if hb_pos_cfg and not isinstance(hb_pos_cfg, dict):
        raise RuntimeError("shared.heartbeat_position must be a mapping: { '<src>_to_<dst>': 'prepend'|'append' }")

    def _hb_position(direction_key: str) -> str:
        v = hb_pos_cfg.get(direction_key, "prepend")
        if not isinstance(v, str):
            raise RuntimeError(f"shared.heartbeat_position['{direction_key}'] must be a string.")
        vv = v.strip().lower()
        if vv not in ("prepend", "append"):
            raise RuntimeError(
                f"shared.heartbeat_position['{direction_key}'] must be 'prepend' or 'append', got '{v}'."
            )
        return vv

    # Precompute entries/pipes per direction
    dir_entries: Dict[Tuple[str, str], List[TopicEntry]] = {}
    dir_pipes: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for (src, dst), direction_key in direction_key_for.items():
        entries = _topic_entries(cfg, direction_key)
        pipes = [
            _compute_pipeline(
                e,
                vars_map,
                restamped_suffix,
                latched_suffix,
                globalframe_suffix,
                comp_alg_suffix,
                ota_suffix,
            )
            for e in entries
        ]
        dir_entries[(src, dst)] = entries
        dir_pipes[(src, dst)] = pipes

    # ---------------------------
    # Zenoh helpers (optional)
    # ---------------------------

    def _zenoh_key_expr_for_topic(publisher_peer: str, entry: TopicEntry, pipe: Dict[str, Any]) -> str:
        base = str(entry.base or "").strip()
        if not base.startswith("/"):
            # Be permissive: allow topics without leading '/', but normalize to match key expr conventions.
            base = "/" + base
        path = base.lstrip("/")
        if not path:
            raise RuntimeError("zen_qos topic cannot be empty.")
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise RuntimeError("zen_qos topic cannot be empty.")

        suffixes: List[str] = []
        if pipe.get("restamp"):
            suffixes.append("restamped")
        if pipe.get("latch"):
            suffixes.append("latched")
        if pipe.get("throttle") is not None:
            suffixes.append(f"max{int(pipe['throttle'])}hz")
        if pipe.get("pixel") is not None:
            suffixes.append(str(pipe["pixel"]))
        if pipe.get("framebridge"):
            suffixes.append("globalframe")
        if pipe.get("compress"):
            suffixes.append("compressed")
        if pipe.get("ota_wrap"):
            suffixes.append("ota_stamped")
        if pipe.get("transport") is not None:
            ts = pipe.get("transport")
            assert isinstance(ts, TransportSpec)
            suffixes.append(ts.type)

        last = parts[-1]
        if suffixes:
            last = last + "_" + "_".join(suffixes)
        parts[-1] = last

        key_expr_path = "/".join(parts)
        return f"ota/{peer_name[publisher_peer]}/{key_expr_path}"

    def _zenoh_qos_pub_block(
        entries_and_pipes: List[Tuple[TopicEntry, Dict[str, Any]]], publisher_peer: str
    ) -> Optional[YamlBlockScalar]:
        items: List[Tuple[str, Dict[str, Any]]] = []
        for e, p in entries_and_pipes:
            if not e.zen_qos:
                continue
            if not isinstance(e.zen_qos, dict):
                raise RuntimeError(f"zen_qos for topic '{e.base}' must be a mapping.")
            zq = dict(e.zen_qos)
            allowed = {"priority", "express"}
            extra = sorted([k for k in zq.keys() if k not in allowed])
            if extra:
                raise RuntimeError(
                    f"zen_qos for topic '{e.base}' has unsupported keys {extra}. Allowed: {sorted(allowed)}"
                )
            priority_raw = zq.get("priority")
            if priority_raw is None:
                raise RuntimeError("zen_qos.priority is required when zen_qos is provided.")
            priority = str(priority_raw).strip()
            if not priority:
                raise RuntimeError("zen_qos.priority must be a non-empty string when provided.")
            express = zq.get("express", None)
            if express is not None and not isinstance(express, bool):
                raise RuntimeError(f"zen_qos.express for topic '{e.base}' must be boolean if provided.")

            key_expr = _zenoh_key_expr_for_topic(publisher_peer, e, p)
            cfg: Dict[str, Any] = {"priority": priority}
            if express:
                cfg["express"] = True
            items.append((key_expr, cfg))

        if not items:
            return None

        # Stable ordering (example expects costmap before heartbeat)
        items.sort(key=lambda x: x[0])

        rendered_entries: List[str] = []
        for key_expr, cfg in items:
            parts_cfg = [f'priority: "{cfg["priority"]}"']
            if cfg.get("express"):
                parts_cfg.append("express: true")
            cfg_inline = "{ " + ", ".join(parts_cfg) + " }"
            rendered_entries.append(f'{{\n  key_exprs: [ "{key_expr}" ],\n  config: {cfg_inline}\n}}')

        # Join as a comma-separated list of objects (as expected by zenoh.json5.template)
        content = ",\n".join(rendered_entries)
        return YamlBlockScalar(header="|1", content=content)

    # Determine whether we should generate qos.yaml:
    # - if shared.qos is explicitly provided, OR
    # - if any topic specifies a qos override
    qos_overrides: Dict[str, Dict[str, Any]] = {}
    for entries in dir_entries.values():
        for e in entries:
            if not e.qos:
                continue
            q = dict(e.qos)
            qos_overrides[e.base] = q
    write_qos = ("qos" in shared and shared.get("qos") is not None) or bool(qos_overrides)

    def topic_list(entries: List[TopicEntry], pipes: List[Dict[str, Any]], hb: str, pos: str) -> List[str]:
        lines = []
        for e, p in zip(entries, pipes):
            if e.base == hb:
                continue
            lines.append(p["final"])
        lines = _dedup_keep_order(lines)
        if use_heartbeat and hb:
            if pos == "prepend":
                return [hb] + [x for x in lines if x != hb]
            return [x for x in lines if x != hb] + [hb]
        return lines

    def topic_list_with_types(
        entries: List[TopicEntry],
        pipes: List[Dict[str, Any]],
        hb: str,
        pos: str,
    ) -> List[Tuple[str, str]]:
        lines: List[Tuple[str, str]] = []
        for e, p in zip(entries, pipes):
            if e.base == hb:
                continue
            lines.append((p["final"], _final_topic_type(e, p)))
        lines = _dedup_topic_type_items(lines)
        if use_heartbeat and hb:
            hb_item = (hb, HEARTBEAT_MSG_TYPE)
            if pos == "prepend":
                return [hb_item] + [x for x in lines if x[0] != hb]
            return [x for x in lines if x[0] != hb] + [hb_item]
        return lines

    # ---------------------------
    # Generate topic lists for each direction (written to <src>_to_<dst>_topics.txt)
    # ---------------------------
    dir_topic_list: Dict[Tuple[str, str], List[str]] = {}
    dir_topic_list_with_types: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    for (src, dst), direction_key in direction_key_for.items():
        pos = _hb_position(direction_key)
        hb = hb_topic.get(src, "")
        dir_topic_list[(src, dst)] = topic_list(
            dir_entries[(src, dst)],
            dir_pipes[(src, dst)],
            hb,
            pos,
        )
        if use_domain_bridge:
            dir_topic_list_with_types[(src, dst)] = topic_list_with_types(
                dir_entries[(src, dst)],
                dir_pipes[(src, dst)],
                hb,
                pos,
            )

    # ---------------------------
    # Per-peer plugin.yaml generation
    # ---------------------------
    per_peer_plugin: Dict[str, str] = {}
    per_peer_comp_yaml: Dict[str, Optional[str]] = {p: None for p in peer_keys}
    per_peer_deco_yaml: Dict[str, Optional[str]] = {p: None for p in peer_keys}
    per_peer_otaw_yaml: Dict[str, Optional[str]] = {p: None for p in peer_keys}
    per_peer_otau_yaml: Dict[str, Optional[str]] = {p: None for p in peer_keys}
    per_peer_domain_bridge_yaml: Dict[str, Optional[str]] = {p: None for p in peer_keys}
    per_peer_status_spec_yaml: Dict[str, Optional[str]] = {p: None for p in peer_keys}

    def _prefix_with_source_name_if_needed(target_peer: str, source_peer: str, topic: str) -> str:
        ps = peer_settings_all.get(target_peer, {}) or {}
        keep_source = (
            bool((ps.get("inbound", {}) or {}).get("keep_source_prefix", False)) if isinstance(ps, dict) else False
        )
        prefix = f"/{peer_name[source_peer]}" if keep_source else ""
        return f"{prefix}{topic}"

    def _com_topic(direction: str, source_name: str, forward_topic: str) -> str:
        return f"/com/{direction}/{source_name}/{forward_topic.lstrip('/')}"

    for local in peer_keys:
        remote = peer_keys[0] if local == peer_keys[1] else peer_keys[1]
        out_dir_key = direction_key_for.get((local, remote))
        in_dir_key = direction_key_for.get((remote, local))

        out_entries = dir_entries.get((local, remote), [])
        out_pipes = dir_pipes.get((local, remote), [])
        in_entries = dir_entries.get((remote, local), [])
        in_pipes = dir_pipes.get((remote, local), [])

        # Local outbound derived lists
        rs_topics = _dedup_keep_order([e.base for e, p in zip(out_entries, out_pipes) if p["restamp"]])
        lat_topics = _dedup_keep_order([p["lat_in"] for p in out_pipes if p["lat_in"]])

        # Trickle: local-only periodic republisher (receiver side only).
        # Subscribes to inbound final topics (with source-name prefix when applicable)
        # and republishes at a fixed rate for visualization software.
        trickle_in_items = [
            (_prefix_with_source_name_if_needed(local, remote, p["final"]), p["trickle_hz"])
            for p in in_pipes
            if p["trickle_hz"] is not None
        ]
        trickle_all_topics = _dedup_keep_order([t for t, _ in trickle_in_items])
        # Use the first configured rate (all trickle topics share a single timer).
        trickle_hz = None
        for _, hz in trickle_in_items:
            if hz is not None:
                trickle_hz = hz
                break

        fb_l2g = _dedup_keep_order([p["fb_l2g_in"] for p in out_pipes if p["fb_l2g_in"]])
        # Local inbound derived lists for framebridge (global_to_local topics are specified on inbound direction)
        fb_g2l = _dedup_keep_order([p["fb_g2l_base"] for p in in_pipes if p["fb_g2l_base"]])

        drp_items = [(p["drp_in"], p["drop_count"], p["window_size"]) for p in out_pipes if p["drp_in"]]
        thr_items = [(p["thr_in"], p["throttle"]) for p in out_pipes if p["thr_in"]]
        ipx_items = [(p["ipx_in"], p["pixel"]) for p in out_pipes if p["ipx_in"]]
        it_items = [(p["it_in"], p["transport"]) for p in out_pipes if p["it_in"]]
        irt_items_local = [(p["irt_in"], p["transport"]) for p in out_pipes if p["irt_in"]]

        # Compression happens on the sender side for topics marked with processing.compress
        comp_topics = _dedup_keep_order([p["comp_in"] for p in out_pipes if p["comp_in"]])
        if comp_topics:
            per_peer_comp_yaml[local] = _render_regex_list("compression", comp_topics)

        ota_topics = _dedup_keep_order([p["ota_in"] for p in out_pipes if p["ota_in"]])
        if ota_topics:
            per_peer_otaw_yaml[local] = _render_regex_list("ota_wrapper", ota_topics)

        # Decompression happens on the receiver side for inbound topics that were compressed by the remote.
        inbound_comp_topics = _dedup_keep_order([p["comp_in"] for p in in_pipes if p["comp_in"]])
        if inbound_comp_topics:
            deco_topics = [
                _prefix_with_source_name_if_needed(local, remote, t) + comp_alg_suffix for t in inbound_comp_topics
            ]
            per_peer_deco_yaml[local] = _render_regex_list("decompression", deco_topics)

        inbound_ota_topics = _dedup_keep_order([p["ota_in"] for p in in_pipes if p["ota_in"]])
        if inbound_ota_topics:
            otau_topics = [
                _prefix_with_source_name_if_needed(local, remote, t) + ota_suffix for t in inbound_ota_topics
            ]
            per_peer_otau_yaml[local] = _render_regex_list("ota_unwrapper", otau_topics)

        # Normalization happens on receiver side for topics with normalize_on_target.
        nor_items = []
        for e, p in zip(in_entries, in_pipes):
            if not p["normalize"]:
                continue
            base = e.base.lstrip("/")
            suffix_source = p["comp_in"] if p["comp_in"] else p["final"]
            suffix = suffix_source[len(e.base) :]
            nor_items.append((base, suffix))

        # Hard limits from session_plugin_base.yaml.
        def _assert_max(label: str, items: List[Any], limit: int) -> None:
            if len(items) > limit:
                raise RuntimeError(
                    f"peer '{local}': too many '{label}' entries ({len(items)}). "
                    f"The base session plugin supports at most {limit}."
                )

        _assert_max("drp", drp_items, 8)
        _assert_max("thr", thr_items, 4)
        _assert_max("ipx", ipx_items, 4)
        _assert_max("it", it_items, 4)
        _assert_max("nor", nor_items, 4)

        # Remote-side reverse transport is configured (if requested via local_republish) on the receiver, too.
        irt_items_remote = []
        for e, p in zip(in_entries, in_pipes):
            if not p["irt_in"]:
                continue
            tspec = p["transport"]
            assert tspec is not None
            topic = _prefix_with_source_name_if_needed(local, remote, p["irt_in"])
            irt_items_remote.append((topic, tspec.type))

        # Build plugin.yaml blocks
        blocks: List[PluginBlock] = []
        blocks.append(
            PluginBlock(
                "paths",
                [
                    ("session_dir", session_dir),
                    ("peer_dir", f"${{session_dir}}/{local}"),
                ],
            )
        )
        blocks.append(
            PluginBlock(
                "peers",
                [
                    ("ip_local", peer_ip[local]),
                    ("local_name", peer_name[local]),
                    ("ip_remote", peer_ip[remote]),
                    ("remote_name", peer_name[remote]),
                ],
            )
        )
        local_items = _build_local_rmw_items(local)
        if local_items:
            blocks.append(PluginBlock("rmw_local", local_items))
        if write_qos:
            blocks.append(PluginBlock("qos", [("qos_config_file", "${session_dir}/qos.yaml")]))

        ps_local = peer_settings_all.get(local, {}) or {}
        ps_local = ps_local if isinstance(ps_local, dict) else {}
        inbound_cfg = (ps_local.get("inbound", {}) or {}) if isinstance(ps_local, dict) else {}
        outbound_cfg = (ps_local.get("outbound", {}) or {}) if isinstance(ps_local, dict) else {}
        outbound_source_cfg = (outbound_cfg.get("source_prefix", {}) or {}) if isinstance(outbound_cfg, dict) else {}
        outbound_target_cfg = (outbound_cfg.get("target_prefix", {}) or {}) if isinstance(outbound_cfg, dict) else {}

        # Remote target-prefix config (used for inbound targeting + topic monitor)
        ps_remote = peer_settings_all.get(remote, {}) or {}
        ps_remote = ps_remote if isinstance(ps_remote, dict) else {}
        remote_outbound = (ps_remote.get("outbound", {}) or {}) if isinstance(ps_remote, dict) else {}
        remote_outbound_target = (
            (remote_outbound.get("target_prefix", {}) or {}) if isinstance(remote_outbound, dict) else {}
        )
        remote_uses_target_prefix = bool(remote_outbound_target.get("use_target_prefix", False))

        # ---------------------------
        # Validate / normalize source + target prefix settings
        # ---------------------------

        # Source prefix:
        use_source_prefix = outbound_source_cfg.get("use_source_prefix", True)
        if not isinstance(use_source_prefix, bool):
            raise RuntimeError(f"peer_settings.{local}.outbound.source_prefix.use_source_prefix must be boolean.")
        if "use_source_prefix" in outbound_source_cfg and not use_source_prefix:
            raise RuntimeError(
                f"peer_settings.{local}.outbound.source_prefix.use_source_prefix=false is not implemented yet."
            )
        native_have_source_prefix = outbound_source_cfg.get("native_have_source_prefix", False)
        if not isinstance(native_have_source_prefix, bool):
            raise RuntimeError(
                f"peer_settings.{local}.outbound.source_prefix.native_have_source_prefix must be boolean."
            )

        # Target prefix:
        # - native_have_outgoing_target_prefix defaults to use_target_prefix
        # - any mismatch is not supported by the current plugin.yaml semantics
        use_target_prefix = outbound_target_cfg.get("use_target_prefix", False)
        if not isinstance(use_target_prefix, bool):
            raise RuntimeError(f"peer_settings.{local}.outbound.target_prefix.use_target_prefix must be boolean.")
        native_have_outgoing_target_prefix = outbound_target_cfg.get(
            "native_have_outgoing_target_prefix", use_target_prefix
        )
        if not isinstance(native_have_outgoing_target_prefix, bool):
            raise RuntimeError(
                f"peer_settings.{local}.outbound.target_prefix.native_have_outgoing_target_prefix must be boolean."
            )
        if native_have_outgoing_target_prefix != use_target_prefix:
            raise RuntimeError(
                f"peer_settings.{local}.outbound.target_prefix has unsupported combination: "
                f"native_have_outgoing_target_prefix={native_have_outgoing_target_prefix} but use_target_prefix={use_target_prefix}. "
                "Currently they must be equal (both true or both false)."
            )

        # Inbound keep_target_prefix validation:
        if not isinstance(inbound_cfg, dict):
            raise RuntimeError(f"peer_settings.{local}.inbound must be a mapping.")
        keep_target_prefix_specified = "keep_target_prefix" in inbound_cfg
        keep_target_prefix = bool(inbound_cfg.get("keep_target_prefix", False))
        if keep_target_prefix_specified and not remote_uses_target_prefix:
            raise RuntimeError(
                f"peer_settings.{local}.inbound.keep_target_prefix is specified but peer '{remote}' "
                "does not use target prefixes on outbound. Remove keep_target_prefix from the config."
            )
        if remote_uses_target_prefix and keep_target_prefix:
            raise RuntimeError(
                f"peer_settings.{local}.inbound.keep_target_prefix=true is not implemented yet. "
                "Set it to false (strip target prefix)."
            )
        local_explicitly_targeted_inbound = remote_uses_target_prefix and (not keep_target_prefix)

        # IN: only if inbound direction exists (or explicitly requested, in which case error if missing)
        in_list = dir_topic_list.get((remote, local), []) if in_dir_key else []
        in_list_with_types = (
            dir_topic_list_with_types.get((remote, local), []) if use_domain_bridge and in_dir_key else []
        )
        in_enabled = bool(shared_use_in) if shared_use_in is not None else (len(in_list) > 0)
        if in_enabled and not in_dir_key:
            raise RuntimeError(f"shared.use_in=true but no inbound topics direction '{remote}_to_{local}' is defined.")
        if in_dir_key:
            blocks.append(
                PluginBlock(
                    "in",
                    (
                        [("in", in_enabled), ("topic_list_paths_inbound", f"${{session_dir}}/{in_dir_key}_topics.txt")]
                        + ([("local_explicitly_targeted_inbound", True)] if local_explicitly_targeted_inbound else [])
                        + (
                            [("app_keep_source_name_inbound", True)]
                            if bool(inbound_cfg.get("keep_source_prefix", False))
                            else []
                        )
                    ),
                )
            )

        # OUT: only if outbound direction exists (or explicitly requested, in which case error if missing)
        out_list = dir_topic_list.get((local, remote), []) if out_dir_key else []
        out_list_with_types = (
            dir_topic_list_with_types.get((local, remote), []) if use_domain_bridge and out_dir_key else []
        )
        out_enabled = bool(shared_use_out) if shared_use_out is not None else (len(out_list) > 0)
        if out_enabled and not out_dir_key:
            raise RuntimeError(
                f"shared.use_out=true but no outbound topics direction '{local}_to_{remote}' is defined."
            )
        if out_dir_key:
            out_items: List[Tuple[str, Any]] = [
                ("out", out_enabled),
                ("topic_list_paths_outbound", f"${{session_dir}}/{out_dir_key}_topics.txt"),
            ]
            if use_target_prefix:
                out_items.append(("remote_explicitly_targeted_name_outbound", peer_name[remote]))
            if native_have_source_prefix:
                out_items.append(("app_has_source_name_outbound", True))
            blocks.append(PluginBlock("out", out_items))

        if _use_domain_bridge(local):
            ldid = peer_local_domain_id[local]
            assert ldid is not None and ota_domain_id is not None

            domain_bridge_topics: Dict[str, Dict[str, Any]] = {}
            if out_enabled:
                for topic_name, msg_type in out_list_with_types:
                    forward_topic = f"/to_{peer_name[remote]}{topic_name}" if use_target_prefix else topic_name
                    domain_bridge_topics[_com_topic("out", peer_name[local], forward_topic)] = {
                        "type": msg_type,
                        "from_domain": ldid,
                        "to_domain": ota_domain_id,
                    }
            if in_enabled:
                for topic_name, msg_type in in_list_with_types:
                    forward_topic = f"/to_{peer_name[local]}{topic_name}" if remote_uses_target_prefix else topic_name
                    domain_bridge_topics[_com_topic("in", peer_name[remote], forward_topic)] = {
                        "type": msg_type,
                        "from_domain": ota_domain_id,
                        "to_domain": ldid,
                    }

            if domain_bridge_topics:
                per_peer_domain_bridge_yaml[local] = _render_domain_bridge_yaml(
                    f"{peer_name[local]}_com_domain_bridge",
                    domain_bridge_topics,
                )
                blocks.append(
                    PluginBlock(
                        "domain_bridge",
                        [
                            ("use_domain_bridge", True),
                            ("local_domain_id", ldid),
                            ("ota_domain_id", ota_domain_id),
                            ("domain_bridge_config_file", "${peer_dir}/domain_bridge.yaml"),
                        ],
                    )
                )
        elif peer_local_domain_id[local] is not None:
            # No domain bridging but the peer still has a pinned ROS_DOMAIN_ID.
            blocks.append(PluginBlock("domain", [("local_domain_id", peer_local_domain_id[local])]))

        zen_block = _build_zenoh_block(
            local,
            remote,
            out_entries_pipes=list(zip(out_entries, out_pipes)),
        )
        if zen_block is not None:
            blocks.append(zen_block)

        blocks.append(PluginBlock("rmw", _build_ota_rmw_items()))

        # Topic monitor: set to_adressant only when target-prefix addressing is in play
        tm_in_to = peer_name[local] if bool(remote_outbound_target.get("use_target_prefix", False)) else None
        tm_out_to = peer_name[remote] if use_target_prefix else None
        if use_topic_monitor:
            blocks.append(
                PluginBlock(
                    "topic_monitor",
                    (
                        [("topic_monitor", True)]
                        + ([("tm_in_to_adressant", tm_in_to)] if tm_in_to is not None else [])
                        + ([("tm_out_to_adressant", tm_out_to)] if tm_out_to is not None else [])
                    ),
                )
            )

        if use_status_overview:
            per_peer_status_spec_yaml[local] = _build_status_pipeline_spec(
                local=local,
                remote=remote,
                peer_name=peer_name,
                out_entries=out_entries,
                out_pipes=out_pipes,
                in_entries=in_entries,
                in_pipes=in_pipes,
                use_target_prefix=use_target_prefix,
                remote_uses_target_prefix=remote_uses_target_prefix,
                native_have_source_prefix=native_have_source_prefix,
                inbound_keep_source_prefix=bool(inbound_cfg.get("keep_source_prefix", False)),
                local_domain_id=peer_local_domain_id[local],
                ota_domain_id=ota_domain_id,
                uses_domain_bridge=_use_domain_bridge(local),
                hb_topic=hb_topic,
                use_heartbeat=use_heartbeat,
                heartbeat_expect=(shared.get("heartbeat") or {}).get("expect"),
                out_enabled=out_enabled,
                in_enabled=in_enabled,
                final_topic_type=_final_topic_type,
            )
            blocks.append(
                PluginBlock(
                    "status_overview",
                    [
                        ("status_overview", True),
                        ("status_spec_file", "${peer_dir}/pipeline_spec.yaml"),
                    ],
                )
            )

        if use_heartbeat:
            # Heartbeat topics are base topics, but may be explicitly targeted on outbound and/or source-prefixed on inbound.
            hb_out = hb_topic[local]
            if use_target_prefix:
                hb_out = f"/to_{peer_name[remote]}{hb_out}"
            hb_in = hb_topic[remote]
            if bool(inbound_cfg.get("keep_source_prefix", False)):
                hb_in = f"/{peer_name[remote]}{hb_in}"
            blocks.append(
                PluginBlock(
                    "heartbeat",
                    [
                        ("heartbeat", True),
                        ("heartbeat_out_topics", hb_out),
                        ("heartbeat_in_topic", hb_in),
                        *_heartbeat_monitor_overrides((shared.get("heartbeat") or {}).get("expect")),
                    ],
                )
            )

        if rs_topics:
            blocks.append(
                PluginBlock(
                    "rs",
                    [
                        ("rs", True),
                        ("rs_restamp_topics", ",".join(rs_topics)),
                        ("rs_topic_suffix", restamped_suffix),
                    ],
                )
            )

        if lat_topics:
            blocks.append(
                PluginBlock(
                    "lat",
                    [
                        ("lat", True),
                        ("lat_topics", ",".join(lat_topics)),
                        ("lat_topic_suffix", latched_suffix),
                    ],
                )
            )

        if trickle_all_topics and trickle_hz is not None:
            blocks.append(
                PluginBlock(
                    "trickle",
                    [
                        ("trickle", True),
                        ("trickle_topics", ",".join(trickle_all_topics)),
                        ("trickle_topic_suffix", "/trickle"),
                        ("trickle_rate_hz", trickle_hz),
                    ],
                )
            )

        if fb_l2g or fb_g2l:
            fb_cfg = (ps_local.get("framebridge", {}) or {}) if isinstance(ps_local, dict) else {}
            global_frame_prefix = str(fb_cfg.get("global_frame_prefix", peer_name[local])).rstrip("_")
            exclude_frames = fb_cfg.get("prefix_exclude_frames", []) or []
            tf_filter_frames = fb_cfg.get("tf_filter_frames", []) or []
            tf_throttle_links = fb_cfg.get("tf_throttle_links", []) or []
            items: List[Tuple[str, Any]] = [
                ("fb", True),
                ("fb_global_frame_prefix", global_frame_prefix),
                ("fb_prefix_exclude_frames", ",".join(exclude_frames)),
            ]
            if tf_filter_frames:
                items.append(("fb_tf_filter_frames", ",".join(tf_filter_frames)))
            if tf_throttle_links:
                items.append(("fb_tf_throttle_links", ",".join(tf_throttle_links)))
            if fb_l2g:
                items.append(("fb_local_to_global_topics", ",".join(fb_l2g)))
            if fb_g2l:
                items.append(("fb_global_to_local_topics", ",".join(fb_g2l)))
            items.append(("fb_global_topic_suffix", globalframe_suffix))
            blocks.append(PluginBlock("fb", items))

        if comp_topics:
            blocks.append(
                PluginBlock(
                    "comp",
                    [
                        ("comp", True),
                        ("comp_config_file", "${peer_dir}/compression.yaml"),
                        ("comp_algorithm", comp_algorithm),
                    ],
                )
            )

        if ota_topics:
            blocks.append(
                PluginBlock(
                    "otaw",
                    [
                        ("otaw", True),
                        ("otaw_config_file", "${peer_dir}/ota_wrapper.yaml"),
                        ("otaw_suffix", ota_suffix),
                    ],
                )
            )

        if inbound_comp_topics:
            blocks.append(
                PluginBlock(
                    "deco",
                    [
                        ("deco", True),
                        ("deco_config_file", "${peer_dir}/decompression.yaml"),
                        ("deco_algorithm", comp_algorithm),
                    ],
                )
            )

        if inbound_ota_topics:
            blocks.append(
                PluginBlock(
                    "otau",
                    [
                        ("otau", True),
                        ("otau_config_file", "${peer_dir}/ota_unwrapper.yaml"),
                        ("otau_suffix", ota_suffix),
                    ],
                )
            )

        if drp_items:
            items2: List[Tuple[str, Any]] = [("drp", True)]
            if len(drp_items) > 4:
                items2.append(("drp2", True))
            for i, (t, dc, ws) in enumerate(drp_items, 1):
                items2.append((f"drp_topic_{i}", t))
                items2.append((f"drp_drop_count_{i}", dc))
                items2.append((f"drp_window_size_{i}", ws))
            blocks.append(PluginBlock("drp", items2))

        if thr_items:
            items2 = [("thr", True)]
            for i, (t, rate) in enumerate(thr_items, 1):
                items2.append((f"thr_topic_{i}", t))
                items2.append((f"thr_rate_{i}", rate))
            blocks.append(PluginBlock("thr", items2))

        if ipx_items:
            items2 = [("ipx", True)]
            for i, (t, preset) in enumerate(ipx_items, 1):
                items2.append((f"ipx_{i}_topics", t))
                items2.append((f"ipx_{i}_preset", preset))
            blocks.append(PluginBlock("ipx", items2))

        if it_items:
            items2 = [("it", True)]
            for i, (t, tspec) in enumerate(it_items, 1):
                assert tspec is not None
                items2.append((f"it_{i}_topic", t))
                items2.append((f"it_{i}_transport", tspec.type))
                if tspec.type == "ffmpeg":
                    if "gop_size" in tspec.params:
                        items2.append((f"it_{i}_ffmpeg_gop_size", int(tspec.params["gop_size"])))
                    if "bit_rate" in tspec.params:
                        items2.append((f"it_{i}_ffmpeg_bit_rate", int(tspec.params["bit_rate"])))
                    if "encoder_av_options" in tspec.params:
                        items2.append((f"it_{i}_ffmpeg_encoder_av_options", tspec.params["encoder_av_options"]))
                elif tspec.type == "foxglove":
                    # image_transport "foxglove" transport (CompressedVideo) tunables
                    if "gop_size" in tspec.params:
                        items2.append((f"it_{i}_foxglove_gop_size", int(tspec.params["gop_size"])))
                    if "encoder_av_options" in tspec.params:
                        items2.append((f"it_{i}_foxglove_encoder_av_options", tspec.params["encoder_av_options"]))
                    if "bit_rate" in tspec.params:
                        items2.append((f"it_{i}_foxglove_bit_rate", int(tspec.params["bit_rate"])))
                    if "qmax" in tspec.params:
                        items2.append((f"it_{i}_foxglove_qmax", int(tspec.params["qmax"])))
                elif tspec.type == "compressed":
                    if "jpeg_quality" in tspec.params:
                        items2.append((f"it_{i}_compressed_jpeg_quality", int(tspec.params["jpeg_quality"])))
                else:
                    raise ValueError(f"Unsupported transport type '{tspec.type}'")
            blocks.append(PluginBlock("it", items2))

        # Merge reverse-transport config for (a) local reconstruction and (b) inbound remote topics (if requested).
        irt_all: List[Tuple[str, str]] = []
        for t, tspec in irt_items_local:
            assert tspec is not None
            irt_all.append((t, tspec.type))
        irt_all.extend(irt_items_remote)
        _assert_max("irt", irt_all, 4)
        if irt_all:
            items2 = [("irt", True)]
            for i, (topic, ttype) in enumerate(irt_all, 1):
                items2.append((f"irt_{i}_topic", topic))
            for i, (topic, ttype) in enumerate(irt_all, 1):
                items2.append((f"irt_{i}_transport", ttype))
            blocks.append(PluginBlock("irt", items2))

        if nor_items:
            items2 = [("nor", True)]
            nor_prefix = f"/{peer_name[remote]}" if bool(inbound_cfg.get("keep_source_prefix", False)) else ""
            for i, (base, suffix) in enumerate(nor_items, 1):
                items2.append((f"nor_topic_{i}_prefix", nor_prefix))
                items2.append((f"nor_topic_{i}_base", base))
                items2.append((f"nor_topic_{i}_suffix", suffix))
            blocks.append(PluginBlock("nor", items2))

        per_peer_plugin[local] = _render_plugin_yaml(blocks)

    qos_yaml = (
        _render_qos_yaml(shared.get("qos", {}) if isinstance(shared, dict) else {}, qos_overrides) if write_qos else ""
    )

    # session_specification.yaml
    session_spec_yaml = _render_session_spec(base_plugin_path)

    generated: List[Tuple[str, str]] = []
    if session_definition_yaml is not None:
        generated.append((os.path.join(param_dir, "session-definition.yaml"), session_definition_yaml))
    # Direction topic lists
    for (src, dst), direction_key in direction_key_for.items():
        generated.append(
            (
                os.path.join(param_dir, f"{direction_key}_topics.txt"),
                "\n".join(dir_topic_list[(src, dst)]) + "\n",
            )
        )

    if write_qos:
        generated.append((os.path.join(param_dir, "qos.yaml"), qos_yaml))

    # Per-peer outputs
    for p in peer_keys:
        generated.append((os.path.join(param_dir, p, "plugin.yaml"), per_peer_plugin[p]))
        generated.append((os.path.join(param_dir, p, "session_specification.yaml"), session_spec_yaml))

        # compression/decompression YAMLs are generated only if needed
        if per_peer_comp_yaml.get(p):
            generated.append((os.path.join(param_dir, p, "compression.yaml"), per_peer_comp_yaml[p] or ""))
        if per_peer_deco_yaml.get(p):
            generated.append((os.path.join(param_dir, p, "decompression.yaml"), per_peer_deco_yaml[p] or ""))
        if per_peer_otaw_yaml.get(p):
            generated.append((os.path.join(param_dir, p, "ota_wrapper.yaml"), per_peer_otaw_yaml[p] or ""))
        if per_peer_otau_yaml.get(p):
            generated.append((os.path.join(param_dir, p, "ota_unwrapper.yaml"), per_peer_otau_yaml[p] or ""))
        if per_peer_domain_bridge_yaml.get(p):
            generated.append((os.path.join(param_dir, p, "domain_bridge.yaml"), per_peer_domain_bridge_yaml[p] or ""))
        if per_peer_status_spec_yaml.get(p):
            generated.append((os.path.join(param_dir, p, "pipeline_spec.yaml"), per_peer_status_spec_yaml[p] or ""))

    _write_generated_files(generated, force=force, rewrite_formatting=rewrite_formatting)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate session files from a session definition or parametrization (semantic diff, newline-safe)."
    )
    parser.add_argument(
        "-c",
        "--session-config-yaml",
        dest="session_config_yaml",
        help="Path to session-definition.yaml, session-parametrization.yaml",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite existing files if they differ semantically (format-only differences are ignored).",
    )
    parser.add_argument(
        "--rewrite-formatting",
        action="store_true",
        help="Rewrite files if content differs textually even when semantically equal (e.g. blank lines / ordering).",
    )
    parser.add_argument(
        "--base-plugin-path",
        help="Path to session_plugin_base.yaml to include in session_specification.yaml",
        default=BASE_PLUGIN_PATH_DEFAULT,
    )
    args = parser.parse_args()
    func(**{k: v for k, v in vars(args).items() if v is not None})
