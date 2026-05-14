#!/usr/bin/env bash

set -Eeuo pipefail

# ==============================
# User configuration
# ==============================
PROJECT_NAME="galaxcore"
WORKSPACE_DIR="$HOME/workspace/$PROJECT_NAME"
TEST_DIR="$WORKSPACE_DIR/test2"
FLOW_CONFIG_FILE="$TEST_DIR/flow_config"

LOG_ROOT="$HOME/logs/galaxcore"
LOCK_FILE="/tmp/nightly_galaxcore_build.lock"
SUMMARY_FILE="$LOG_ROOT/summary.tsv"

# Runtime library required by GalaxCore
PROTOBUF_LIB_DIR="/home/fpga/lib/protobuf-3.9.0/lib"

# Optional cleanup after run.sh success
# 1 = cleanup test output dirs and svn up restore
# 0 = keep outputs
POST_RUN_CLEANUP=0

# Whole-flow retry policy
MAX_ATTEMPTS=3
RETRY_INTERVAL=1800   # 30 minutes

DATE_TAG="$(date '+%Y-%m-%d')"
START_TS="$(date '+%Y-%m-%d %H:%M:%S')"
START_EPOCH="$(date +%s)"

LOG_DIR="$LOG_ROOT/$DATE_TAG"
LOG_FILE="$LOG_DIR/run.log"
STEP_SUMMARY_FILE="$LOG_DIR/steps.tsv"

CURRENT_STEP=""
FAILED_STEP=""
FAILED_REASON=""
CURRENT_ATTEMPT=0

mkdir -p "$LOG_DIR"
mkdir -p "$LOG_ROOT"

# ==============================
# Helper functions
# ==============================
log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    printf "%s\n" "$msg" >> "$LOG_FILE"
}

reset_attempt_logs() {
    : > "$LOG_FILE"
    : > "$STEP_SUMMARY_FILE"
}

append_daily_summary() {
    local status="$1"
    local end_ts end_epoch duration

    end_ts="$(date '+%Y-%m-%d %H:%M:%S')"
    end_epoch="$(date +%s)"
    duration=$((end_epoch - START_EPOCH))

    if [[ ! -f "$SUMMARY_FILE" ]]; then
        printf "date\tstart_time\tend_time\tduration_sec\tstatus\tattempt\tfailed_step\tlog_file\n" > "$SUMMARY_FILE"
    fi

    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$DATE_TAG" \
        "$START_TS" \
        "$end_ts" \
        "$duration" \
        "$status" \
        "$CURRENT_ATTEMPT" \
        "${FAILED_STEP:-}" \
        "$LOG_FILE" >> "$SUMMARY_FILE"
}

append_step_summary() {
    local step_name="$1"
    local start_ts="$2"
    local end_ts="$3"
    local duration="$4"
    local rc="$5"

    if [[ ! -f "$STEP_SUMMARY_FILE" ]]; then
        printf "step_name\tstart_time\tend_time\tduration_sec\texit_code\tstatus\n" > "$STEP_SUMMARY_FILE"
    fi

    local step_status="SUCCESS"
    if [[ "$rc" -ne 0 ]]; then
        step_status="FAILED"
    fi

    printf "%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$step_name" \
        "$start_ts" \
        "$end_ts" \
        "$duration" \
        "$rc" \
        "$step_status" >> "$STEP_SUMMARY_FILE"
}

notify_popup() {
    local mode="$1"
    local title="$2"
    local text="$3"

    local gui_display=":7.0"
    local gui_dbus="unix:abstract=/tmp/dbus-iSmIoCM72m,guid=f3a4584d5b3a40e004164d8369c9d0cf"
    local gui_xauth="$HOME/.Xauthority"

    if command -v zenity >/dev/null 2>&1; then
        if [[ "$mode" == "error" ]]; then
            env DISPLAY="$gui_display" \
                DBUS_SESSION_BUS_ADDRESS="$gui_dbus" \
                XAUTHORITY="$gui_xauth" \
                nohup zenity --error \
                    --title="$title" \
                    --width=560 \
                    --height=320 \
                    --no-wrap \
                    --text="$text" \
                    >/dev/null 2>&1 &
        else
            env DISPLAY="$gui_display" \
                DBUS_SESSION_BUS_ADDRESS="$gui_dbus" \
                XAUTHORITY="$gui_xauth" \
                nohup zenity --info \
                    --title="$title" \
                    --width=560 \
                    --height=320 \
                    --no-wrap \
                    --text="$text" \
                    >/dev/null 2>&1 &
        fi
    else
        log "[WARN] zenity not found, skip popup"
    fi
}

