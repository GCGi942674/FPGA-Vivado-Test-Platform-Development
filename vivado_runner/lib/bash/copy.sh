#!/bin/bash

LIVE_REPORT_DIR=""

copy_file_if_exists() {
    local src="$1"
    local dst_dir="$2"
    [ -f "$src" ] || return 0

    cp -f "$src" "$dst_dir/"
    chmod 777 "$dst_dir/$(basename "$src")" 2>/dev/null || true
}

copy_report_atomic() {
    local src="$1"
    local dst_dir="$2"
    local base tmp

    [ -f "$src" ] || return 0
    [ -d "$dst_dir" ] || return 1

    base=$(basename "$src")
    tmp="$dst_dir/.${base}.tmp.$$"

    cp -f "$src" "$tmp"
    mv -f "$tmp" "$dst_dir/$base"
    chmod 777 "$dst_dir/$base" 2>/dev/null || true
}

init_live_report_dir() {
    local server_name svn_version timestamp version_dir
    local result_path_file detail_path_file

    [ "${ENABLE_COPY_REPORTS:-0}" -eq 1 ] || return 0

    if [ -n "${LIVE_REPORT_DIR:-}" ] && [ -d "$LIVE_REPORT_DIR" ]; then
        return 0
    fi

    server_name=$(get_host_name)
    svn_version=$(get_svn_version)
    timestamp=$(date '+%Y%m%d_%H%M%S')

    version_dir="$REPORT_DST_DIR/$svn_version"
    mkdir -p "$version_dir" || {
        log_error "Cannot create version directory: $version_dir"
        return 1
    }
    chmod 777 "$REPORT_DST_DIR" "$version_dir" 2>/dev/null || true

    LIVE_REPORT_DIR="$version_dir/${server_name}_${timestamp}"
    mkdir -p "$LIVE_REPORT_DIR" || {
        log_error "Cannot create report directory: $LIVE_REPORT_DIR"
        return 1
    }
    chmod 777 "$LIVE_REPORT_DIR" 2>/dev/null || true

    # Create visible report files before the first case finishes.
    : > "$LIVE_REPORT_DIR/$(basename "$SUMMARY_TXT")"
    : > "$LIVE_REPORT_DIR/$(basename "$FAILED_TXT")"
    : > "$LIVE_REPORT_DIR/$(basename "$STAT_TXT")"
    : > "$LIVE_REPORT_DIR/$(basename "$EXECUTION_TXT")"
    printf '{}\n' > "$LIVE_REPORT_DIR/$(basename "$EXECUTION_JSON")"

    chmod 777 "$LIVE_REPORT_DIR"/* 2>/dev/null || true

    result_path_file="$REPORT_DIR/last_result_path.txt"
    detail_path_file="$REPORT_DIR/last_detail_path.txt"

    echo "$LIVE_REPORT_DIR" > "$result_path_file"
    echo "$LIVE_REPORT_DIR/execution_report.txt" > "$detail_path_file"

    chmod 777 "$result_path_file" "$detail_path_file" 2>/dev/null || true

    log_info "Live report directory: $LIVE_REPORT_DIR"
}

copy_live_reports() {
    [ "${ENABLE_COPY_REPORTS:-0}" -eq 1 ] || return 0

    init_live_report_dir || return 1

    copy_report_atomic "$SUMMARY_TXT" "$LIVE_REPORT_DIR"
    copy_report_atomic "$FAILED_TXT" "$LIVE_REPORT_DIR"
    copy_report_atomic "$STAT_TXT" "$LIVE_REPORT_DIR"
    copy_report_atomic "$EXECUTION_TXT" "$LIVE_REPORT_DIR"
    copy_report_atomic "$EXECUTION_JSON" "$LIVE_REPORT_DIR"

    chmod -R 777 "$LIVE_REPORT_DIR" 2>/dev/null || true
}

copy_summary_reports() {
    copy_live_reports

    if [ -n "${LIVE_REPORT_DIR:-}" ]; then
        log_info "Copied reports to: $LIVE_REPORT_DIR"
        log_info "Result path recorded in: $REPORT_DIR/last_result_path.txt"
    fi
}
