#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${PJTEST_RUNTIME_DIR:-$SCRIPT_DIR/tmp}"
PID_FILE="${PJTEST_WORKER_PID_FILE:-$RUNTIME_DIR/pjtest_worker.pid}"
STOP_TIMEOUT="${PJTEST_STOP_TIMEOUT:-40}"
FORCE_TIMEOUT="${PJTEST_FORCE_STOP_TIMEOUT:-5}"

is_worker_process() {
    local pid="$1"
    local command_line
    command_line="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    case "$command_line" in
        *"$SCRIPT_DIR/worker.py"*) return 0 ;;
        *) return 1 ;;
    esac
}

if [ ! -s "$PID_FILE" ]; then
    echo "PJTest worker is not running (PID file not found)."
    exit 0
fi

worker_pid="$(cat "$PID_FILE")"
if ! [[ "$worker_pid" =~ ^[0-9]+$ ]]; then
    echo "Invalid worker PID file: $PID_FILE" >&2
    rm -f "$PID_FILE"
    exit 1
fi

if ! kill -0 "$worker_pid" 2>/dev/null; then
    echo "Removing stale worker PID: $worker_pid"
    rm -f "$PID_FILE"
    exit 0
fi

if ! is_worker_process "$worker_pid"; then
    echo "PID $worker_pid does not belong to this PJTest worker; not sending a signal." >&2
    rm -f "$PID_FILE"
    exit 1
fi

echo "Stopping PJTest worker gracefully. pid=$worker_pid"
kill -TERM "$worker_pid"

elapsed=0
while kill -0 "$worker_pid" 2>/dev/null && [ "$elapsed" -lt "$STOP_TIMEOUT" ]; do
    sleep 1
    elapsed=$((elapsed + 1))
done

if kill -0 "$worker_pid" 2>/dev/null; then
    echo "Graceful stop timed out; requesting forced worker shutdown." >&2
    kill -TERM "$worker_pid" 2>/dev/null || true
    elapsed=0
    while kill -0 "$worker_pid" 2>/dev/null && [ "$elapsed" -lt "$FORCE_TIMEOUT" ]; do
        sleep 1
        elapsed=$((elapsed + 1))
    done
fi

if kill -0 "$worker_pid" 2>/dev/null; then
    echo "Worker did not stop; sending SIGKILL. pid=$worker_pid" >&2
    kill -KILL "$worker_pid" 2>/dev/null || true
    sleep 1
fi

if kill -0 "$worker_pid" 2>/dev/null; then
    echo "Failed to stop PJTest worker. pid=$worker_pid" >&2
    exit 1
fi

rm -f "$PID_FILE"
echo "PJTest worker stopped. pid=$worker_pid"