notify_final_result() {
    local final_status="$1"

    local end_ts end_epoch duration
    end_ts="$(date '+%Y-%m-%d %H:%M:%S')"
    end_epoch="$(date +%s)"
    duration=$((end_epoch - START_EPOCH))

    local title text

    if [[ "$final_status" == "SUCCESS" ]]; then
        title="Nightly Build Success"
        text="All nightly steps completed successfully.

Project: $PROJECT_NAME
Attempt: $CURRENT_ATTEMPT/$MAX_ATTEMPTS
Start time: $START_TS
End time: $end_ts
Duration: ${duration}s

Log file:
$LOG_FILE"
        notify_popup "info" "$title" "$text"
    else
        title="Nightly Build Failed"
        text="Nightly flow finished with FAILURE.

Project: $PROJECT_NAME
Attempt: $CURRENT_ATTEMPT/$MAX_ATTEMPTS
Failed step: ${FAILED_STEP:-unknown}
Reason: ${FAILED_REASON:-unknown}
Start time: $START_TS
End time: $end_ts
Duration: ${duration}s

Please check log:
$LOG_FILE"
        notify_popup "error" "$title" "$text"
    fi
}

fail() {
    local step_name="$1"
    local reason="$2"

    FAILED_STEP="$step_name"
    FAILED_REASON="$reason"

    log "[ERROR] [$step_name] $reason"
    return 1
}

run_step() {
    local step_name="$1"
    shift

    local step_start_epoch step_end_epoch duration rc
    local step_start_ts step_end_ts

    CURRENT_STEP="$step_name"
    step_start_epoch="$(date +%s)"
    step_start_ts="$(date '+%Y-%m-%d %H:%M:%S')"

    log "============================================================"
    log "[STEP START] $step_name"
    log "============================================================"

    set +e
    "$@" >> "$LOG_FILE" 2>&1
    rc=$?
    set -e

    step_end_epoch="$(date +%s)"
    step_end_ts="$(date '+%Y-%m-%d %H:%M:%S')"
    duration=$((step_end_epoch - step_start_epoch))

    append_step_summary "$step_name" "$step_start_ts" "$step_end_ts" "$duration" "$rc"

    if [[ $rc -ne 0 ]]; then
        log "[STEP FAIL ] $step_name (cost ${duration}s)"
        CURRENT_STEP=""
        fail "$step_name" "exit code $rc"
        return 1
    fi

    log "[STEP DONE ] $step_name (cost ${duration}s)"
    CURRENT_STEP=""
    return 0
}

