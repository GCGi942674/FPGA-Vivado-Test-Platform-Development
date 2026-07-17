#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PJTEST_ROOT="${PJTEST_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
RUNTIME_DIR="${PJTEST_RUNTIME_DIR:-$SCRIPT_DIR/tmp}"
SLOT_ROOT="${PJTEST_WORKER_SLOT_ROOT:-$SCRIPT_DIR/worker_slots}"
LOG_DIR="${PJTEST_WORKER_SERVICE_LOG_DIR:-$PJTEST_ROOT/logs}"
PID_FILE="${PJTEST_WORKER_PID_FILE:-$RUNTIME_DIR/pjtest_worker.pid}"
LOG_FILE="${PJTEST_WORKER_SERVICE_LOG:-$LOG_DIR/worker_service.log}"

mkdir -p "$RUNTIME_DIR" "$SLOT_ROOT" "$LOG_DIR"

export PJTEST_WORKER_SLOT_ROOT="$SLOT_ROOT"
export PJTEST_WORKER_PROCESS_LOCK_DIR="${PJTEST_WORKER_PROCESS_LOCK_DIR:-$RUNTIME_DIR}"
export RUN_SH_LOCK_DIR="${RUN_SH_LOCK_DIR:-$RUNTIME_DIR}"

is_worker_process() {
    local pid="$1"
    local command_line
    command_line="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    case "$command_line" in
        *"$SCRIPT_DIR/worker.py"*) return 0 ;;
        *) return 1 ;;
    esac
}

if [ -s "$PID_FILE" ]; then
    old_pid="$(cat "$PID_FILE")"
    if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
        if is_worker_process "$old_pid"; then
            echo "PJTest worker is already running. pid=$old_pid"
            ps -fp "$old_pid"
            exit 1
        fi
        echo "Ignoring stale PID file owned by another process. pid=$old_pid" >&2
    fi
    rm -f "$PID_FILE"
fi

nohup "${PJTEST_PYTHON:-python3}" "$SCRIPT_DIR/worker.py" "$@" >>"$LOG_FILE" 2>&1 &
worker_pid=$!
printf '%s\n' "$worker_pid" > "$PID_FILE.tmp"
mv -f "$PID_FILE.tmp" "$PID_FILE"

sleep 2
if ! kill -0 "$worker_pid" 2>/dev/null; then
    echo "PJTest worker failed to start. See: $LOG_FILE" >&2
    rm -f "$PID_FILE"
    tail -n 30 "$LOG_FILE" 2>/dev/null || true
    exit 1
fi

echo "PJTest worker started. pid=$worker_pid log=$LOG_FILE"
ps -fp "$worker_pid"
