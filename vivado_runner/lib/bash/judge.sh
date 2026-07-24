#!/bin/bash

check_runtime_signature() {
    local run_log="$1"
    grep -q "$KW_RUNTIME" "$run_log"
}

judge_case_result() {
    local case_dir="$1"
    local log_file="$2"

    CASE_STATUS="FAIL"
    CASE_REASON="UNKNOWN"
    CASE_STAGE="none"
    local compare_artifacts_passed=0

    if [ ! -f "$log_file" ]; then
        CASE_REASON="MISSING_LOG"
        return 0
    fi

    if grep -Eq "$KW_FATAL_ERROR" "$log_file"; then
        CASE_REASON="FATAL_ERROR"
        return 0
    fi

    if printf '%s\n' "${FLOW_ARGS[@]}" | grep -qx "dcp_cmp"; then
        if grep -Eq "$KW_DCP_FAIL" "$log_file"; then
            CASE_REASON="DCP_COMPARE_FAIL"
            CASE_STAGE=$(extract_dcp_stage "$log_file")
            return 0
        elif grep -Eq "$KW_DCP_PASS" "$log_file"; then
            CASE_STATUS="PASS"
            CASE_REASON="DCP_COMPARE_PASS"
            CASE_STAGE=$(extract_dcp_stage "$log_file")
            return 0
        fi
    fi

    if printf '%s\n' "${FLOW_ARGS[@]}" | grep -qx "bgn_cmp"; then
        grep -q "$KW_BGN_FINISH" "$log_file" || { CASE_REASON="BGN_TRANSLATE_MISSING"; return 0; }
        if [ -e "$case_dir/result_bgn.log" ]; then
            if [ -s "$case_dir/result_bgn.log" ]; then
                CASE_REASON="BGN_COMPARE_FAIL"
                return 0
            fi
        else
            CASE_REASON="BGN_RESULT_MISSING"
            return 0
        fi
        compare_artifacts_passed=1
    fi

    if printf '%s\n' "${FLOW_ARGS[@]}" | grep -qx "bit_cmp"; then
        grep -q "$KW_WRITE_BIT_FINISH" "$log_file" || { CASE_REASON="WRITE_BIT_MISSING"; return 0; }
        if [ -e "$case_dir/mis_bit.txt" ]; then
            if [ -s "$case_dir/mis_bit.txt" ]; then
                CASE_REASON="BIT_COMPARE_FAIL"
                return 0
            fi
        else
            CASE_REASON="BIT_RESULT_MISSING"
            return 0
        fi
        compare_artifacts_passed=1
    fi

    if printf '%s\n' "${FLOW_ARGS[@]}" | grep -qx "msk_cmp"; then
        grep -q "$KW_WRITE_BIT_FINISH" "$log_file" || { CASE_REASON="WRITE_BIT_MISSING"; return 0; }
        if [ -e "$case_dir/mis_msk.txt" ]; then
            if [ -s "$case_dir/mis_msk.txt" ]; then
                CASE_REASON="MSK_COMPARE_FAIL"
                return 0
            fi
        else
            CASE_REASON="MSK_RESULT_MISSING"
            return 0
        fi
        compare_artifacts_passed=1
    fi

    if printf '%s\n' "${FLOW_ARGS[@]}" | grep -qx "report_timing_summary"; then
        if [ -e "$case_dir/mis_timing_summary.txt" ]; then
            if [ -s "$case_dir/mis_timing_summary.txt" ]; then
                CASE_REASON="TIMING_SUMMARY_FAIL"
                return 0
            fi
        else
            CASE_REASON="TIMING_SUMMARY_MISSING"
            return 0
        fi
    fi

    # checksum compare
    # checksum_cmp is independent from write_bitstream.
    #
    # checksum result is determined only by mis_checksum.txt:
    #   missing   -> checksum flow unfinished
    #   non-empty -> checksum compare failed
    #   empty     -> checksum compare passed
    #
    # mis_checksum.txt is the only trusted result artifact.
    if printf '%s\n' "${FLOW_ARGS[@]}" | grep -qx "checksum_cmp"; then

        local checksum_file="$case_dir/mis_checksum.txt"

        # checksum result file missing
        if [ ! -f "$checksum_file" ]; then
            CASE_REASON="CHECKSUM_RESULT_MISSING"
            CASE_STAGE="checksum_cmp"
            return 0
        fi

        # checksum compare failed
        if [ -s "$checksum_file" ]; then
            CASE_REASON="CHECKSUM_COMPARE_FAIL"
            CASE_STAGE="checksum_cmp"
            return 0
        fi

        # checksum compare passed
        CASE_STATUS="PASS"
        CASE_REASON="CHECKSUM_COMPARE_PASS"
        CASE_STAGE="checksum_cmp"
        return 0
    fi

    # report_utilization compare
    # The result is determined only by mis_report_utilization.txt:
    #   missing   -> utilization compare flow unfinished
    #   non-empty -> utilization compare failed
    #   empty     -> utilization compare passed
    if printf '%s\n' "${FLOW_ARGS[@]}" | grep -qx "report_utilization"; then

        local utilization_file="$case_dir/mis_report_utilization.txt"

        if [ ! -f "$utilization_file" ]; then
            CASE_REASON="REPORT_UTILIZATION_RESULT_MISSING"
            CASE_STAGE="report_utilization"
            return 0
        fi

        if [ -s "$utilization_file" ]; then
            CASE_REASON="REPORT_UTILIZATION_COMPARE_FAIL"
            CASE_STAGE="report_utilization"
            return 0
        fi

        CASE_STATUS="PASS"
        CASE_REASON="REPORT_UTILIZATION_COMPARE_PASS"
        CASE_STAGE="report_utilization"
        return 0
    fi

    # rpx compare
    # The result is determined only by mis_rpx.txt:
    #   missing   -> rpx compare flow unfinished
    #   non-empty -> rpx compare failed
    #   empty     -> rpx compare passed
    if printf '%s\n' "${FLOW_ARGS[@]}" | grep -qx "rpx_cmp"; then

        local rpx_file="$case_dir/mis_rpx.txt"

        if [ ! -f "$rpx_file" ]; then
            CASE_REASON="RPX_RESULT_MISSING"
            CASE_STAGE="rpx_cmp"
            return 0
        fi

        if [ -s "$rpx_file" ]; then
            CASE_REASON="RPX_COMPARE_FAIL"
            CASE_STAGE="rpx_cmp"
            return 0
        fi

        CASE_STATUS="PASS"
        CASE_REASON="RPX_COMPARE_PASS"
        CASE_STAGE="rpx_cmp"
        return 0
    fi

    # BGN/BIT/MSK compare modules have already validated their completion
    # markers and empty mismatch artifacts. They do not require the generic
    # Runtime signature used by ordinary non-compare flows.
    if [ "$compare_artifacts_passed" -eq 1 ]; then
        CASE_STATUS="PASS"
        CASE_REASON="COMPARE_ARTIFACTS_PASS"
        CASE_STAGE="compare"
        return 0
    fi

    if check_runtime_signature "$log_file"; then
        CASE_STATUS="PASS"
        CASE_REASON="PASS"
    else
        CASE_REASON="RUNTIME_MISSING"
    fi
}

extract_dcp_stage() {
    local log_file="$1"
    local line
    line=$(grep -E "$KW_DCP_PASS|$KW_DCP_FAIL" "$log_file" | tail -n 1 || true)
    echo "$line" | sed -n 's/.*(\([^)]*\)).*/\1/p'
}
