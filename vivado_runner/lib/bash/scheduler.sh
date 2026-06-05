#!/bin/bash

PID_MAP_FILE="$TMP_DIR/pid_map.txt"
INTERRUPTED=0
DISPATCHED_CASE_COUNT=0
TOTAL_CASE_COUNT=0


count_finished_cases() {
    [ -f "$TMP_DIR/case_results.raw" ] || {
        echo 0
        return 0
    }

    awk -F'|' 'NF >= 2 && $1 != "" { count++ } END { print count + 0 }' "$TMP_DIR/case_results.raw"
}

log_dispatch_progress() {
    local current_index="$1"
    local total_cases="$2"
    local current_case="$3"

    log_info "Progress: [${current_index}/${total_cases}] current=$current_case"
}

is_pid_running() {
    local pid="$1"
    local stat

    kill -0 "$pid" 2>/dev/null || return 1

    stat=$(ps -p "$pid" -o stat= 2>/dev/null | awk '{print $1}')
    [ -n "$stat" ] || return 1

    # A zombie PID already finished and only needs wait/reap.
    case "$stat" in
        Z*) return 1 ;;
    esac

    return 0
}

handle_interrupt() {
    INTERRUPTED=1
    trap '' INT TERM

    log_warn "Interrupted, terminating running jobs..."

    local pid run_tcl case_dir start_ts status_out
    local end_ts runtime_sec case_status_dir result_file

    if [ -f "$PID_MAP_FILE" ]; then
        while IFS='|' read -r pid run_tcl case_dir start_ts status_out; do
            [ -n "$pid" ] || continue

            if is_pid_running "$pid"; then
                kill -TERM -- "-$pid" 2>/dev/null || true
                sleep 1
                kill -KILL -- "-$pid" 2>/dev/null || true
                wait "$pid" 2>/dev/null || true
            fi

            end_ts=$(date '+%s')
            runtime_sec=$((end_ts - start_ts))

            case_status_dir=$(build_case_status_dir "$case_dir")
            mkdir -p "$case_status_dir"
            result_file="$case_status_dir/result.env"

            write_kv_file "$result_file" \
                CASE_DIR "$case_dir" \
                CASE_NAME "$(basename "$case_dir")" \
                RUN_TCL "$run_tcl" \
                STATUS "INTERRUPTED" \
                REASON "USER_INTERRUPT" \
                STAGE "interrupt" \
                START_TS "$start_ts" \
                END_TS "$end_ts" \
                RUNTIME_SEC "$runtime_sec" \
                HOST_NAME "$(get_host_name)" \
                SVN_VERSION "$(get_svn_version)" \
                FLOW_CONFIG "$FLOW_CONFIG_ABS" \
                RET_CODE "130"

            printf 'INTERRUPTED|%s|USER_INTERRUPT\n' "$case_dir" >> "$TMP_DIR/case_results.raw"
        done < "$PID_MAP_FILE"

        : > "$PID_MAP_FILE"
    fi

    # Save the latest partial reports before exiting.
    finalize_reports || true
    log_warn "Exit by Ctrl+C"
    exit 130
}

launch_case_job() {
    local run_tcl="$1"
    local case_dir status_out
    case_dir=$(dirname "$run_tcl")
    status_out="$TMP_DIR/$(echo "$case_dir" | sed 's#[/ ]#_#g').out"

    setsid bash -c '
        source "$1/lib/bash/common.sh"
        source "$1/config/log_keywords.conf"
        source "$1/lib/bash/judge.sh"
        source "$1/lib/bash/worker.sh"
        WORKSPACE_ROOT="$2"
        PROJECT_ROOT="$1"
        RUNTIME_DIR="$3"
        LOG_DIR="$4"
        STATUS_DIR="$5"
        TMP_DIR="$6"
        REPORT_DIR="$7"
        ARCHIVE_DIR="$8"
        CACHE_DIR="$9"
        GALAXCORE_BIN="${10}"
        FLOW_CONFIG_ABS="${11}"
        FLOW_ARGS_STR="${12}"
        IFS="|" read -r -a FLOW_ARGS <<< "$FLOW_ARGS_STR"
        run_one_case "${13}"
    ' _ "$PROJECT_ROOT" "$WORKSPACE_ROOT" "$RUNTIME_DIR" "$LOG_DIR" "$STATUS_DIR" "$TMP_DIR" "$REPORT_DIR" "$ARCHIVE_DIR" "$CACHE_DIR" "$GALAXCORE_BIN" "$FLOW_CONFIG_ABS" "$(IFS='|'; echo "${FLOW_ARGS[*]}")" "$run_tcl" > "$status_out" 2>&1 &

    local pid=$!
    printf '%s|%s|%s|%s|%s\n' "$pid" "$run_tcl" "$case_dir" "$(date '+%s')" "$status_out" >> "$PID_MAP_FILE"
}

count_running_jobs() {
    [ -f "$PID_MAP_FILE" ] || {
        echo 0
        return 0
    }

    awk -F'|' 'NF >= 5 && $1 != "" { count++ } END { print count + 0 }' "$PID_MAP_FILE"
}

