#!/bin/bash

# Print commands and their arguments as they are executed.
set -x

SESSION_DIR=example/5_sized_payload
IDENTITY=a
rosotacom --session-dir $SESSION_DIR --identity $IDENTITY
