#!/bin/bash

# Print commands and their arguments as they are executed.
set -x

SESSION_DIR=example/5_sized_payload
IDENTITY=a
start_rosotacom $SESSION_DIR --identity $IDENTITY
