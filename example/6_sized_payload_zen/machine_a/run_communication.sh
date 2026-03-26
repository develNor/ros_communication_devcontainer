#!/bin/bash

# Print commands and their arguments as they are executed.
set -x

SESSION_DIR=example/6_sized_payload_zen
IDENTITY=a
start_rosotacom $SESSION_DIR --identity $IDENTITY
