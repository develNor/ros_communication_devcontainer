#!/usr/bin/env python3
"""Unified OTA DDS XML generator.

Loads an XML template from /ws/ota_configs/<config>(.template), detects which
placeholders it uses (#host_ip, #peer, #easy_mode_ip, #spdp_interval),
substitutes already resolved addresses, and prints the resulting XML to stdout.

Replaces the old per-template scripts (get_fastdds_xml.py,
get_fastdds_easy_mode_xml.py, get_cyclonedds_xml.py).
"""

import argparse
import os
import re


KNOWN_PLACEHOLDERS = {"#host_ip", "#peer", "#easy_mode_ip", "#spdp_interval"}
PLACEHOLDER_PATTERN = re.compile(r"#[A-Za-z_][A-Za-z0-9_]*")
DEFAULT_SPDP_INTERVAL = "30s"


def _template_path(name: str) -> str:
    """Resolve a config name to an on-disk template path.

    Accepts forms like 'fastdds_v1.xml', 'fastdds_v1.xml.template',
    or 'fastdds_v1' (we always look for a '<name>.template' file).
    Container path is preferred; falls back to the script directory for host runs.
    """
    name = name.strip()
    if not name:
        raise ValueError("config name must be non-empty.")
    if not name.endswith(".template"):
        name = name + ".template"

    container_path = f"/ws/ota_configs/{name}"
    if os.path.exists(container_path):
        return container_path
    here = os.path.dirname(os.path.realpath(__file__))
    return os.path.join(here, name)


def _resolved_address(value: str, label: str) -> str:
    address = value.strip()
    if not address:
        raise ValueError(f"{label} must be a non-empty resolved address.")
    return address


def _resolved_spdp_interval(value: str) -> str:
    interval = value.strip().lower()
    if not interval:
        raise ValueError("SPDP interval must be a non-empty duration.")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s)?", interval)
    if not match:
        raise ValueError("SPDP interval must use seconds or milliseconds, e.g. '30s' or '500ms'.")
    number = float(match.group(1))
    if number <= 0.0:
        raise ValueError("SPDP interval must be > 0.")
    return interval


def _resolve_peer_ips(value: str) -> list:
    seen = set()
    unique = []
    for address in value.split(","):
        address = address.strip()
        if not address or address in seen:
            continue
        seen.add(address)
        unique.append(address)
    if not unique:
        raise ValueError("Peer address must contain at least one resolved address.")
    return unique


PEER_BLOCK_OPEN = "<!--peer-block-->"
PEER_BLOCK_CLOSE = "<!--/peer-block-->"
PEER_BLOCK_PATTERN = re.compile(
    re.escape(PEER_BLOCK_OPEN) + r"(.*?)" + re.escape(PEER_BLOCK_CLOSE),
    re.DOTALL,
)


def _expand_peer_block(content: str, peer_ips: list) -> str:
    """Substitute #peer.

    Single peer: simple string replacement of every '#peer' with the IP.

    Multiple peers: the template MUST delimit the per-peer region with
    '<!--peer-block-->...<!--/peer-block-->' markers. The region between the
    markers is duplicated once per peer IP (markers stripped), keeping the
    indentation of the marker line so duplicates align.
    """
    occurrences = content.count("#peer")
    if occurrences == 0:
        return content

    has_markers = PEER_BLOCK_OPEN in content
    if has_markers and (PEER_BLOCK_CLOSE not in content):
        raise RuntimeError(
            f"Template has '{PEER_BLOCK_OPEN}' without matching '{PEER_BLOCK_CLOSE}'."
        )

    if len(peer_ips) == 1 and not has_markers:
        return content.replace("#peer", peer_ips[0])

    if not has_markers:
        raise RuntimeError(
            "Multiple peers requested but template has no peer-block markers. "
            f"Wrap the per-peer region with '{PEER_BLOCK_OPEN}' and "
            f"'{PEER_BLOCK_CLOSE}' (e.g. around the <locator>...</locator> "
            "block) to enable multi-peer expansion."
        )

    matches = list(PEER_BLOCK_PATTERN.finditer(content))
    if len(matches) != 1:
        raise RuntimeError(
            f"Template must contain exactly one peer-block region; found {len(matches)}."
        )
    m = matches[0]
    block = m.group(1)
    if "#peer" not in block:
        raise RuntimeError("Peer-block region does not contain '#peer'.")
    if content.count("#peer") != block.count("#peer"):
        raise RuntimeError("All '#peer' placeholders must live inside the peer-block region.")

    # Preserve the indentation of the marker line so duplicated blocks align.
    line_start = content.rfind("\n", 0, m.start()) + 1
    indent = content[line_start:m.start()]
    sep = ("\n" + indent) if not indent.strip() else ""

    rendered = sep.join(block.replace("#peer", ip) for ip in peer_ips)
    return content[:m.start()] + rendered + content[m.end():]


def main(
    config: str,
    host_ip: str = None,
    peer: str = None,
    easy_mode_ip: str = None,
    spdp_interval: str = DEFAULT_SPDP_INTERVAL,
) -> str:
    path = _template_path(config)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Template not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    found = set(PLACEHOLDER_PATTERN.findall(content))
    unknown = found - KNOWN_PLACEHOLDERS
    if unknown:
        raise RuntimeError(
            f"Template '{os.path.basename(path)}' contains unsupported placeholders: "
            f"{sorted(unknown)}. Supported: {sorted(KNOWN_PLACEHOLDERS)}."
        )

    if "#host_ip" in found:
        if not host_ip:
            raise RuntimeError(
                f"Template '{os.path.basename(path)}' uses #host_ip but --host-ip was not provided."
            )
        content = content.replace("#host_ip", _resolved_address(host_ip, "Host IP"))

    if "#easy_mode_ip" in found:
        if not easy_mode_ip:
            raise RuntimeError(
                f"Template '{os.path.basename(path)}' uses #easy_mode_ip but --easy-mode-ip was not provided."
            )
        content = content.replace(
            "#easy_mode_ip", _resolved_address(easy_mode_ip, "Easy Mode IP")
        )

    if "#spdp_interval" in found:
        content = content.replace("#spdp_interval", _resolved_spdp_interval(spdp_interval))

    if "#peer" in found:
        if not peer:
            raise RuntimeError(
                f"Template '{os.path.basename(path)}' uses #peer but --peer was not provided."
            )
        content = _expand_peer_block(content, _resolve_peer_ips(peer))

    return content


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Resolve placeholders in an OTA DDS XML template."
    )
    parser.add_argument(
        "-c", "--config", required=True,
        help="Template name (e.g. fastdds_v1.xml, cyclonedds.xml, fastdds_easy_mode.xml).",
    )
    parser.add_argument(
        "-i", "--host-ip", dest="host_ip",
        help="Resolved local host address (#host_ip).",
    )
    parser.add_argument(
        "-p", "--peer", dest="peer",
        help="Resolved peer address, or comma-separated addresses (#peer).",
    )
    parser.add_argument(
        "-e", "--easy-mode-ip", dest="easy_mode_ip",
        help="Resolved Fast DDS Easy Mode address (#easy_mode_ip).",
    )
    parser.add_argument(
        "--spdp-interval",
        dest="spdp_interval",
        default=DEFAULT_SPDP_INTERVAL,
        help=f"CycloneDDS SPDP interval duration (#spdp_interval, default: {DEFAULT_SPDP_INTERVAL}).",
    )
    args = parser.parse_args()
    print(main(**{k: v for k, v in vars(args).items() if v is not None}))
