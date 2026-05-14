#!/bin/bash

DEFAULT_CONFIG_FILE="$PROJECT_ROOT/config/default.conf"
LOG_KEYWORDS_FILE="$PROJECT_ROOT/config/log_keywords.conf"
DEFAULT_FLOW_CONFIG_FILE="$WORKSPACE_ROOT/flow_config"

enabled_modules=""
FLOW_ARGS=()

append_enabled_module() {
    if [ -z "$enabled_modules" ]; then
        enabled_modules="$1"
    else
        enabled_modules="$enabled_modules, $1"
    fi
}

load_default_config() {
    [ -f "$DEFAULT_CONFIG_FILE" ] || { log_error "Missing default config: $DEFAULT_CONFIG_FILE"; exit 1; }
    # shellcheck disable=SC1090
    source "$DEFAULT_CONFIG_FILE"
    [ -f "$LOG_KEYWORDS_FILE" ] && source "$LOG_KEYWORDS_FILE"

    if [ "$KEEP_RUNTIME" -eq 0 ]; then
        safe_remove "$LOG_DIR"
        safe_remove "$STATUS_DIR"
        safe_remove "$TMP_DIR"
        safe_remove "$REPORT_DIR"
        safe_remove "$ARCHIVE_DIR"
        safe_remove "$CACHE_DIR"
        init_runtime_dirs
    fi
}

apply_cli_overrides() {
    [ -n "$CLI_BG_MAX" ] && BG_MAX="$CLI_BG_MAX"
    [ -n "$CLI_TIME_LIMIT" ] && TIME_LIMIT="$CLI_TIME_LIMIT"
    [ -n "$CLI_REPORT_DST_DIR" ] && REPORT_DST_DIR="$CLI_REPORT_DST_DIR"
    [ -n "$CLI_GALAXCORE_BIN" ] && GALAXCORE_BIN="$CLI_GALAXCORE_BIN"
    return 0
}

is_enabled() {
    local key="$1"
    grep -Eq "^[[:space:]]*$key[[:space:]]+1([[:space:]]*)$" "$FLOW_CONFIG_ABS"
}

ensure_flow_config_complete() {

    local cfg="$1"
    local tpl="$PROJECT_ROOT/templates/flow_config.template"

    [ -f "$tpl" ] || {
        log_error "flow_config template not found: $tpl"
        exit 1
    }

    # no flow_config -> copy template
    if [ ! -f "$cfg" ]; then
        cp "$tpl" "$cfg"
        log_warn "flow_config not found, created from template: $cfg"
        return 0
    fi

    # append missing keys from template
    while IFS= read -r line; do

        line="${line%%#*}"
        line="$(echo "$line" | xargs)"

        [ -z "$line" ] && continue

        key="${line%% *}"

        if ! grep -Eq "^[[:space:]]*$key[[:space:]]+" "$cfg"; then
            printf "%s\n" "$line" >> "$cfg"
            log_warn "flow_config missing '$key', appended default: $line"
        fi

    done < "$tpl"
}

load_flow_config() {
    local candidate="${FLOW_CONFIG_PATH:-$DEFAULT_FLOW_CONFIG_FILE}"
    FLOW_CONFIG_ABS=$(normalize_path "$candidate") || { log_error "Invalid flow_config path: $candidate"; exit 1; }
    ensure_flow_config_complete "$FLOW_CONFIG_ABS"

    FLOW_ARGS=()
    FLOW_COPY_ARGS=()
    enabled_modules=""
    ENABLE_COPY_FROM_FLOW=0

    for key in report_timing_summary opt_design place_design place_design_from_syn phys_opt_design route_design route_design_from_place write_checkpoint write_bitstream bit_cmp msk_cmp bgn_cmp checksum_cmp dcp_cmp; do
        if is_enabled "$key"; then
            FLOW_ARGS+=("$key")
            append_enabled_module "$key"
        fi
    done

    if is_enabled "enable_copy"; then
        ENABLE_COPY_FROM_FLOW=1
        FLOW_COPY_ARGS=(
            "--copy"
            "--"
            "--report-dst"
            "/home/xiaonan/Share/zw_cache/run_logs/"
        )
    fi

    [ -n "$enabled_modules" ] || enabled_modules="none"
    return 0
}

validate_runtime_config() {
    case "$BG_MAX" in ''|*[!0-9]*) log_error "Invalid BG_MAX: $BG_MAX"; exit 1 ;; esac
    case "$TIME_LIMIT" in ''|*[!0-9]*) log_error "Invalid TIME_LIMIT: $TIME_LIMIT"; exit 1 ;; esac
    GALAXCORE_BIN=$(normalize_path "$GALAXCORE_BIN") || { log_error "Invalid GalaxCore path: $GALAXCORE_BIN"; exit 1; }
    REPORT_DST_DIR=$(normalize_path "$REPORT_DST_DIR") || { log_error "Invalid report destination: $REPORT_DST_DIR"; exit 1; }
    mkdir -p "$REPORT_DST_DIR" || { log_error "Cannot create report destination: $REPORT_DST_DIR"; exit 1; }
}
