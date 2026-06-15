#!/usr/bin/env bash
set -euo pipefail
set -x

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLE_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
CONFIG="${ROSOTACOM_CONFIG:-$EXAMPLE_ROOT/rosotacom.yaml}"

rosotacom start --rosotacom-config "$CONFIG" 6_sized_payload_zen --identity a --force
