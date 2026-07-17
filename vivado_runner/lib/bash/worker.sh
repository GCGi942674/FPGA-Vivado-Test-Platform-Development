#!/bin/bash

remove_case_artifacts() {
    local case_dir="$1"
    rm -f "$case_dir/run" \
          "$case_dir/output.bit" \
          "$case_dir/output.msk" \
          "$case_dir/output.msd" \
          "$case_dir/output.rbd" \
          "$case_dir/output.bgn" \
          "$case_dir/output_timing.rpt" \
          "$case_dir/output_timing.rpx" \
          "$case_dir/output_cmp.bgn" \
          "$case_dir/golden_cmp.bgn" \
          "$case_dir/output.dcp" \
          "$case_dir/output_report_timing_summary.log" \
          "$case_dir/mis_bit.txt" \
          "$case_dir/mis_msk.txt" \
          "$case_dir/mis_checksum.txt" \
          "$case_dir/mis_report_utilization.txt" \
          "$case_dir/mis_rpx.txt" \
          "$case_dir/mis_msd.txt" \
          "$case_dir/mis_rbd.txt" \
          "$case_dir/result_bgn.log" \
          "$case_dir/mis_timing_summary.txt" \
          "$case_dir/.run_status" \
          "$case_dir/.run_reason" \
          "$case_dir/.run_runtime" \
          "$case_dir/.run_stage" \
          "$case_dir/.run_ret"
}

build_case_status_dir() {
    local case_dir="$1"
    local rel
    rel=$(python3 - <<PY
import os
root = os.path.realpath("$WORKSPACE_ROOT")
case_dir = os.path.realpath("$case_dir")
try:
    print(os.path.relpath(case_dir, root))
except Exception:
    print(os.path.basename(case_dir))
PY
)
    rel=$(sanitize_relpath "$rel")
    echo "$STATUS_DIR/$rel"
}

run_one_case() {
    local run_tcl="$1"
    local case_dir case_name host_name svn_version start_ts end_ts runtime_sec ret_code
    local case_status_dir case_log_file result_file child_pid=""

    on_case_interrupt() {
        if [ -n "$child_pid" ]; then
            kill -TERM "$child_pid" 2>/dev/null || true
            sleep 1
            kill -KILL "$child_pid" 2>/dev/null || true
        fi
        exit 130
    }

    trap 'on_case_interrupt' INT TERM

    case_dir=$(dirname "$run_tcl")
    case_name=$(basename "$case_dir")
    host_name=$(get_host_name)
    svn_version=$(get_svn_version)
    case_status_dir=$(build_case_status_dir "$case_dir")
    mkdir -p "$case_status_dir"
    case_log_file="$case_status_dir/run.log"
    result_file="$case_status_dir/result.env"

    remove_case_artifacts "$case_dir"
    start_ts=$(date '+%s')

    (
        cd "$case_dir" || exit 127
        if [ -x "$GALAXCORE_BIN" ]; then
            "$GALAXCORE_BIN" "$(basename "$run_tcl")" "${FLOW_ARGS[@]}" > run 2>&1
        else
            bash "$(basename "$run_tcl")" > run 2>&1
        fi
    ) &
    child_pid=$!
    wait "$child_pid"
    ret_code=$?
    child_pid=""

    cp -f "$case_dir/run" "$case_log_file" 2>/dev/null || true

    end_ts=$(date '+%s')
    runtime_sec=$((end_ts - start_ts))

    if [ "$ret_code" -eq 124 ] || [ "$ret_code" -eq 137 ] || [ "$ret_code" -eq 143 ] || [ "$ret_code" -eq 130 ]; then
        CASE_STATUS="TIMEOUT"
        CASE_REASON="PROCESS_KILLED"
        CASE_STAGE="timeout"
    elif [ "$ret_code" -ne 0 ]; then
        judge_case_result "$case_dir" "$case_log_file"

        if [ "$CASE_STATUS" = "PASS" ]; then
            CASE_STATUS="FAIL"
            CASE_REASON="NON_ZERO_EXIT_WITH_PASS_SIGNATURE"
        elif [ "$CASE_REASON" = "UNKNOWN" ] || [ "$CASE_REASON" = "PASS" ]; then
            CASE_REASON="NON_ZERO_EXIT"
        fi
    else
        judge_case_result "$case_dir" "$case_log_file"
    fi

    write_kv_file "$result_file" \
        CASE_DIR "$case_dir" \
        CASE_NAME "$case_name" \
        RUN_TCL "$run_tcl" \
        STATUS "$CASE_STATUS" \
        REASON "$CASE_REASON" \
        STAGE "$CASE_STAGE" \
        START_TS "$start_ts" \
        END_TS "$end_ts" \
        RUNTIME_SEC "$runtime_sec" \
        HOST_NAME "$host_name" \
        SVN_VERSION "$svn_version" \
        FLOW_CONFIG "$FLOW_CONFIG_ABS" \
        RET_CODE "$ret_code"

    echo "$CASE_STATUS" > "$case_dir/.run_status"
    echo "$CASE_REASON" > "$case_dir/.run_reason"
    echo "$runtime_sec" > "$case_dir/.run_runtime"
    echo "$CASE_STAGE" > "$case_dir/.run_stage"
    echo "$ret_code" > "$case_dir/.run_ret"

    printf '%s|%s|%s\n' "$CASE_STATUS" "$case_dir" "$CASE_REASON"
}
