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
    # It passes only when the checksum flow finished and mis_checksum.txt is empty.
    if printf '%s\n' "${FLOW_ARGS[@]}" | grep -qx "checksum_cmp"; then
        grep -q "$KW_CHECKSUM_FINISH" "$log_file" || {
            CASE_REASON="CHECKSUM_FINISH_MISSING"
            CASE_STAGE="checksum_cmp"
            return 0
        }

        if [ ! -e "$case_dir/mis_checksum.txt" ]; then
            CASE_REASON="CHECKSUM_RESULT_MISSING"
            CASE_STAGE="checksum_cmp"
            return 0
        fi

        if [ -s "$case_dir/mis_checksum.txt" ]; then
            CASE_REASON="CHECKSUM_COMPARE_FAIL"
            CASE_STAGE="checksum_cmp"
            return 0
        fi

        CASE_STATUS="PASS"
        CASE_REASON="CHECKSUM_COMPARE_PASS"
        CASE_STAGE="checksum_cmp"
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
