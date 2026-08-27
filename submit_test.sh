#!/bin/bash

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
GALAXCORE_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
source "${SCRIPT_DIR}/common.sh"

#1. run all cases, run 4 cases in background at one time.

start_time=`date "+%s"`
GalaxCore_bin=../../../../bin/Linux_64/GalaxCore

job_key="\${GalaxCore_bin} run.tcl"

handle_interrupt() {
    echo "Interrupt the submit_test.sh!"
    kill 0
    exit 1
}

trap 'handle_interrupt' SIGINT


get_svn_revision() {
    local revision

    revision=$(
        svn info --xml "$GALAXCORE_ROOT" 2>/dev/null \
        | sed -n 's/.*revision="\([0-9][0-9]*\)".*/\1/p' \
        | head -n 1
    )

    if [ -n "$revision" ]; then
        printf '%s' "$revision"
    else
        printf 'unknown'
    fi
}


get_failed_testcases() {
    local failed_file="$1"

    if [ ! -s "$failed_file" ]; then
        return 0
    fi

    awk '
        NF {
            line = $0
            sub(/^[[:space:]]+/, "", line)
            sub(/[[:space:]]+$/, "", line)
            if (line != "") {
                if (result != "") {
                    result = result " "
                }
                result = result line
            }
        }
        END {
            printf "%s", result
        }
    ' "$failed_file"
}


write_runtime_summary() {
    local result_status="$1"
    local failed_testcases="$2"
    local revision
    local summary_tmp

    revision=$(get_svn_revision)
    summary_tmp="${runTime_Summary}.tmp.$$"

    printf '%s    %s    %s\n' \
        "$revision" \
        "$result_status" \
        "$failed_testcases" \
        > "$summary_tmp"
    mv -f "$summary_tmp" "$runTime_Summary"
}


#generate argv

declare -a my_argv
my_argv[0]=write_bitstream
my_argv[1]=bit_cmp
my_argv[2]=msk_cmp
my_argv[3]=bgn_cmp
#my_argv[4]=report_timing_summary
declare -a my_argv_readback_cmp
declare -a my_argv_report_timing_only


my_argv_readback_cmp=("${my_argv[@]}")
#my_argv_readback_cmp+=("readback_cmp")
my_argv_readback_cmp+=("report_timing_summary")

my_argv_report_timing_only=("${my_argv[@]}")
my_argv_report_timing_only+=("report_timing_summary")

my_argv_route_design=("${my_argv[@]}")
my_argv_route_design+=("place_design")
my_argv_route_design+=("route_design")
my_argv_route_design+=("report_timing_summary")

echo ${my_argv[@]}

#define an array to save related cases

declare -a related_cases
#related_cases=(./kintexuplus/xcku5p-ffvb676-2-e/blk_mem_gen_0_exdes)
related_cases=(./kintexuplus/xcku5p-ffvb676-2-e/blk_mem_gen_0_exdes ./kintexuplus/xcku3p-ffvd900-1-i/convergentRoundingEven ./kintexuplus/xcku5p-ffvb676-2-e/car_parking_management)
case_count=${#related_cases[@]}
if [ ! $2 ]; then
  echo "running cases..."
  current_case_index=0
  for each_case in ${related_cases[@]}; do
    bg_max=4
    bg_count=`jobs|grep -c "$job_key"`
    while [ $bg_count -ge  $bg_max ]; do
      bg_count=`jobs|grep -c "$job_key"`
      sleep 0.1
    done
#run case.
    'cd' $each_case
    removeFile
#blk_mem_gen_0_exdes need exec readback_cmp
    if [[ $each_case == "./kintexuplus/xcku5p-ffvb676-2-e/blk_mem_gen_0_exdes" ]]; then
      ${GalaxCore_bin} run.tcl ${my_argv_readback_cmp[@]} > run &
    elif [[ $each_case == "./kintexuplus/xcku5p-ffvb676-2-e/car_parking_management" ]]; then
      ${GalaxCore_bin} run.tcl ${my_argv_report_timing_only[@]} > run &
    elif [[ $each_case == "./kintexuplus/xcku3p-ffvd900-1-i/convergentRoundingEven" ]]; then
      ${GalaxCore_bin} run.tcl ${my_argv_route_design[@]} > run &
    else
      ${GalaxCore_bin} run.tcl ${my_argv[@]} > run &
    fi
    current_case_index=`expr $current_case_index + 1`
    bg_count=`jobs|grep -c "$job_key"`
    echo running $each_case at background
    done_count=`expr $current_case_index - $bg_count`
    echo $done_count/$case_count cases done
    echo $bg_count cases running in background
    'cd' ../../../
  done
fi

#2. statistic the running result
bg_count=`jobs | grep "$job_key"| grep -c "Running"`
while [ $bg_count  -gt 0 ]
do
   bg_count=`jobs | grep "$job_key" | grep -c "Running"`
   sleep 1
done
#3.translate case

#4. generate lisr_fail_to_run
echo generating dirty case list...
if [ -e list_fail_to_submit ]
then
  mv list_fail_to_submit .list_fail_to_submit
fi
list_fail_to_run=`pwd`/list_fail_to_submit
if [ -e runTime_summary ]
then
  mv runTime_summary .runTime_summary
fi
if [ -e runTime_Summary ]
then
  mv runTime_Summary .runTime_Summary
fi
runTime_Summary=`pwd`/runTime_summary

for each_case in ${related_cases[@]}; do
{
  'cd' $each_case/
  if [[ $each_case == "./kintexuplus/xcku5p-ffvb676-2-e/blk_mem_gen_0_exdes" ]]; then
    sortResult my_argv_readback_cmp[@] "$each_case"
  elif [[ $each_case == "./kintexuplus/xcku5p-ffvb676-2-e/car_parking_management" ]]; then
    sortResult my_argv_report_timing_only[@] "$each_case"
  elif [[ $each_case == "./kintexuplus/xcku3p-ffvd900-1-i/convergentRoundingEven" ]]; then
    sortResult my_argv_route_design[@] "$each_case"
  else
    sortResult my_argv[@] "$each_case"
  fi
}&
done
wait
#statistic
end_time=$(date)
echo Date:$end_time

showResult
return_code=$?

if [ "$return_code" -eq 0 ]; then
  summary_status=PASS
  failed_testcases="ok"
else
  summary_status=FAIL
  failed_testcases=$(get_failed_testcases "$list_fail_to_run")
fi

write_runtime_summary "$summary_status" "$failed_testcases"

end_time=`date "+%s"`
echo elapsed time: $(expr $end_time - $start_time)
exit $return_code
