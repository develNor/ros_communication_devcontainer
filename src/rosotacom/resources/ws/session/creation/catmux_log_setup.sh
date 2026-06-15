#!/usr/bin/env bash
# Mirror the current catmux/tmux pane to a per-instance log file.
#
# This file is sourced from catmux common.before_commands. It must be tolerant:
# logging should never prevent a pane command from starting.

if [[ -z "${TMUX_PANE:-}" ]]; then
  echo "[rosotacom_catmux_log] no TMUX_PANE; skipping pane logging" >&2
  return 0 2>/dev/null || exit 0
fi

export RCUTILS_COLORIZED_OUTPUT="${RCUTILS_COLORIZED_OUTPUT:-0}"

ROSOTACOM_CATMUX_LOG_DIR="${ROSOTACOM_CATMUX_LOG_DIR:-${ROSOTACOM_LOGS_DIR:-/tmp/rosotacom_logs}/catmux}"
ROSOTACOM_ROSBAG_DIR="${ROSOTACOM_ROSBAG_DIR:-${ROSOTACOM_INSTANCE_DIR:-/tmp/rosotacom_instance}/rosbags}"
export ROSOTACOM_INSTANCE_DIR ROSOTACOM_CONFIG_DIR ROSOTACOM_CATMUX_LOG_DIR ROSOTACOM_ROSBAG_DIR

if ! command -v tmux >/dev/null 2>&1; then
  echo "[rosotacom_catmux_log] tmux not on PATH; pane logging disabled" >&2
  return 0 2>/dev/null || exit 0
fi

__rosotacom_window="$(tmux display-message -p -t "${TMUX_PANE}" '#{window_name}' 2>/dev/null)"
__rosotacom_window_index="$(tmux display-message -p -t "${TMUX_PANE}" '#{window_index}' 2>/dev/null)"
__rosotacom_pane="$(tmux display-message -p -t "${TMUX_PANE}" '#{pane_index}' 2>/dev/null)"
if [[ -z "${__rosotacom_window}" || -z "${__rosotacom_window_index}" || -z "${__rosotacom_pane}" ]]; then
  echo "[rosotacom_catmux_log] could not query tmux pane id; skipping" >&2
  return 0 2>/dev/null || exit 0
fi

__rosotacom_window_safe="$(printf '%s' "${__rosotacom_window}" | tr -c 'A-Za-z0-9_.-' '_')"
printf -v __rosotacom_window_prefix '%02d' "${__rosotacom_window_index}"
__rosotacom_pane_log_dir="${ROSOTACOM_CATMUX_LOG_DIR}/${__rosotacom_window_prefix}-${__rosotacom_window_safe}"
__rosotacom_pane_log_file="${__rosotacom_pane_log_dir}/${__rosotacom_pane}.log"
export ROSOTACOM_CATMUX_PANE_LOG_DIR="${__rosotacom_pane_log_dir}"
export ROSOTACOM_CATMUX_PANE_LOG_FILE="${__rosotacom_pane_log_file}"

if ! mkdir -p "${__rosotacom_pane_log_dir}" 2>/dev/null; then
  echo "[rosotacom_catmux_log] cannot create ${__rosotacom_pane_log_dir}; skipping" >&2
  return 0 2>/dev/null || exit 0
fi

__rosotacom_strip_ansi="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)/strip_ansi.py"
if [[ -x "${__rosotacom_strip_ansi}" ]] && command -v python3 >/dev/null 2>&1; then
  __rosotacom_pipe_filter="python3 '${__rosotacom_strip_ansi//\'/\'\\\'\'}'"
else
  echo "[rosotacom_catmux_log] strip_ansi.py unavailable; logging raw pane output" >&2
  __rosotacom_pipe_filter="cat"
fi

tmux pipe-pane -o -t "${TMUX_PANE}" \
  "${__rosotacom_pipe_filter} >> '${__rosotacom_pane_log_file//\'/\'\\\'\'}'"

{
  printf '\n--- rosotacom catmux pipe-pane started %s ---\n' "$(date -Iseconds)"
  printf '    window=%s (#%s) pane=%s\n' \
    "${__rosotacom_window}" "${__rosotacom_window_index}" "${__rosotacom_pane}"
  printf '    log_file=%s\n' "${__rosotacom_pane_log_file}"
} >&2

unset __rosotacom_window __rosotacom_window_index __rosotacom_pane \
  __rosotacom_window_safe __rosotacom_window_prefix \
  __rosotacom_pane_log_dir __rosotacom_pane_log_file \
  __rosotacom_strip_ansi __rosotacom_pipe_filter
