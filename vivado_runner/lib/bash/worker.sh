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
          "$case_dir/.run_ret" \
          "$case_dir/.run_log_limit"
}

is_result_artifact_pass() {
    case "$CASE_REASON" in
        CHECKSUM_COMPARE_PASS|REPORT_UTILIZATION_COMPARE_PASS|RPX_COMPARE_PASS|COMPARE_ARTIFACTS_PASS)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

run_case_with_log_watchdog() {
    local run_tcl="$1"
    local max_log_bytes command_pid command_rc current_size log_limit_hit=0

    max_log_bytes=$((MAX_CASE_LOG_MB * 1024 * 1024))

    # Give the case its own process group.  The watchdog and signal handler can
    # then terminate GalaxCore and every child it started without touching the
    # runner process group.
    if [ -x "$GALAXCORE_BIN" ]; then
        setsid "$GALAXCORE_BIN" "$(basename "$run_tcl")" "${FLOW_ARGS[@]}" > run 2>&1 &
    else
        setsid bash "$(basename "$run_tcl")" > run 2>&1 &
    fi
    command_pid=$!

    watched_case_is_running() {
        local process_state

        kill -0 "$command_pid" 2>/dev/null || return 1
        process_state=$(ps -p "$command_pid" -o stat= 2>/dev/null | awk '{print $1}')
        [ -n "$process_state" ] || return 1

        case "$process_state" in
            Z*) return 1 ;;
        esac

        return 0
    }

    stop_case_process_group() {
        kill -TERM -- "-$command_pid" 2>/dev/null || true

        local wait_count=0
        while watched_case_is_running && [ "$wait_count" -lt 10 ]; do
            sleep 0.1
            wait_count=$((wait_count + 1))
        done

        if watched_case_is_running; then
            kill -KILL -- "-$command_pid" 2>/dev/null || true
        fi
    }

    on_watched_case_interrupt() {
        stop_case_process_group
        wait "$command_pid" 2>/dev/null || true
        exit 130
    }

    trap 'on_watched_case_interrupt' INT TERM

    while watched_case_is_running; do
        if [ -f run ]; then
            current_size=$(wc -c < run)
            if [ "$current_size" -ge "$max_log_bytes" ]; then
                log_limit_hit=1
                stop_case_process_group
                break
            fi
        fi
        sleep 0.2
    done

    set +e
    wait "$command_pid"
    command_rc=$?
    set -e
    trap - INT TERM

    # The process may finish between two polls.  Enforce the limit against the
    # final file as well so a short, fast burst cannot escape the watchdog.
    if [ "$log_limit_hit" -eq 0 ] && [ -f run ]; then
        current_size=$(wc -c < run)
        if [ "$current_size" -ge "$max_log_bytes" ]; then
            log_limit_hit=1
        fi
    fi

    if [ "$log_limit_hit" -eq 1 ]; then
        : > .run_log_limit
        printf '\n[ERROR] run log reached MAX_CASE_LOG_MB=%s; process was terminated\n' \
            "$MAX_CASE_LOG_MB" >> run
        return 153
    fi

    return "$command_rc"
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

        if [ "${MAX_CASE_LOG_MB:-0}" -gt 0 ]; then
            # GalaxCore must write run directly because checksum_cmp reads the
            # file while the case is still running.  A `GalaxCore | head > run`
            # pipeline lets the reader outrun the separate writer on a busy
            # host.  Monitor the directly-written file instead and terminate
            # the whole case process group if it reaches the configured limit.
            run_case_with_log_watchdog "$run_tcl"
            exit $?
        fi

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

    if [ "$ret_code" -eq 153 ] && [ -f "$case_dir/.run_log_limit" ]; then
        CASE_STATUS="FAIL"
        CASE_REASON="LOG_SIZE_LIMIT"
        CASE_STAGE="log_limit"
    elif [ "$ret_code" -eq 124 ] || [ "$ret_code" -eq 137 ] || [ "$ret_code" -eq 143 ] || [ "$ret_code" -eq 130 ]; then
        CASE_STATUS="TIMEOUT"
        CASE_REASON="PROCESS_KILLED"
        CASE_STAGE="timeout"
    elif [ "$ret_code" -ne 0 ]; then
        judge_case_result "$case_dir" "$case_log_file"

        # These compare modules define their result exclusively through the
        # freshly generated mis_* artifact. Preserve the raw process return
        # code in result.env, but do not let it override an authoritative PASS.
        if [ "$CASE_STATUS" = "PASS" ] && ! is_result_artifact_pass; then
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
