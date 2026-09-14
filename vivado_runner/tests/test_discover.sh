#!/bin/bash

set -euo pipefail

TEST_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$TEST_DIR/../lib/bash/common.sh"

TEST_ROOT=$(mktemp -d)
trap 'rm -rf "$TEST_ROOT"' EXIT
WORKSPACE_ROOT="$TEST_ROOT/test2"
TMP_DIR="$TEST_ROOT/runtime"
REPORT_DIR="$TEST_ROOT/reports/r18376/place_design_task"
mkdir -p "$WORKSPACE_ROOT/cases/demo" "$TMP_DIR" "$REPORT_DIR/cases/demo"
mkdir -p "$REPORT_DIR/cases/report_only" "$TEST_ROOT/outside case"
touch "$WORKSPACE_ROOT/cases/demo/run.tcl"
touch "$REPORT_DIR/cases/demo/run.tcl" "$REPORT_DIR/cases/report_only/run.tcl"
touch "$WORKSPACE_ROOT/cases/demo/other.tcl" "$TEST_ROOT/outside case/run.tcl"

source "$TEST_DIR/../lib/bash/discover.sh"

assert_cases() {
    local name="$1"
    shift
    local expected actual
    expected=$(printf '%s\n' "$@" | sort -u)
    actual=$(cat "$CASE_LIST_FILE")
    if [ "$actual" != "$expected" ]; then
        printf 'FAIL %s\nExpected:\n%s\nActual:\n%s\n' "$name" "$expected" "$actual" >&2
        exit 1
    fi
    printf 'PASS %s\n' "$name"
}

# A report outside test2 must select the local case, even if the report
# directory contains another file with the same relative name.
printf 'cases/demo/run.tcl\n' > "$REPORT_DIR/list_pass_to_run"
cd "$TEST_ROOT"
discover_cases "$REPORT_DIR/list_pass_to_run"
assert_cases external_report_local_case "$WORKSPACE_ROOT/cases/demo/run.tcl"

cp "$REPORT_DIR/list_pass_to_run" "$WORKSPACE_ROOT/list_pass_to_run"
discover_cases "$WORKSPACE_ROOT/list_pass_to_run"
assert_cases copied_list_same_case "$WORKSPACE_ROOT/cases/demo/run.tcl"

# Missing local cases must not silently run copies beside the report.
printf 'cases/report_only/run.tcl\n' > "$REPORT_DIR/list_pass_to_run"
discover_cases "$REPORT_DIR/list_pass_to_run"
assert_cases no_report_directory_fallback

printf '%s\n' "$TEST_ROOT/outside case/run.tcl" > "$REPORT_DIR/list_pass_to_run"
discover_cases "$REPORT_DIR/list_pass_to_run"
assert_cases absolute_path_with_spaces "$TEST_ROOT/outside case/run.tcl"

# Keep trimming, comments, validation, deduplication and the final line
# without a newline working when the list is read from an external path.
printf ' # comment\r\n\r\n cases/demo/run.tcl \r\ncases/demo/other.tcl\nmissing/run.tcl\ncases/demo\ncases/demo/run.tcl' > "$REPORT_DIR/list_pass_to_run"
discover_cases "$REPORT_DIR/list_pass_to_run"
assert_cases mixed_list "$WORKSPACE_ROOT/cases/demo/run.tcl"

discover_cases "$WORKSPACE_ROOT/cases/demo/run.tcl"
assert_cases direct_run_tcl "$WORKSPACE_ROOT/cases/demo/run.tcl"

printf 'All discovery tests passed.\n'
