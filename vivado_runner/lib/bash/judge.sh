#!/bin/bash

check_runtime_signature() {
    local run_log="$1"

    # A case is complete only when the last non-empty GalaxCore output line is
    # exactly "Runtime: <seconds>".  Merely finding Runtime somewhere in the
    # log can accept a process that continued writing errors afterwards.
    LC_ALL=C awk '
        {
            line = $0
            sub(/\r$/, "", line)
            if (line ~ /[^[:space:]]/) {
                last = line
            }
        }
        END {
            if (last ~ /^Runtime:[[:space:]]*[0-9]+[[:space:]]*$/) {
                exit 0
            }
            exit 1
        }
    ' "$run_log"
}

flow_arg_enabled() {
    local module="$1"
    printf '%s\n' "${FLOW_ARGS[@]}" | grep -qx -- "$module"
}

judge_shape_compare_result() {
    local log_file="$1"
    local shape_result

    CASE_STAGE="shape_cmp"

    if ! flow_arg_enabled "read_edif" ||
       ! flow_arg_enabled "read_xdc" ||
       ! flow_arg_enabled "write_checkpoint"; then
        CASE_REASON="DCP_SHAPE_FLOW_CONFIG_INVALID"
        return 0
    fi

    # Use the last marker if the tool emitted more than one comparison result.
    # ANSI color escapes around the marker do not affect these matches.
    shape_result=$(grep -E "$KW_DCP_SHAPE_PASS|$KW_DCP_SHAPE_FAIL" "$log_file" | tail -n 1 || true)

    if printf '%s\n' "$shape_result" | grep -Eq "$KW_DCP_SHAPE_FAIL"; then
        CASE_REASON="DCP_SHAPE_COMPARE_FAIL"
        return 0
    fi

    if printf '%s\n' "$shape_result" | grep -Eq "$KW_DCP_SHAPE_PASS"; then
        CASE_STATUS="PASS"
        CASE_REASON="DCP_SHAPE_COMPARE_PASS"
        return 0
    fi

    CASE_REASON="DCP_SHAPE_RESULT_MISSING"
    return 0
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

    # Runtime must be the final non-empty output line before any result
    # artifact is trusted.  Comparison files alone cannot prove that the
    # GalaxCore process completed normally.
    if ! check_runtime_signature "$log_file"; then
        if grep -q "$KW_RUNTIME" "$log_file"; then
            CASE_REASON="RUNTIME_NOT_FINAL"
        else
            CASE_REASON="RUNTIME_MISSING"
        fi
        return 0
    fi

    # Runtime proves only that GalaxCore completed. An explicitly enabled
    # shape_cmp flow must still honor the shape PASS/FAIL marker in run.
    if flow_arg_enabled "shape_cmp"; then
        judge_shape_compare_result "$log_file"
        return 0
    fi

    if flow_arg_enabled "dcp_cmp"; then
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

    # BGN/BIT/MSK comparison is meaningful only as part of write_bitstream.
    # With a valid final Runtime line, the fresh result artifacts are the
    # source of truth: missing means unfinished, non-empty means mismatch,
    # and empty means pass.
    if flow_arg_enabled "write_bitstream"; then
        if flow_arg_enabled "bgn_cmp"; then
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

        if flow_arg_enabled "bit_cmp"; then
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

        if flow_arg_enabled "msk_cmp"; then
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
    fi

    if flow_arg_enabled "report_timing_summary"; then
        if [ -e "$case_dir/mis_timing_summary.txt" ]; then
            if [ -s "$case_dir/mis_timing_summary.txt" ]; then
                CASE_REASON="TIMING_SUMMARY_FAIL"
                return 0
            fi
        else
            CASE_REASON="TIMING_SUMMARY_MISSING"
            return 0
        fi
        compare_artifacts_passed=1
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
    if flow_arg_enabled "checksum_cmp"; then

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
    if flow_arg_enabled "report_utilization"; then

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
    if flow_arg_enabled "rpx_cmp"; then

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

    # Runtime was validated before the result files, so empty comparison
    # artifacts now represent a complete successful run.
    if [ "$compare_artifacts_passed" -eq 1 ]; then
        CASE_STATUS="PASS"
        CASE_REASON="COMPARE_ARTIFACTS_PASS"
        CASE_STAGE="compare"
        return 0
    fi

    CASE_STATUS="PASS"
    CASE_REASON="PASS"
}

extract_dcp_stage() {
    local log_file="$1"
    local line
    line=$(grep -E "$KW_DCP_PASS|$KW_DCP_FAIL" "$log_file" | tail -n 1 || true)
    echo "$line" | sed -n 's/.*(\([^)]*\)).*/\1/p'
}