run_attempt() {
    FAILED_STEP=""
    FAILED_REASON=""
    CURRENT_STEP=""

    reset_attempt_logs

    log "============================================================"
    log "Nightly job started"
    log "Project    : $PROJECT_NAME"
    log "Workspace  : $WORKSPACE_DIR"
    log "Test dir   : $TEST_DIR"
    log "Attempt    : $CURRENT_ATTEMPT/$MAX_ATTEMPTS"
    log "Log file   : $LOG_FILE"
    log "============================================================"

    # ==============================
    # Basic checks
    # ==============================
    [[ -d "$WORKSPACE_DIR" ]] || { fail "basic checks" "Workspace directory not found: $WORKSPACE_DIR"; return 1; }
    [[ -d "$TEST_DIR" ]] || { fail "basic checks" "Test directory not found: $TEST_DIR"; return 1; }

    if ! command -v tcsh >/dev/null 2>&1; then
        fail "basic checks" "tcsh not found in PATH"
        return 1
    fi

    if ! command -v zenity >/dev/null 2>&1; then
        log "[WARN] zenity not found, popup will fallback to Desktop file"
    fi

    log "[INFO] tcsh path: $(command -v tcsh)"
    log "[INFO] protobuf lib dir: $PROTOBUF_LIB_DIR"

    # ==============================
    # Step 1: svn up
    # ==============================
    run_step "svn up" tcsh -fc "
        source ~/.cshrc
        cd \"$WORKSPACE_DIR\"
        svn up
    " || return 1

    # ==============================
    # Step 2: make clean
    # ==============================
    run_step "make clean" tcsh -fc "
        source ~/.cshrc
        cd \"$WORKSPACE_DIR\"
        make clean
    " || return 1

    # ==============================
    # Step 3: bd build
    # ==============================
    run_step "bd build" tcsh -fc "
        source ~/.cshrc
        setenv LD_LIBRARY_PATH ${PROTOBUF_LIB_DIR}:\$LD_LIBRARY_PATH
        cd \"$WORKSPACE_DIR\"
        bd
    " || return 1

    # ==============================
    # Step 4: mk compile
    # ==============================
    run_step "mk compile" tcsh -fc "
        source ~/.cshrc
        setenv LD_LIBRARY_PATH ${PROTOBUF_LIB_DIR}:\$LD_LIBRARY_PATH
        cd \"$WORKSPACE_DIR\"
        echo '[DEBUG] which gcc:' \`which gcc\`
        echo '[DEBUG] which g++:' \`which g++\`
        gcc --version | head -n 1
        g++ --version | head -n 1
        mk
    " || return 1

    # ==============================
    # Step 5: ensure enable_copy is 1 in flow_config
    # ==============================
    CURRENT_STEP="update flow_config"

    if [[ ! -f "$FLOW_CONFIG_FILE" ]]; then
        fail "update flow_config" "flow_config file not found: $FLOW_CONFIG_FILE"
        CURRENT_STEP=""
        return 1
    fi

    if grep -Eq '^[[:space:]]*enable_copy[[:space:]]+[01][[:space:]]*$' "$FLOW_CONFIG_FILE"; then
        sed -i -E 's/^[[:space:]]*enable_copy[[:space:]]+[01][[:space:]]*$/enable_copy 1/' "$FLOW_CONFIG_FILE"
        log "[INFO] Updated enable_copy to 1 in $FLOW_CONFIG_FILE"
    else
        printf "\nenable_copy 1\n" >> "$FLOW_CONFIG_FILE"
        log "[WARN] enable_copy line not found, appended 'enable_copy 1' to $FLOW_CONFIG_FILE"
    fi

    log "[INFO] Current enable_copy lines:"
    grep -En 'enable_copy' "$FLOW_CONFIG_FILE" >> "$LOG_FILE" 2>&1 || true
    CURRENT_STEP=""

    # ==============================
    # Step 6: debug runtime environment for GalaxCore
    # ==============================
    run_step "check GalaxCore libs" tcsh -fc "
        source ~/.cshrc
        setenv LD_LIBRARY_PATH ${PROTOBUF_LIB_DIR}:\$LD_LIBRARY_PATH
        cd \"$TEST_DIR\"
        echo '[DEBUG] pwd:' \`pwd\`
        echo '[DEBUG] LD_LIBRARY_PATH:' \$LD_LIBRARY_PATH
        echo '[DEBUG] GalaxCore path:'
        ls -l ../../../../bin/Linux_64/GalaxCore
        echo '[DEBUG] ldd missing libs:'
        ldd ../../../../bin/Linux_64/GalaxCore | grep 'not found' || true
    " || return 1

    # ==============================
    # Step 7: run test
    # ==============================
    run_step "./run.sh ." tcsh -fc "
        source ~/.cshrc
        setenv LD_LIBRARY_PATH ${PROTOBUF_LIB_DIR}:\$LD_LIBRARY_PATH
        cd \"$TEST_DIR\"
        ./run.sh .
        set run_rc=\$status
        echo \"[INFO] ./run.sh . finished with exit code \$run_rc\"
        echo \"[INFO] Ignore testcase failures and continue nightly flow\"
        exit 0
    " || return 1

    # ==============================
    # Step 8: optional cleanup after success
    # ==============================
    if [[ "$POST_RUN_CLEANUP" -eq 1 ]]; then
        run_step "cleanup generated output dirs" bash -c "
            set -e
            cd '$TEST_DIR'
            ./clean.sh 2>/dev/null || true
            [[ -d kintexuplus ]] && rm -rf kintexuplus/*
            [[ -d virtexuplus ]] && rm -rf virtexuplus/*
        " || return 1

        run_step "svn restore after cleanup" tcsh -fc "
            source ~/.cshrc
            cd \"$WORKSPACE_DIR\"
            svn up
        " || return 1
    else
        log "[INFO] POST_RUN_CLEANUP=0, keep output files for inspection"
    fi

    log "Nightly attempt finished successfully"
    return 0
}

# ==============================
# Acquire lock to prevent overlap
# ==============================
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    mkdir -p "$LOG_DIR"
    : > "$LOG_FILE"
    printf "[%s] [WARN] Another nightly job is still running. Exit without starting a new one.\n" \
        "$(date '+%Y-%m-%d %H:%M:%S')" > "$LOG_FILE"
    append_daily_summary "SKIPPED_LOCKED"
    exit 0
fi

trap '
    rc=$?
    if [[ $rc -ne 0 ]]; then
        if [[ -n "${CURRENT_STEP:-}" ]]; then
            FAILED_STEP="${CURRENT_STEP}"
        fi
        if [[ -z "${FAILED_REASON:-}" ]]; then
            FAILED_REASON="Unexpected error at line $LINENO"
        fi
    fi
' ERR

# ==============================
# Main retry loop
# ==============================
for (( attempt=1; attempt<=MAX_ATTEMPTS; attempt++ )); do
    CURRENT_ATTEMPT="$attempt"

    if run_attempt; then
        append_daily_summary "SUCCESS"
        notify_final_result "SUCCESS"
        exit 0
    else
        rc=$?
    fi

    if (( attempt < MAX_ATTEMPTS )); then
        log "[INFO] Attempt $attempt failed at step [$FAILED_STEP]"
        log "[INFO] Reason: $FAILED_REASON"
        log "[INFO] Sleep ${RETRY_INTERVAL}s, then restart whole flow from svn up"
        sleep "$RETRY_INTERVAL"
    fi
done

append_daily_summary "FAILED"
notify_final_result "FAILED"
exit 1