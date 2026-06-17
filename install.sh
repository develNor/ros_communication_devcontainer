#!/usr/bin/env bash
set -euo pipefail

# Install rosotacom into a checkout-local virtual environment.
#
# Usage:
#   ./install.sh
#   ./install.sh --global-symlink   # optional legacy compatibility symlinks
#
# Optional env vars:
#   VENV_DIR=.venv
#   ROS2DOCKER_SPEC='ros2docker>=0.1.2,<0.2'
#   BIN_DIR=~/.local/bin

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$SCRIPT_DIR"
VENV_DIR="${VENV_DIR:-"$ROOT_DIR/.venv"}"
ROS2DOCKER_SPEC="${ROS2DOCKER_SPEC:-"ros2docker>=0.1.2,<0.2"}"
BIN_DIR="${BIN_DIR:-"$HOME/.local/bin"}"

INSTALL_GLOBAL_SYMLINKS=false
for arg in "$@"; do
  case "$arg" in
    --global-symlink|--global-symlinks)
      INSTALL_GLOBAL_SYMLINKS=true
      ;;
    -h|--help)
      sed -n '1,18p' "$0"
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$ROOT_DIR/pyproject.toml" ]]; then
  echo "ERROR: pyproject.toml not found in $ROOT_DIR" >&2
  exit 1
fi

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install "$ROS2DOCKER_SPEC"
"$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR"

echo "Installed rosotacom into: $VENV_DIR"
echo
echo "Activate it with:"
echo "  source \"$VENV_DIR/bin/activate\""
echo
echo "Try:"
echo "  rosotacom --version"
echo "  python -m rosotacom --version"
echo "  rosotacom doctor"
echo "  rosotacom examples create ./rosotacom_examples && cd ./rosotacom_examples"
echo "  rosotacom smoke    # ./rosotacom.yaml is auto-discovered from the current dir"
echo
echo "With no project nearby, rosotacom falls back to a built-in example, so"
echo "'rosotacom smoke' works with zero setup. Pin a default with:"
echo "  rosotacom config set project ./rosotacom.yaml --global"
echo
echo "For a released version instead of this checkout, use: pipx install rosotacom"
echo "For contributor checks, install dev tooling with:"
echo "  just setup"

if [[ "$INSTALL_GLOBAL_SYMLINKS" == true ]]; then
  mkdir -p "$BIN_DIR"
  ln -sf "$VENV_DIR/bin/rosotacom" "$BIN_DIR/rosotacom"
  ln -sf "$VENV_DIR/bin/start_rosotacom" "$BIN_DIR/start_rosotacom"
  ln -sf "$VENV_DIR/bin/stop_rosotacom" "$BIN_DIR/stop_rosotacom"
  echo
  echo "Installed legacy global symlinks into: $BIN_DIR"
fi
