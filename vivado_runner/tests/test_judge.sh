#!/bin/bash

set -euo pipefail

TEST_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$TEST_DIR/.." && pwd)

KW_RUNTIME='Runtime:'
KW_FATAL_ERROR='FATAL|Segmentation fault'
KW_DCP_FAIL='DCP_FAIL'
KW_DCP_PASS='DCP_PASS'
KW_DCP_SHAPE_FAIL='DCP Shape Compare FAIL'
KW_DCP_SHAPE_PASS='DCP Shape Compare PASS'

# shellcheck source=../lib/bash/judge.sh
source "$PROJECT_ROOT/lib/bash/judge.sh"

TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT

assert_result() {
    local name="$1"
    local expected_status="$2"
    local expected_reason="$3"

    if [ "$CASE_STATUS" != "$expected_status" ] || [ "$CASE_REASON" != "$expected_reason" ]; then
        printf 'FAIL %-32s expected=%s/%s actual=%s/%s\n' \
            "$name" "$expected_status" "$expected_reason" "$CASE_STATUS" "$CASE_REASON" >&2
        exit 1
    fi
    printf 'PASS %s\n' "$name"
}

new_case() {
    CASE_DIR="$TMP_ROOT/$1"
    mkdir -p "$CASE_DIR"
    RUN_LOG="$CASE_DIR/run"
}

new_case runtime_missing
FLOW_ARGS=(write_bitstream bgn_cmp bit_cmp msk_cmp)
: > "$CASE_DIR/result_bgn.log"
: > "$CASE_DIR/mis_bit.txt"
: > "$CASE_DIR/mis_msk.txt"
printf 'Routing Is Done.\n' > "$RUN_LOG"
judge_case_result "$CASE_DIR" "$RUN_LOG"
assert_result runtime_missing FAIL RUNTIME_MISSING

new_case runtime_not_final
FLOW_ARGS=(write_bitstream bgn_cmp bit_cmp msk_cmp)
: > "$CASE_DIR/result_bgn.log"
: > "$CASE_DIR/mis_bit.txt"
: > "$CASE_DIR/mis_msk.txt"
printf 'Runtime: 73\nlate error\n' > "$RUN_LOG"
judge_case_result "$CASE_DIR" "$RUN_LOG"
assert_result runtime_not_final FAIL RUNTIME_NOT_FINAL

new_case all_compares_pass
FLOW_ARGS=(write_bitstream bgn_cmp bit_cmp msk_cmp)
: > "$CASE_DIR/result_bgn.log"
: > "$CASE_DIR/mis_bit.txt"
: > "$CASE_DIR/mis_msk.txt"
printf 'Routing Is Done.\nRuntime: 73\n' > "$RUN_LOG"
judge_case_result "$CASE_DIR" "$RUN_LOG"
assert_result all_compares_pass PASS COMPARE_ARTIFACTS_PASS

new_case runtime_with_crlf_and_trailing_blank
FLOW_ARGS=()
printf 'Runtime: 73\r\n\r\n' > "$RUN_LOG"
judge_case_result "$CASE_DIR" "$RUN_LOG"
assert_result runtime_with_crlf_and_trailing_blank PASS PASS

new_case bgn_compare_fail
FLOW_ARGS=(write_bitstream bgn_cmp bit_cmp msk_cmp)
printf 'mismatch\n' > "$CASE_DIR/result_bgn.log"
: > "$CASE_DIR/mis_bit.txt"
: > "$CASE_DIR/mis_msk.txt"
printf 'Runtime: 73\n' > "$RUN_LOG"
judge_case_result "$CASE_DIR" "$RUN_LOG"
assert_result bgn_compare_fail FAIL BGN_COMPARE_FAIL

new_case missing_bit_result
FLOW_ARGS=(write_bitstream bgn_cmp bit_cmp msk_cmp)
: > "$CASE_DIR/result_bgn.log"
: > "$CASE_DIR/mis_msk.txt"
printf 'Runtime: 73\n' > "$RUN_LOG"
judge_case_result "$CASE_DIR" "$RUN_LOG"
assert_result missing_bit_result FAIL BIT_RESULT_MISSING

new_case compares_ignored_without_write
FLOW_ARGS=(bgn_cmp bit_cmp msk_cmp)
printf 'Runtime: 73\n' > "$RUN_LOG"
judge_case_result "$CASE_DIR" "$RUN_LOG"
assert_result compares_ignored_without_write PASS PASS

new_case shape_compare_fail
FLOW_ARGS=(read_edif read_xdc write_checkpoint shape_cmp)
printf '\033[31mDCP Shape Compare FAIL : DIFFERENT\033[0m\nRuntime: 34\n' > "$RUN_LOG"
judge_case_result "$CASE_DIR" "$RUN_LOG"
assert_result shape_compare_fail FAIL DCP_SHAPE_COMPARE_FAIL

new_case shape_compare_pass
FLOW_ARGS=(read_edif read_xdc write_checkpoint shape_cmp)
printf '\033[32mDCP Shape Compare PASS : SAME\033[0m\nRuntime: 34\n' > "$RUN_LOG"
judge_case_result "$CASE_DIR" "$RUN_LOG"
assert_result shape_compare_pass PASS DCP_SHAPE_COMPARE_PASS

new_case shape_compare_result_missing
FLOW_ARGS=(read_edif read_xdc write_checkpoint shape_cmp)
printf 'Runtime: 34\n' > "$RUN_LOG"
judge_case_result "$CASE_DIR" "$RUN_LOG"
assert_result shape_compare_result_missing FAIL DCP_SHAPE_RESULT_MISSING

new_case shape_compare_flow_config_invalid
FLOW_ARGS=(read_edif write_checkpoint shape_cmp)
printf 'DCP Shape Compare PASS : SAME\nRuntime: 34\n' > "$RUN_LOG"
judge_case_result "$CASE_DIR" "$RUN_LOG"
assert_result shape_compare_flow_config_invalid FAIL DCP_SHAPE_FLOW_CONFIG_INVALID

new_case shape_marker_ignored_without_shape_cmp
FLOW_ARGS=(read_edif read_xdc write_checkpoint)
printf 'DCP Shape Compare FAIL : DIFFERENT\nRuntime: 34\n' > "$RUN_LOG"
judge_case_result "$CASE_DIR" "$RUN_LOG"
assert_result shape_marker_ignored_without_shape_cmp PASS PASS

printf 'All judge tests passed.\n'
