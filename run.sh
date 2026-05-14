#!/bin/bash
set -euo pipefail

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
    init_runtime_dirs
    trap 'handle_interrupt' INT TERM

    parse_args "$@"

    if [ -z "${FLOW_CONFIG_PATH:-}" ]; then
        FLOW_CONFIG_PATH="$DEFAULT_FLOW_CONFIG"
    fi

    load_default_config
    apply_cli_overrides
    load_flow_config
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

    dispatch_cases
    finalize_reports

    if [ "$ENABLE_COPY_REPORTS" -eq 1 ]; then
        copy_summary_reports
    else
        log_info "Skip copying reports. Use --copy to export final summary."
    fi

    print_final_summary
}

main "$@"
exit 0