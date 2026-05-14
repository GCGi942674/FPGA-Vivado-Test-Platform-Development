#!/bin/bash

copy_file_if_exists() {
    local src="$1"
    local dst_dir="$2"
    [ -f "$src" ] || return 0

    cp -f "$src" "$dst_dir/"
    chmod 777 "$dst_dir/$(basename "$src")" 2>/dev/null || true
}

copy_summary_reports() {
    local server_name svn_version timestamp version_dir report_dst_dir tmp_dir result_path_file detail_path_file
    server_name=$(get_host_name)
    svn_version=$(get_svn_version)
    timestamp=$(date '+%Y%m%d_%H%M%S')

    version_dir="$REPORT_DST_DIR/$svn_version"
    mkdir -p "$version_dir" || { log_error "Cannot create version directory: $version_dir"; return 1; }
    chmod 777 "$REPORT_DST_DIR" "$version_dir" 2>/dev/null || true

    report_dst_dir="$version_dir/${server_name}_${timestamp}"
    tmp_dir="$report_dst_dir.tmp"

    mkdir -p "$tmp_dir" || { log_error "Cannot create report directory: $tmp_dir"; return 1; }
    chmod 777 "$tmp_dir" 2>/dev/null || true

    copy_file_if_exists "$SUMMARY_TXT" "$tmp_dir"
    copy_file_if_exists "$FAILED_TXT" "$tmp_dir"
    copy_file_if_exists "$STAT_TXT" "$tmp_dir"
    copy_file_if_exists "$EXECUTION_TXT" "$tmp_dir"
    copy_file_if_exists "$EXECUTION_JSON" "$tmp_dir"

    chmod -R 777 "$tmp_dir" 2>/dev/null || true
    mv "$tmp_dir" "$report_dst_dir"
    chmod -R 777 "$report_dst_dir" 2>/dev/null || true

    result_path_file="$REPORT_DIR/last_result_path.txt"
    detail_path_file="$REPORT_DIR/last_detail_path.txt"

    echo "$report_dst_dir" > "$result_path_file"
    echo "$report_dst_dir/execution_report.txt" > "$detail_path_file"

    chmod 777 "$result_path_file" "$detail_path_file" 2>/dev/null || true

    log_info "Copied reports to: $report_dst_dir"
    log_info "Result path recorded in: $result_path_file"
}