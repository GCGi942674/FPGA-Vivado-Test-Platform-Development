#!/bin/bash

CASE_LIST_FILE="$TMP_DIR/case_list.txt"
DEFAULT_CASE_ROOTS=("$WORKSPACE_ROOT/kintexplus" "$WORKSPACE_ROOT/virtexplus")

add_case_if_valid() {
    local run_tcl="$1"
    [ -f "$run_tcl" ] || return 0
    [ "$(basename "$run_tcl")" = "run.tcl" ] || return 0

    run_tcl=$(normalize_path "$run_tcl") || return 0
    echo "$run_tcl" >> "$CASE_LIST_FILE"
}

resolve_list_entry_path() {
    local entry="$1"
    local list_dir="$2"

    # absolute path
    if [[ "$entry" = /* ]]; then
        echo "$entry"
        return 0
    fi

    # relative path -> relative to list file directory
    echo "$list_dir/$entry"
}

load_case_list_file() {
    local list_file="$1"
    local list_dir
    local each_case
    local resolved_path

    list_dir=$(cd "$(dirname "$list_file")" && pwd)

    while IFS= read -r each_case || [ -n "$each_case" ]; do
        # trim leading/trailing spaces
        each_case="${each_case#"${each_case%%[![:space:]]*}"}"
        each_case="${each_case%"${each_case##*[![:space:]]}"}"

        [ -z "$each_case" ] && continue
        case "$each_case" in
            \#*) continue ;;
        esac

        resolved_path=$(resolve_list_entry_path "$each_case" "$list_dir")
        add_case_if_valid "$resolved_path"
    done < "$list_file"
}

scan_default_roots_with_keyword() {
    local keyword="$1"
    local root

    for root in "${DEFAULT_CASE_ROOTS[@]}"; do
        [ -d "$root" ] || continue
        if [ -n "$keyword" ] && [ "$keyword" != "." ]; then
            find "$root" -type f -name 'run.tcl' | grep -E "$keyword" | sort >> "$CASE_LIST_FILE" || true
        else
            find "$root" -type f -name 'run.tcl' | sort >> "$CASE_LIST_FILE"
        fi
    done
}

discover_cases() {
    local input="$1"

    : > "$CASE_LIST_FILE"

    if [ -z "$input" ]; then
        log_error "Usage: ./run.sh . | ./run.sh ./path | ./run.sh /abs/path | ./run.sh path/to/run.tcl | ./run.sh case.list"
        return 1
    fi

    # 1) input is a directory -> recursively find run.tcl
    if [ -d "$input" ]; then
        find "$input" -type f -name 'run.tcl' | sort > "$CASE_LIST_FILE"
        return 0
    fi

    # 2) input is a single run.tcl file
    if [ -f "$input" ] && [ "$(basename "$input")" = "run.tcl" ]; then
        add_case_if_valid "$input"
        return 0
    fi

    # 3) input is a list file
    if [ -f "$input" ]; then
        load_case_list_file "$input"
        sort -u "$CASE_LIST_FILE" -o "$CASE_LIST_FILE"
        return 0
    fi

    # 4) optional: treat input as keyword
    # scan_default_roots_with_keyword "$input"
    # sort -u "$CASE_LIST_FILE" -o "$CASE_LIST_FILE"
    # return 0

    log_error "Invalid input: $input"
    return 1
}

count_cases() {
    [ -f "$CASE_LIST_FILE" ] || { echo 0; return 0; }
    wc -l < "$CASE_LIST_FILE" | awk '{print $1}'
}