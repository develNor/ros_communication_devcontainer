#!/usr/bin/env python3

import os

from ros2docker.api import build_run

directory_of_this_script = os.path.dirname(os.path.realpath(__file__))

def run():
    config = f'{directory_of_this_script}/external.ros2docker.json'
    build_run(config_file=config)

if __name__ == "__main__":
    run()