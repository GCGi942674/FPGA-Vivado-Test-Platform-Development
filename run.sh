#!/bin/bash
set -euo pipefail

# Prevent the same Linux user from running multiple run.sh jobs at the same time.
# This does not block other users on the same machine.
RUN_SH_LOCK_FILE="${RUN_SH_LOCK_FILE:-/tmp/galaxcore_run_${USER}.lock}"
exec 200>"$RUN_SH_LOCK_FILE"

if ! flock -n 200; then
    echo "[ERROR] Current user already has a run.sh task running: $RUN_SH_LOCK_FILE"
    exit 75
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INTERNAL_ROOT="$SCRIPT_DIR/vivado_runner"
DEFAULT_FLOW_CONFIG="$SCRIPT_DIR/flow_config"

source "$INTERNAL_ROOT/lib/bash/common.sh"
source "$INTERNAL_ROOT/lib/bash/args.sh"
source "$INTERNAL_ROOT/lib/bash/config.sh"
source "$INTERNAL_ROOT/lib/bash/discover.sh"
source "$INTERNAL_ROOT/lib/bash/judge.sh"
source "$INTERNAL_ROOT/lib/bash/worker.sh"
source "$INTERNAL_ROOT/lib/bash/scheduler.sh"
source "$INTERNAL_ROOT/lib/bash/report.sh"
source "$INTERNAL_ROOT/lib/bash/copy.sh"

main() {
    trap 'handle_interrupt' INT TERM

    parse_args "$@"

    if [ -z "${FLOW_CONFIG_PATH:-}" ]; then
        FLOW_CONFIG_PATH="$DEFAULT_FLOW_CONFIG"
    fi

    init_runtime_dirs

    load_default_config
    apply_cli_overrides
    load_flow_config

    # Merge copy switches from CLI and flow_config.
    # After this point, all copy logic only checks ENABLE_COPY_REPORTS.
    if [ "${ENABLE_COPY_FROM_FLOW:-0}" -eq 1 ]; then
        ENABLE_COPY_REPORTS=1
    fi

    validate_runtime_config

    log_info "Workspace root: $SCRIPT_DIR"
    log_info "Input target: $INPUT_TARGET"
    log_info "Flow config: $FLOW_CONFIG_ABS"
    log_info "Runtime root: $RUNTIME_DIR"

    discover_cases "$INPUT_TARGET"

    local case_count
    case_count=$(count_cases)
    if [ "$case_count" -le 0 ]; then
        log_error "No valid run.tcl cases found"
        exit 1
    fi

    log_info "Discovered $case_count case(s)"

    # Create the Share report directory before the first case starts.
    if [ "${ENABLE_COPY_REPORTS:-0}" -eq 1 ]; then
        init_live_report_dir
    fi

    # Generate the first version of reports immediately.
    # Later scheduler.sh should call refresh_reports after each finished case.
    refresh_reports || true

    dispatch_cases
    finalize_reports

    if [ "${ENABLE_COPY_REPORTS:-0}" -eq 1 ]; then
        copy_summary_reports
    else
        log_info "Skip copying reports. Enable --copy or set 'enable_copy 1' in flow_config to export summaries."
    fi

    print_final_summary
}

main "$@"
exit 0