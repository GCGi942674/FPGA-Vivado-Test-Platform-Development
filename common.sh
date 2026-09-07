#!/bin/bash


removeFile() {
  #if [ -e run ] ; then
  #  rm run
  #fi

  if [ -e output.bit ]
  then
    rm output.bit
  fi
  if [ -e output.bgn ]
  then
    rm output.bgn
  fi
  if [ -e output.msk ]
  then
    rm output.msk
  fi

  if [ -e output.msd ]
  then
    rm output.msd
  fi
  if [ -e output.rbd ]
  then
    rm output.rbd
  fi

  if [ -e output_cmp.bgn ]
  then
    rm output_cmp.bgn
  fi
  if [ -e golden_cmp.bgn ]
  then
    rm golden_cmp.bgn
  fi

  if [ -e output.dcp ]
  then
    rm output.dcp
  fi

  if [ -e output_report_timing_summary.log ]
  then
    rm output_report_timing_summary.log
  fi

}

removeResultFile() {
  if [ -e mis_bit.txt ]
  then
    rm mis_bit.txt
  fi
  if [ -e mis_msk.txt ]
  then
    rm mis_msk.txt
  fi

  if [ -e result_bgn.log ]
  then
    rm result_bgn.log
  fi

  if [ -e mis_timing_summary.txt ]
  then
    rm mis_timing_summary.txt
  fi

}

sortResult() {
  local arr=("${!1}")
  local case=$2
  if [[ "${arr[@]}" =~ "bgn_cmp" ]] ; then
    grep "translate bgn finish" run
    if [ ! $? -eq 0 ] ; then
      echo write $case bgn crash >> $list_fail_to_run
      return
    else
      if [ -e result_bgn.log ]; then
        if [ -s result_bgn.log ]; then
          echo Error in $case/result_bgn.log >> $list_fail_to_run
          return
        fi
      else
        echo $case result_bgn.log not exists >> $list_fail_to_run
        return
      fi
    fi
  fi

  if [[ "${arr[@]}" =~ "bit_cmp" ]] ; then
    grep "write_bitstream finish" run
    if [ ! $? -eq 0 ] ; then
      echo write $case bit crash >> $list_fail_to_run
      return
    else
      if [ -e mis_bit.txt ]; then
        if [ -s mis_bit.txt ]; then
          echo Error in $case/mis_bit.txt >> $list_fail_to_run
          return
        fi
      else
        echo  $case mis_bit.txt not exists  >> $list_fail_to_run
        return
      fi
    fi
  fi

  if [[ "${arr[@]}" =~ "msk_cmp" ]] ; then
    grep "write_bitstream finish" run
    if [ ! $? -eq 0 ] ; then
      echo write $case msk crash >> $list_fail_to_run
      return
    else
      if [ -e mis_msk.txt ]; then
        if [ -s mis_msk.txt ]; then
          echo Error in $case/mis_msk.txt  >> $list_fail_to_run
          return
        fi
      else
        echo $case mis_msk.txt not exists >> $list_fail_to_run
        return
      fi
    fi
  fi

  if [[ "${arr[@]}" =~ "readback_cmp" ]] ; then
    grep "write_bitstream finish" run
    if [ ! $? -eq 0 ] ; then
      echo write $case readback_cmp crash >> $list_fail_to_run
      return
    else
      if [ -e mis_msd.txt ] && [ -e mis_rbd.txt ]; then
        if [ -s mis_msd.txt ]; then
          echo Error in $case/mis_msd.txt >> $list_fail_to_run
          return
        fi
        if [ -s mis_rbd.txt ]; then
          echo Error in $case/mis_rbd.txt >> $list_fail_to_run
          return
        fi
      else
        echo $case mis_msd.txt or mis_rbd.txt not exists >> $list_fail_to_run
        return
      fi
    fi
  fi

  if [[ "${arr[@]}" =~ "report_timing_summary" ]] ; then
    if [ -e mis_timing_summary.txt ]; then
      if [ -s mis_timing_summary.txt ]; then
        echo Error in $case/mis_timing_summary.txt  >> $list_fail_to_run
        return
      fi
    else
      echo $case mis_timing_summary.txt not exists >> $list_fail_to_run
      return
    fi
  fi


  grep "Runtime:" run
  if [ ! $? -eq 0 ] ; then
    echo $case runTime missing >> $runTime_Summary
    return
  else
    grep -E "Runtime:" run | awk -v case_name="$case" '{print $0 "  " case_name}' >> $runTime_Summary
    return
  fi
  #sort -k 2n runTime_Summary
}

