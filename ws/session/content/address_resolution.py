#!/usr/bin/env python3
"""Resolve rosotacom peer address expressions.

Address expressions are intentionally small:
- literal IPs/hostnames are used as-is, e.g. ``192.168.1.10`` or ``robot.local``
- data-dict references use ``data:<key>``, e.g. ``data:machine_a_ip``
- multiple remote addresses may be joined with ``+``
"""

import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple


DATA_PREFIX = "data:"


def remove_comments(json_like: str) -> str:
    pattern = r"//.*?$|/\*.*?\*/"
    return re.sub(pattern, "", json_like, flags=re.DOTALL | re.MULTILINE)


def candidate_data_dict_paths() -> List[str]:
    script_dir = os.path.dirname(os.path.realpath(__file__))
    repo_dir = os.path.abspath(os.path.join(script_dir, "..", "..", ".."))
    workspace_dir = os.path.dirname(repo_dir)
    candidates = [
        "/data_dict.json",
        "/session/data_dict.json",
        os.path.join(repo_dir, "data_dict.json"),
        os.path.join(workspace_dir, "session", "data_dict.json"),
    ]
    result = []
    for path in candidates:
        if path not in result:
            result.append(path)
    return result


def load_data_dict(candidate_paths: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    paths = list(candidate_paths) if candidate_paths is not None else candidate_data_dict_paths()
    for path in paths:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as file:
                return json.loads(remove_comments(file.read()))
    raise FileNotFoundError(
        "Could not find data_dict.json. Tried: " + ", ".join(paths)
    )


def parse_data_reference(address_expr: str) -> Optional[str]:
    if not isinstance(address_expr, str):
        return None
    value = address_expr.strip()
    if not value.startswith(DATA_PREFIX):
        return None
    key = value[len(DATA_PREFIX):].strip()
    if not key:
        raise RuntimeError("Address expression 'data:' must include a data_dict key.")
    return key


def format_data_reference(key: str) -> str:
    key = str(key).strip()
    if key.startswith(DATA_PREFIX):
        key = key[len(DATA_PREFIX):].strip()
    if not key:
        raise RuntimeError("data_dict reference key must be non-empty.")
    return DATA_PREFIX + key


def _resolve_nested_key(data_dict: Dict[str, Any], key: str) -> Any:
    result: Any = data_dict
    for part in key.split():
        if isinstance(result, dict) and part in result:
            result = result[part]
        else:
            raise KeyError(key)
    return result


def resolve_data_reference(key: str, data_dict: Optional[Dict[str, Any]] = None) -> str:
    key = str(key).strip()
    if not key:
        raise RuntimeError("data_dict reference key must be non-empty.")
    if data_dict is None:
        data_dict = load_data_dict()

    if " " in key:
        try:
            return str(_resolve_nested_key(data_dict, key))
        except KeyError as exc:
            raise RuntimeError(f"data_dict key '{key}' was not found.") from exc

    if key in data_dict:
        return str(data_dict[key])

    matches = []
    for top_value in data_dict.values():
        if isinstance(top_value, dict) and key in top_value:
            matches.append(top_value[key])

    if len(matches) == 1:
        return str(matches[0])
    if len(matches) > 1:
        raise RuntimeError(f"Ambiguous data_dict key '{key}' found in multiple groups.")
    raise RuntimeError(f"data_dict key '{key}' was not found.")


def find_data_dict_leaf(data_dict: Dict[str, Any], leaf_key: str) -> Tuple[Optional[str], str]:
    leaf_key = str(leaf_key).strip()
    matches = []
    for top_key, value in (data_dict or {}).items():
        if isinstance(value, dict):
            if leaf_key in value:
                matches.append((top_key, value[leaf_key]))
        elif top_key == leaf_key:
            matches.append((None, value))

    if not matches:
        raise RuntimeError(f"Peer override references unknown data_dict key '{leaf_key}'.")
    if len(matches) > 1:
        groups = [m[0] for m in matches]
        raise RuntimeError(
            f"Peer override key '{leaf_key}' is ambiguous in data_dict; matches groups={groups}."
        )
    group_name, value = matches[0]
    return group_name, str(value)


def resolve_address_expression(
    address_expr: str,
    data_dict: Optional[Dict[str, Any]] = None,
) -> str:
    if not isinstance(address_expr, str):
        raise TypeError("Address expression must be a string.")
    value = address_expr.strip()
    if not value:
        raise RuntimeError("Address expression must be non-empty.")

    data_key = parse_data_reference(value)
    if data_key is None:
        return value
    return resolve_data_reference(data_key, data_dict=data_dict)


def resolve_address_expressions(address_exprs: str) -> List[str]:
    if not isinstance(address_exprs, str):
        raise TypeError("Address expression must be a string.")
    parts = [part.strip() for part in address_exprs.split("+")]
    resolved = [resolve_address_expression(part) for part in parts if part]
    if not resolved:
        raise RuntimeError("Address expression must resolve to at least one address.")
    return resolved


def main(key_string: str) -> List[str]:
    return resolve_address_expressions(key_string)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Resolve rosotacom address expressions.")
    parser.add_argument("-k", "--key_string", required=True)
    args = parser.parse_args()
    print(";".join(main(args.key_string)))
