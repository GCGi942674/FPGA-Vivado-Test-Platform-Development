#!/bin/bash

WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PROJECT_ROOT="$WORKSPACE_ROOT/vivado_runner"
RUNTIME_DIR="$PROJECT_ROOT/runtime"
LOG_DIR="$RUNTIME_DIR/logs"
STATUS_DIR="$RUNTIME_DIR/status"
TMP_DIR="$RUNTIME_DIR/tmp"
REPORT_DIR="$RUNTIME_DIR/reports/latest"
ARCHIVE_DIR="$RUNTIME_DIR/reports/archive"
CACHE_DIR="$RUNTIME_DIR/cache"
RUN_LOG_FILE="$LOG_DIR/run.log"

log_ts() { date '+%Y-%m-%d %H:%M:%S'; }
_log_line() {
    local level="$1"
    shift

    local msg
    msg=$(printf '[%s] [%s] %s\n' "$(log_ts)" "$level" "$*")

    {
        flock -x 200
        printf '%s\n' "$msg" >> "$RUN_LOG_FILE"
        printf '%s\n' "$msg"
    } 200>"$RUN_LOG_FILE.lock"
}

log_info() {
    _log_line "INFO" "$@"
}

log_warn() {
    _log_line "WARN" "$@" >&2
}

log_error() {
    _log_line "ERROR" "$@" >&2
}

normalize_path() {
    local path="$1"
    if [ -d "$path" ]; then
        (cd "$path" && pwd)
    else
        local parent
        parent=$(cd "$(dirname "$path")" 2>/dev/null && pwd) || return 1
        echo "$parent/$(basename "$path")"
    fi
}

get_host_name() {
    local server_name
    server_name=$(hostname -s 2>/dev/null)
    [ -n "$server_name" ] || server_name=$(hostname 2>/dev/null)
    [ -n "$server_name" ] || server_name="unknown_host"
    echo "$server_name"
}

get_svn_version() {
    # Backward-compatible function name.
    # In distributed runs this returns the selected GalaxCore binary build
    # revision from worker metadata, not necessarily the SVN workspace revision.
    local ver=""
    local info_file=""

    if [ -n "${GALAXCORE_BUILD_REVISION:-}" ]; then
        ver="$GALAXCORE_BUILD_REVISION"
    elif [ -n "${GALAXCORE_REVISION:-}" ]; then
        ver="$GALAXCORE_REVISION"
    else
        if [ -n "${GALAXCORE_BUILD_INFO:-}" ] && [ -f "$GALAXCORE_BUILD_INFO" ]; then
            info_file="$GALAXCORE_BUILD_INFO"
        elif [ -f "$WORKSPACE_ROOT/.galaxcore_build_info" ]; then
            info_file="$WORKSPACE_ROOT/.galaxcore_build_info"
        fi

        if [ -n "$info_file" ]; then
            ver=$(awk '$1 == "GALAXCORE_BUILD_REVISION" {print $2; exit}' "$info_file")
        fi
    fi

    if [ -z "$ver" ] && command -v svn >/dev/null 2>&1; then
        ver=$(cd "$WORKSPACE_ROOT" 2>/dev/null && svn info 2>/dev/null | awk '/^Revision:/ {print $2}')
    fi

    ver="${ver#r}"
    ver="${ver#R}"

    if [ -z "$ver" ]; then
        echo "unknown_svn"
    else
        echo "r${ver}"
    fi
}

sanitize_relpath() {
    local value="$1"
    value="${value#./}"
    echo "$value" | sed 's#^/##' | sed 's#[^A-Za-z0-9._/-]#_#g'
}

safe_remove() {
    local target="$1"
    [ -e "$target" ] || return 0
    rm -rf "$target"
}

init_runtime_dirs() {
    mkdir -p "$LOG_DIR" "$STATUS_DIR" "$TMP_DIR" "$REPORT_DIR" "$ARCHIVE_DIR" "$CACHE_DIR"
    : > "$RUN_LOG_FILE"
}

write_kv_file() {
    local file="$1"
    shift
    : > "$file"
    while [ "$#" -gt 1 ]; do
        printf '%s=%q\n' "$1" "$2" >> "$file"
        shift 2
    done
}
