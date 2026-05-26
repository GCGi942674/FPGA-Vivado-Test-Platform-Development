#!/bin/bash

SUMMARY_TXT="$REPORT_DIR/runTime_Summary"
FAILED_TXT="$REPORT_DIR/list_fail_to_run"
STAT_TXT="$REPORT_DIR/stat_summary"
EXECUTION_TXT="$REPORT_DIR/execution_report.txt"
EXECUTION_JSON="$REPORT_DIR/execution_report.json"

sort_reports() {

    # Sort runtime summary by runtime number
    if [ -f "$SUMMARY_TXT" ]; then
        sort -k2 -n "$SUMMARY_TXT" -o "$SUMMARY_TXT"
    fi

    # Sort failed list alphabetically
    if [ -f "$FAILED_TXT" ]; then
        sort "$FAILED_TXT" -o "$FAILED_TXT"
    fi
}

finalize_reports() {
    python3 "$PROJECT_ROOT/lib/python/summarize.py" \
        --status-root "$STATUS_DIR" \
        --case-list "$CASE_LIST_FILE" \
        --summary "$SUMMARY_TXT" \
        --failed "$FAILED_TXT" \
        --stat "$STAT_TXT" \
        --text-report "$EXECUTION_TXT" \
        --json-report "$EXECUTION_JSON" \
        --enabled-modules "$enabled_modules" \
        --time-limit "$TIME_LIMIT" \
        --bg-max "$BG_MAX" \
        --host-name "$(get_host_name)" \
        --svn-version "$(get_svn_version)" \
        --flow-config "$FLOW_CONFIG_ABS"

    local stamp archive_dir
    stamp=$(date '+%Y%m%d_%H%M%S')
    archive_dir="$ARCHIVE_DIR/$stamp"
    mkdir -p "$archive_dir"
    sort_reports
    cp -f "$SUMMARY_TXT" "$FAILED_TXT" "$STAT_TXT" "$EXECUTION_TXT" "$EXECUTION_JSON" "$archive_dir/" 2>/dev/null || true

    # Keep only 3 final artifacts locally
    # rm -f "$EXECUTION_TXT" "$EXECUTION_JSON" 2>/dev/null || true
}

print_final_summary() {
    log_info "Latest summary: $SUMMARY_TXT"
    log_info "Latest fail list: $FAILED_TXT"
    log_info "Latest stat file: $STAT_TXT"

   if [ -f "$EXECUTION_TXT" ]; then
        cat "$EXECUTION_TXT" || true
    else
        log_warn "Execution report not found: $EXECUTION_TXT"
    fi

    return 0
}
