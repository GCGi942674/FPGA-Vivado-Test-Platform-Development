#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
WATCHER_SCRIPT=${GALAXCORE_WATCHER_SCRIPT:-"${SCRIPT_DIR}/build_Galaxcore.py"}
PYTHON_BIN=${GALAXCORE_PYTHON_BIN:-python3}
RUN_DIR=${AUTOMATION_CRON_RUN_DIR:-"${SCRIPT_DIR}/run"}
LOG_DIR=${AUTOMATION_CRON_LOG_DIR:-"${SCRIPT_DIR}/logs"}
PID_FILE=${AUTOMATION_CRON_PID_FILE:-"${RUN_DIR}/build_Galaxcore.pid"}
LOG_FILE=${AUTOMATION_CRON_LOG_FILE:-"${LOG_DIR}/build_Galaxcore.log"}

is_watcher_pid() {
    local pid=$1
    local arg

    [[ ${pid} =~ ^[0-9]+$ ]] || return 1
    kill -0 "${pid}" 2>/dev/null || return 1
    [[ -r "/proc/${pid}/cmdline" ]] || return 1

    while IFS= read -r -d '' arg; do
        [[ ${arg} == "${WATCHER_SCRIPT}" ]] && return 0
    done < "/proc/${pid}/cmdline"
    return 1
}

find_watcher_pids() {
    local cmdline
    local pid

    for cmdline in /proc/[0-9]*/cmdline; do
        [[ -r ${cmdline} ]] || continue
        pid=${cmdline#/proc/}
        pid=${pid%/cmdline}
        is_watcher_pid "${pid}" && printf '%s\n' "${pid}"
    done
}

if [[ ! -f ${WATCHER_SCRIPT} ]]; then
    printf 'ERROR: watcher script not found: %s\n' "${WATCHER_SCRIPT}" >&2
    exit 1
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    printf 'ERROR: Python executable not found: %s\n' "${PYTHON_BIN}" >&2
    exit 1
fi

mkdir -p -- "${RUN_DIR}" "${LOG_DIR}"

if [[ -f ${PID_FILE} ]]; then
    pid=$(tr -d '[:space:]' < "${PID_FILE}")
    if is_watcher_pid "${pid}"; then
        printf 'Already running: pid=%s log=%s\n' "${pid}" "${LOG_FILE}"
        exit 0
    fi
    rm -f -- "${PID_FILE}"
fi

mapfile -t existing_pids < <(find_watcher_pids)
if (( ${#existing_pids[@]} > 0 )); then
    if (( ${#existing_pids[@]} > 1 )); then
        printf 'ERROR: multiple watcher processes found: %s\n' "${existing_pids[*]}" >&2
        exit 1
    fi
    pid=${existing_pids[0]}
    printf '%s\n' "${pid}" > "${PID_FILE}"
    printf 'Already running; PID file restored: pid=%s log=%s\n' \
        "${pid}" "${LOG_FILE}"
    exit 0
fi

if command -v setsid >/dev/null 2>&1; then
    nohup setsid "${PYTHON_BIN}" "${WATCHER_SCRIPT}" \
        >> "${LOG_FILE}" 2>&1 < /dev/null &
else
    nohup "${PYTHON_BIN}" "${WATCHER_SCRIPT}" \
        >> "${LOG_FILE}" 2>&1 < /dev/null &
fi
pid=$!

tmp_pid_file="${PID_FILE}.$$"
printf '%s\n' "${pid}" > "${tmp_pid_file}"
mv -f -- "${tmp_pid_file}" "${PID_FILE}"

sleep 1
if ! is_watcher_pid "${pid}"; then
    rm -f -- "${PID_FILE}"
    printf 'ERROR: watcher exited during startup; inspect %s\n' "${LOG_FILE}" >&2
    exit 1
fi

printf 'Started: pid=%s log=%s\n' "${pid}" "${LOG_FILE}"
