#!/bin/bash

SUMMARY_TXT="$REPORT_DIR/runTime_Summary"
FAILED_TXT="$REPORT_DIR/list_fail_to_run"
STAT_TXT="$REPORT_DIR/stat_summary"
EXECUTION_TXT="$REPORT_DIR/execution_report.txt"
EXECUTION_JSON="$REPORT_DIR/execution_report.json"
TIMEOUT_TXT="$WORKSPACE_ROOT/timeout_list"

sort_reports() {
    # Sort runtime summary by runtime number if the format contains one.
    if [ -f "$SUMMARY_TXT" ]; then
        sort -k2 -n "$SUMMARY_TXT" -o "$SUMMARY_TXT"
    fi

    # Sort failed list alphabetically.
    if [ -f "$FAILED_TXT" ]; then
        sort "$FAILED_TXT" -o "$FAILED_TXT"
    fi

    # Keep timeout report stable and unique.
    if [ -f "$TIMEOUT_TXT" ]; then
        sort -u "$TIMEOUT_TXT" -o "$TIMEOUT_TXT"
    fi
}

refresh_reports() {
    python3 "$PROJECT_ROOT/lib/python/summarize.py" \
        --status-root "$STATUS_DIR" \
        --case-list "$CASE_LIST_FILE" \
        --summary "$SUMMARY_TXT" \
        --failed "$FAILED_TXT" \
        --stat "$STAT_TXT" \
        --text-report "$EXECUTION_TXT" \
        --json-report "$EXECUTION_JSON" \
        --timeout-list "$TIMEOUT_TXT" \
        --workspace-root "$WORKSPACE_ROOT" \
        --enabled-modules "$enabled_modules" \
        --time-limit "$TIME_LIMIT" \
        --bg-max "$BG_MAX" \
        --host-name "$(get_host_name)" \
        --svn-version "$(get_svn_version)" \
        --flow-config "$FLOW_CONFIG_ABS"

    sort_reports

    # During a copied run, keep the Share report directory updated in place.
    if [ "${ENABLE_COPY_REPORTS:-0}" -eq 1 ] && declare -F copy_live_reports >/dev/null 2>&1; then
        copy_live_reports || true
    fi
}

finalize_reports() {
    refresh_reports

    local stamp archive_dir
    stamp=$(date '+%Y%m%d_%H%M%S')
    archive_dir="$ARCHIVE_DIR/$stamp"
    mkdir -p "$archive_dir"

    cp -f "$SUMMARY_TXT" \
          "$FAILED_TXT" \
          "$STAT_TXT" \
          "$EXECUTION_TXT" \
          "$EXECUTION_JSON" \
          "$archive_dir/" 2>/dev/null || true

    # timeout_list intentionally stays under WORKSPACE_ROOT and is not copied to Share.
}

print_final_summary() {
    log_info "Latest summary: $SUMMARY_TXT"
    log_info "Latest fail list: $FAILED_TXT"
    log_info "Latest stat file: $STAT_TXT"
    log_info "Timeout list: $TIMEOUT_TXT"

    if [ -f "$EXECUTION_TXT" ]; then
        cat "$EXECUTION_TXT" || true
    else
        log_warn "Execution report not found: $EXECUTION_TXT"
    fi

    return 0
}
