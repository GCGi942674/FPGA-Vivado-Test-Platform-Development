#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
WATCHER_SCRIPT=${GALAXCORE_WATCHER_SCRIPT:-"${SCRIPT_DIR}/build_Galaxcore.py"}
RUN_DIR=${AUTOMATION_CRON_RUN_DIR:-"${SCRIPT_DIR}/run"}
PID_FILE=${AUTOMATION_CRON_PID_FILE:-"${RUN_DIR}/build_Galaxcore.pid"}
STOP_TIMEOUT_SEC=${AUTOMATION_CRON_STOP_TIMEOUT_SEC:-30}

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

signal_watcher() {
    local signal_name=$1
    local pid=$2
    local pgid

    pgid=$(ps -o pgid= -p "${pid}" 2>/dev/null | tr -d '[:space:]')
    if [[ ${pgid} == "${pid}" ]]; then
        kill "-${signal_name}" -- "-${pgid}" 2>/dev/null || true
    else
        kill "-${signal_name}" "${pid}" 2>/dev/null || true
    fi
}

if [[ ! ${STOP_TIMEOUT_SEC} =~ ^[0-9]+$ ]]; then
    printf 'ERROR: AUTOMATION_CRON_STOP_TIMEOUT_SEC must be a non-negative integer\n' >&2
    exit 1
fi

pid=""
if [[ -f ${PID_FILE} ]]; then
    candidate=$(tr -d '[:space:]' < "${PID_FILE}")
    if is_watcher_pid "${candidate}"; then
        pid=${candidate}
    else
        rm -f -- "${PID_FILE}"
    fi
fi

if [[ -z ${pid} ]]; then
    mapfile -t existing_pids < <(find_watcher_pids)
    if (( ${#existing_pids[@]} == 0 )); then
        printf 'Not running\n'
        exit 0
    fi
    if (( ${#existing_pids[@]} > 1 )); then
        printf 'ERROR: multiple watcher processes found: %s\n' "${existing_pids[*]}" >&2
        exit 1
    fi
    pid=${existing_pids[0]}
fi

printf 'Stopping: pid=%s\n' "${pid}"
signal_watcher TERM "${pid}"

for ((elapsed = 0; elapsed < STOP_TIMEOUT_SEC; elapsed++)); do
    is_watcher_pid "${pid}" || break
    sleep 1
done

if is_watcher_pid "${pid}"; then
    printf 'Graceful stop timed out after %ss; forcing stop: pid=%s\n' \
        "${STOP_TIMEOUT_SEC}" "${pid}" >&2
    signal_watcher KILL "${pid}"
fi

for ((elapsed = 0; elapsed < 5; elapsed++)); do
    is_watcher_pid "${pid}" || break
    sleep 1
done

if is_watcher_pid "${pid}"; then
    printf 'ERROR: watcher is still running: pid=%s\n' "${pid}" >&2
    exit 1
fi

rm -f -- "${PID_FILE}"
printf 'Stopped: pid=%s\n' "${pid}"
