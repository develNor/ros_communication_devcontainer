#!/usr/bin/env python3

import argparse
import os
import sys

ws_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.append(ws_dir)

from ota_configs.utils import process_template
from session.content.get_data_dict_entries import main as get_data_dict_entries


def _template_path(name: str) -> str:
    container_path = f"/ws/ota_configs/{name}"
    if os.path.exists(container_path):
        return container_path
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), name)


def _resolve_single_ip(value: str, label: str) -> str:
    resolved = get_data_dict_entries(value)
    if len(resolved) != 1:
        raise ValueError(f"{label} must resolve to exactly one IP address.")
    return resolved[0]


def _render_peer_locators(peers: str) -> str:
    peer_ips = get_data_dict_entries(peers)

    seen = set()
    unique_peer_ips = []
    for peer_ip in peer_ips:
        if peer_ip in seen:
            continue
        seen.add(peer_ip)
        unique_peer_ips.append(peer_ip)

    blocks = []
    for peer_ip in unique_peer_ips:
        blocks.append(
            "\n".join(
                [
                    "            <locator>",
                    "              <udpv4>",
                    f"                <address>{peer_ip}</address>",
                    "              </udpv4>",
                    "            </locator>",
                ]
            )
        )
    return "\n".join(blocks)


def main(host_ip: str, peers: str) -> str:
    host_ip_resolved = _resolve_single_ip(host_ip, "Host IP")
    peer_locators = _render_peer_locators(peers)

    return process_template(
        template_path=_template_path("fastdds.xml.template"),
        substitutions={
            "#host_ip": host_ip_resolved,
            "#peers": peer_locators,
        },
        remove_comments_flag=False,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--host_ip", required=True)
    parser.add_argument("-p", "--peers", required=True)
    args = parser.parse_args()

    result = main(**{k: v for k, v in vars(args).items() if v is not None})
    print(result)
