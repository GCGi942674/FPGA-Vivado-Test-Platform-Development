#!/bin/bash
set -euo pipefail

# Delete PJTest log files older than the retention window.
#
# Safe defaults:
#   * dry-run unless --apply is supplied;
#   * never follows symlinks;
#   * never deletes a file that is still open by a process;
#   * only scans PJTest/logs and known log artifacts below worker_slots.

SCRIPT_PATH="${BASH_SOURCE[0]}"
case "$SCRIPT_PATH" in
    */*) SCRIPT_PARENT="${SCRIPT_PATH%/*}" ;;
    *) SCRIPT_PARENT="." ;;
esac
SCRIPT_DIR="$(cd "$SCRIPT_PARENT" && pwd)"
PJTEST_ROOT="${PJTEST_ROOT:-$SCRIPT_DIR}"
RETENTION_DAYS="${PJTEST_LOG_RETENTION_DAYS:-30}"
APPLY=0
VERBOSE=0
EXTRA_ROOTS=()

usage() {
    cat <<'USAGE'
Usage: cleanup_logs.sh [options]

Options:
  --apply             Actually delete files (default is dry-run)
  --days <N>          Retain files modified within N days (default: 30)
  --root <path>       Also clean every regular file below this explicit log root
                      (may be repeated)
  --verbose           Print every retained/open file decision
  -h, --help          Show this help

Examples:
  ./cleanup_logs.sh
  ./cleanup_logs.sh --apply --days 30
  ./cleanup_logs.sh --apply --root /home/xiaonan/Share/zw_cache/run_log
USAGE
}

require_value() {
    if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
        echo "[ERROR] missing value for $1" >&2
        exit 2
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --apply)
            APPLY=1
            shift
            ;;
        --days)
            require_value "$1" "${2:-}"
            RETENTION_DAYS="$2"
            shift 2
            ;;
        --root)
            require_value "$1" "${2:-}"
            EXTRA_ROOTS+=("$2")
            shift 2
            ;;
        --verbose)
            VERBOSE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[ERROR] unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$RETENTION_DAYS" in
    ''|*[!0-9]*)
        echo "[ERROR] --days must be a positive integer" >&2
        exit 2
        ;;
esac
if [ "$RETENTION_DAYS" -le 0 ]; then
    echo "[ERROR] --days must be greater than zero" >&2
    exit 2
fi

if [ "$APPLY" -eq 1 ] && ! command -v lsof >/dev/null 2>&1; then
    echo "[ERROR] lsof is required in --apply mode so open logs are never deleted" >&2
    exit 1
fi

FILES_FOUND=0
FILES_DELETED=0
FILES_OPEN=0
BYTES_RECLAIMABLE=0
BYTES_DELETED=0

allocated_bytes() {
    local blocks
    blocks=$(stat -c '%b' -- "$1" 2>/dev/null || echo 0)
    echo $((blocks * 512))
}

is_file_open() {
    lsof -t -- "$1" >/dev/null 2>&1
}

handle_candidate() {
    local file="$1"
    local bytes
    FILES_FOUND=$((FILES_FOUND + 1))
    bytes=$(allocated_bytes "$file")

    if command -v lsof >/dev/null 2>&1 && is_file_open "$file"; then
        FILES_OPEN=$((FILES_OPEN + 1))
        echo "[WARN] skip open file: $file" >&2
        return 0
    fi

    BYTES_RECLAIMABLE=$((BYTES_RECLAIMABLE + bytes))
    if [ "$APPLY" -eq 1 ]; then
        rm -f -- "$file"
        FILES_DELETED=$((FILES_DELETED + 1))
        BYTES_DELETED=$((BYTES_DELETED + bytes))
        [ "$VERBOSE" -eq 0 ] || echo "[DELETE] $file"
    else
        echo "[DRY-RUN] $file"
    fi
}

scan_all_files() {
    local root="$1"
    [ -d "$root" ] || return 0
    echo "[INFO] scan log root: $root"
    while IFS= read -r -d '' file; do
        handle_candidate "$file"
    done < <(find -P "$root" -xdev -type f -mtime "+$RETENTION_DAYS" -print0)
}

scan_worker_slot_logs() {
    local root="$1"
    [ -d "$root" ] || return 0
    echo "[INFO] scan worker slot logs: $root"
    while IFS= read -r -d '' file; do
        handle_candidate "$file"
    done < <(
        find -P "$root" -xdev -type f -mtime "+$RETENTION_DAYS" \
            \( -name 'run' -o -path '*/vivado_runner/runtime/logs/*' \) \
            -print0
    )
}

remove_empty_directories() {
    local root="$1"
    [ "$APPLY" -eq 1 ] || return 0
    [ -d "$root" ] || return 0
    find -P "$root" -xdev -depth -mindepth 1 -type d -empty \
        -mtime "+$RETENTION_DAYS" -delete
}

scan_all_files "$PJTEST_ROOT/logs"
scan_worker_slot_logs "$PJTEST_ROOT/worker/worker_slots"

for root in "${EXTRA_ROOTS[@]}"; do
    if [[ "$root" != /* ]]; then
        echo "[ERROR] --root must be an absolute path: $root" >&2
        exit 2
    fi
    if [ ! -d "$root" ]; then
        echo "[ERROR] --root is not a directory: $root" >&2
        exit 2
    fi
    case "$root" in
        /|/home|/home/|/usr|/var|/tmp)
            echo "[ERROR] refusing broad --root target: $root" >&2
            exit 2
            ;;
    esac
    case "/${root#/}/" in
        */log/|*/logs/|*/run_log/|*/run_logs/)
            ;;
        *)
            echo "[ERROR] --root must end in log, logs, run_log, or run_logs: $root" >&2
            exit 2
            ;;
    esac
    scan_all_files "$root"
done

remove_empty_directories "$PJTEST_ROOT/logs"
for root in "${EXTRA_ROOTS[@]}"; do
    remove_empty_directories "$root"
done

echo "[INFO] mode=$([ "$APPLY" -eq 1 ] && echo apply || echo dry-run) retention_days=$RETENTION_DAYS"
echo "[INFO] candidates=$FILES_FOUND deleted=$FILES_DELETED open_skipped=$FILES_OPEN"
echo "[INFO] reclaimable_bytes=$BYTES_RECLAIMABLE deleted_bytes=$BYTES_DELETED"

if command -v lsof >/dev/null 2>&1; then
    deleted_open=$(lsof +L1 2>/dev/null | grep -F "$PJTEST_ROOT" || true)
    if [ -n "$deleted_open" ]; then
        echo "[WARN] deleted files are still held open; stop the listed processes to release space:" >&2
        printf '%s\n' "$deleted_open" >&2
    fi
fi