#Add get dcp_cmp result
collect_dcp_cmp_result() {
    local runtime_file="$1"
    local fail_file="$2"
    local stat_file="$3"
    shift 3

    local each_case
    local case_dir
    local run_log
    local stage
    local total_count=0
    local pass_count=0
    local fail_count=0
    local miss_count=0

    # Clear output files before writing
    : > "$runtime_file"
    : > "$fail_file"
    : > "$stat_file"

    for each_case in "$@"; do
        case_dir="$(dirname "$each_case")"
        run_log="${case_dir}/run"
        total_count=$((total_count + 1))

        if [ ! -f "$run_log" ]; then
            echo "$each_case    DCP Compare result missing" >> "$fail_file"
            miss_count=$((miss_count + 1))
            continue
        fi

        if grep -q "DCP Compare FAIL" "$run_log"; then
            stage=$(grep "DCP Compare FAIL" "$run_log" | tail -n 1 | sed -n 's/.*DCP Compare FAIL (\([^)]*\)).*/\1/p')
            [ -z "$stage" ] && stage="unknown"
            echo "$each_case    DCP Compare FAIL ($stage)" >> "$fail_file"
            fail_count=$((fail_count + 1))
        elif grep -q "DCP Compare PASS" "$run_log"; then
            stage=$(grep "DCP Compare PASS" "$run_log" | tail -n 1 | sed -n 's/.*DCP Compare PASS (\([^)]*\)).*/\1/p')
            [ -z "$stage" ] && stage="unknown"
            echo "[DCP_CMP_PASS][$stage] $each_case" >> "$runtime_file"
            pass_count=$((pass_count + 1))
        else
            echo "$each_case    DCP Compare result missing" >> "$fail_file"
            miss_count=$((miss_count + 1))
        fi
    done

    {
        echo "========== DCP Compare Summary =========="
        echo "DCP Compare Total : $total_count"
        echo "DCP Compare PASS  : $pass_count"
        echo "DCP Compare FAIL  : $fail_count"
        echo "DCP Compare MISS  : $miss_count"
        echo "========================================="
    } >> "$stat_file"
}

showResult() {
  if [ -e $list_fail_to_run ] ; then
    grep "crash\|Error" $list_fail_to_run
    if [ $? -eq 0 ] ; then
      echo -e "\033[0;31mCase Fail! please don't submit your code !\033[0m"
    else
      grep "not exists" $list_fail_to_run
      if [ $? -eq 0 ] ; then
        echo -e "\033[0;31mcase log not exists! please don't submit your code !\033[0m"
      fi
    fi
    return 1
  else
    echo -e "\033[0;32mNo Case Fail, You can submit your code now~\033[0m"
    return 0
  fi
}

showSummary() {
  if [ -e $list_fail_to_run ]; then
    echo Bit Error case Summary:
    echo "Bit Error case Count: $(grep -c '\(mis_\)\?bit' $list_fail_to_run)"
    grep "\(mis_\)\?bit" $list_fail_to_run
    echo ""
    echo Msk Error case Summary:
    echo "Msk Error case Count: $(grep -c '\(mis_\)\?msk' $list_fail_to_run)"
    grep "\(mis_\)\?msk" $list_fail_to_run
    echo ""
    echo Bgn Error case Summary:
    echo "Bgn Error case Count: $(grep -c '\(mis_\)\?bgn' $list_fail_to_run)"
    grep "\(mis_\)\?bgn" $list_fail_to_run
    echo ""
    echo ReportTiming Error case Summary:
    echo "ReportTiming Error case Count: $(grep -c '\(mis_\)\?timing_summary' $list_fail_to_run)"
    grep "\(mis_\)\?timing_summary" $list_fail_to_run

  else
    echo No Case Fail!
  fi
}