reap_finished_jobs() {
    local new_map="$TMP_DIR/pid_map.next"
    local pid map_run_tcl map_case_dir start_ts status_out

    : > "$new_map"

    [ -f "$PID_MAP_FILE" ] || return 0

    while IFS='|' read -r pid map_run_tcl map_case_dir start_ts status_out; do
        [ -n "$pid" ] || continue

        if is_pid_running "$pid"; then
            printf '%s|%s|%s|%s|%s\n' "$pid" "$map_run_tcl" "$map_case_dir" "$start_ts" "$status_out" >> "$new_map"
            continue
        fi

        wait "$pid" 2>/dev/null || true

        if [ -f "$status_out" ]; then
            cat "$status_out" >> "$TMP_DIR/case_results.raw"
        else
            printf 'FAIL|%s|MISSING_STATUS_OUTPUT\n' "$map_case_dir" >> "$TMP_DIR/case_results.raw"
        fi

        # Refresh reports immediately after one case finishes.
        refresh_reports || true
    done < "$PID_MAP_FILE"

    mv "$new_map" "$PID_MAP_FILE"
}

kill_timeout_jobs() {
    local now new_map end_ts runtime_sec result_file case_status_dir
    local pid map_run_tcl map_case_dir start_ts status_out

    now=$(date '+%s')
    new_map="$TMP_DIR/pid_map.timeout"
    : > "$new_map"

    [ -f "$PID_MAP_FILE" ] || return 0

    while IFS='|' read -r pid map_run_tcl map_case_dir start_ts status_out; do
        [ -n "$pid" ] || continue

        if ! is_pid_running "$pid"; then
            # Do not drop finished jobs here.
            # Let reap_finished_jobs consume status_out and append case_results.raw.
            printf '%s|%s|%s|%s|%s\n' "$pid" "$map_run_tcl" "$map_case_dir" "$start_ts" "$status_out" >> "$new_map"
            continue
        fi

        if [ $((now - start_ts)) -ge "$TIME_LIMIT" ]; then
            log_warn "Timeout reached: $map_case_dir"

            kill -TERM -- "-$pid" 2>/dev/null || true
            sleep 1
            kill -KILL -- "-$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true

            end_ts=$(date '+%s')
            runtime_sec=$((end_ts - start_ts))

            case_status_dir=$(build_case_status_dir "$map_case_dir")
            mkdir -p "$case_status_dir"
            result_file="$case_status_dir/result.env"

            write_kv_file "$result_file" \
                CASE_DIR "$map_case_dir" \
                CASE_NAME "$(basename "$map_case_dir")" \
                RUN_TCL "$map_run_tcl" \
                STATUS "TIMEOUT" \
                REASON "TIME_LIMIT_REACHED" \
                STAGE "timeout" \
                START_TS "$start_ts" \
                END_TS "$end_ts" \
                RUNTIME_SEC "$runtime_sec" \
                HOST_NAME "$(get_host_name)" \
                SVN_VERSION "$(get_svn_version)" \
                FLOW_CONFIG "$FLOW_CONFIG_ABS" \
                RET_CODE "124"

            echo "TIMEOUT" > "$map_case_dir/.run_status"
            echo "TIME_LIMIT_REACHED" > "$map_case_dir/.run_reason"
            echo "$runtime_sec" > "$map_case_dir/.run_runtime"
            echo "timeout" > "$map_case_dir/.run_stage"
            echo "124" > "$map_case_dir/.run_ret"

            printf 'TIMEOUT|%s|TIME_LIMIT_REACHED\n' "$map_case_dir" >> "$TMP_DIR/case_results.raw"

            # Refresh reports immediately after one case times out.
            refresh_reports || true
        else
            printf '%s|%s|%s|%s|%s\n' "$pid" "$map_run_tcl" "$map_case_dir" "$start_ts" "$status_out" >> "$new_map"
        fi
    done < "$PID_MAP_FILE"

    mv "$new_map" "$PID_MAP_FILE"
}

dispatch_cases() {
    local case_run_tcl

    : > "$PID_MAP_FILE"
    : > "$TMP_DIR/case_results.raw"

    TOTAL_CASE_COUNT=$(count_cases)
    DISPATCHED_CASE_COUNT=0

    while IFS= read -r case_run_tcl || [ -n "$case_run_tcl" ]; do
        reap_finished_jobs
        kill_timeout_jobs
        reap_finished_jobs

        case_run_tcl="${case_run_tcl%$'\r'}"
        # log_info "DEBUG raw case entry: <$case_run_tcl>"

        if [ -z "$case_run_tcl" ]; then
            log_warn "Skip empty case entry from case_list"
            continue
        fi

        case "$case_run_tcl" in
            */run.tcl)
                ;;
            *)
                log_warn "Skip invalid case entry: $case_run_tcl"
                continue
                ;;
        esac

        if [ ! -f "$case_run_tcl" ]; then
            log_warn "Skip missing run.tcl: $case_run_tcl"
            continue
        fi

        while [ "$(count_running_jobs)" -ge "$BG_MAX" ]; do
            sleep 1
            reap_finished_jobs
            kill_timeout_jobs
            reap_finished_jobs
        done

        DISPATCHED_CASE_COUNT=$((DISPATCHED_CASE_COUNT + 1))
        log_dispatch_progress "$DISPATCHED_CASE_COUNT" "$TOTAL_CASE_COUNT" "$case_run_tcl"

        log_info "Dispatching: $case_run_tcl"
        launch_case_job "$case_run_tcl"
    done < "$CASE_LIST_FILE"

    while [ "$(count_running_jobs)" -gt 0 ]; do
        sleep 1
        reap_finished_jobs
        kill_timeout_jobs
        reap_finished_jobs
    done

    reap_finished_jobs
}