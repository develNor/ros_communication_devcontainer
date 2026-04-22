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


def main(easy_mode_ip: str) -> str:
    easy_mode_ip_resolved = get_data_dict_entries(easy_mode_ip)
    if len(easy_mode_ip_resolved) != 1:
        raise ValueError("Easy Mode IP must resolve to exactly one IP address.")

    return process_template(
        template_path=_template_path("fastdds_easy_mode.xml.template"),
        substitutions={"#easy_mode_ip": easy_mode_ip_resolved[0]},
        remove_comments_flag=False,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-e", "--easy_mode_ip", required=True)
    args = parser.parse_args()

    result = main(**{k: v for k, v in vars(args).items() if v is not None})
    print(result)
