#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# task_dispatcher.sh
#
# Simple locked dispatcher:
# - polls task_queue.txt
# - handles only tasks for local server
# - uses locks for queue/running/history
# - uses per-server lock to ensure only one running task
# ============================================================

# ----------------------------
# Configuration
# ----------------------------
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

SHARE_ROOT="/home/xiaonan/Share/zw_cache/run_logs"

TASK_QUEUE="${SHARE_ROOT}/task_queue.txt"
TASK_RUNNING="${SHARE_ROOT}/task_running.txt"
TASK_HISTORY="${SHARE_ROOT}/task_history.log"
DISPATCHER_LOG="${SHARE_ROOT}/task_dispatcher.log"

LOCK_DIR="${SHARE_ROOT}/locks"
SERVER_LOCK_DIR="${SHARE_ROOT}/server_locks"

QUEUE_LOCK="${LOCK_DIR}/task_queue.lock"
RUNNING_LOCK="${LOCK_DIR}/task_running.lock"
HISTORY_LOCK="${LOCK_DIR}/task_history.lock"

POLL_INTERVAL=10
TIMEOUT_SEC=10800
LOCAL_SERVER="$(hostname)"

declare -A SERVER_ROOTS=(
  [pudong]="/home/chenggong/workspace/galaxcore3/test2"
  [kangqiao]="/home/chenggong/workspace/galaxcore3/test2"
)

mkdir -p "$SHARE_ROOT" "$LOCK_DIR" "$SERVER_LOCK_DIR"
touch "$TASK_QUEUE" "$TASK_RUNNING" "$TASK_HISTORY" "$DISPATCHER_LOG"
touch "$QUEUE_LOCK" "$RUNNING_LOCK" "$HISTORY_LOCK"

# ----------------------------
# Helpers
# ----------------------------
log() {
  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "[$ts] $*" | tee -a "$DISPATCHER_LOG"
}

trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

write_or_replace_key() {
  local file="$1"
  local key="$2"
  local value="$3"

  if grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$file"; then
    sed -i "s|^[[:space:]]*${key}[[:space:]]*=.*$|${key}=${value}|g" "$file"
  else
    echo "${key}=${value}" >> "$file"
  fi
}

server_lock_file() {
  echo "${SERVER_LOCK_DIR}/${LOCAL_SERVER}.lock"
}

server_lock_exists() {
  [[ -f "$(server_lock_file)" ]]
}

create_server_lock() {
  local number="$1"
  echo "NUMBER=${number}" > "$(server_lock_file)"
}

remove_server_lock() {
  rm -f "$(server_lock_file)"
}

# ----------------------------
# Queue operations
# ----------------------------

# Atomically claim the first PENDING task for LOCAL_SERVER.
# Output format:
#   line_no|full_original_line
claim_first_pending_task() {
  exec 9<>"$QUEUE_LOCK"
  flock -x 9

  local tmp_file
  local claim_file
  tmp_file="$(mktemp)"
  claim_file="$(mktemp)"

  awk -v srv="$LOCAL_SERVER" -v claim_file="$claim_file" '
    BEGIN { OFS=" " }
    NR == 1 { print; next }
    /^#/ { print; next }
    /^$/ { print; next }

    {
      if (!claimed && NF >= 7 && $2 == srv && $7 == "PENDING") {
        print NR "|" $0 > claim_file
        $7 = "RUNNING"
        claimed = 1
      }
      print
    }
  ' "$TASK_QUEUE" > "$tmp_file"

  mv "$tmp_file" "$TASK_QUEUE"

  local result=""
  if [[ -s "$claim_file" ]]; then
    result="$(cat "$claim_file")"
  fi
  rm -f "$claim_file"

  flock -u 9
  exec 9>&-

  printf '%s\n' "$result"
}

mark_task_done_by_number() {
  local number="$1"

  exec 9<>"$QUEUE_LOCK"
  flock -x 9

  local tmp_file
  tmp_file="$(mktemp)"

  awk -v n="$number" '
    BEGIN { OFS=" " }
    NR == 1 { print; next }
    /^#/ { print; next }
    /^$/ { print; next }
    {
      if ($1 == n) {
        $7 = "DONE"
      }
      print
    }
  ' "$TASK_QUEUE" > "$tmp_file"

  mv "$tmp_file" "$TASK_QUEUE"

  flock -u 9
  exec 9>&-
}

# ----------------------------
# Running file operations
# ----------------------------
write_task_running() {
  local number="$1"
  local server="$2"
  local test_type="$3"
  local test_target="$4"
  local stages="$5"
  local bg="$6"
  local cmd="$7"
  local start_time="$8"

  exec 8<>"$RUNNING_LOCK"
  flock -x 8

  cat > "$TASK_RUNNING" <<EOF
================ Running Task ================
Number              : ${number}
Server              : ${server}
Test Type           : ${test_type}
Test Target         : ${test_target}
Stages              : ${stages}
Parallel Max        : ${bg}
Command             : ${cmd}
Start Time          : ${start_time}
==============================================
EOF

  flock -u 8
  exec 8>&-
}

clear_task_running() {
  exec 8<>"$RUNNING_LOCK"
  flock -x 8
  : > "$TASK_RUNNING"
  flock -u 8
  exec 8>&-
}

# ----------------------------
# History file operations
# ----------------------------
append_history_block() {
  local block_file="$1"

  exec 7<>"$HISTORY_LOCK"
  flock -x 7
  cat "$block_file" >> "$TASK_HISTORY"
  flock -u 7
  exec 7>&-
}

# ----------------------------
# Task parsing
# ----------------------------
parse_task_line() {
  local line="$1"
  read -r NUMBER SERVER TEST_TYPE TEST_TARGET STAGES BG STATE <<< "$line"

  NUMBER="$(trim "$NUMBER")"
  SERVER="$(trim "$SERVER")"
  TEST_TYPE="$(trim "$TEST_TYPE")"
  TEST_TARGET="$(trim "$TEST_TARGET")"
  STAGES="$(trim "$STAGES")"
  BG="$(trim "$BG")"
  STATE="$(trim "$STATE")"
}

# ----------------------------
# flow_config update
# ----------------------------
apply_flow_config() {
  local cfg="$1"
  local stages_csv="$2"

  local opt_design=0
  local place_design=0
  local route_design=0
  local phys_opt_design=0
  local write_checkpoint=0
  local write_bitstream=0
  local report_timing_summary=0
  local bit_cmp=0
  local msk_cmp=0
  local bgn_cmp=0

  IFS=',' read -r -a arr <<< "$stages_csv"
  for s in "${arr[@]}"; do
    s="$(trim "$s")"
    case "$s" in
      opt_design) opt_design=1 ;;
      place_design) place_design=1 ;;
      route_design) route_design=1 ;;
      phys_opt_design) phys_opt_design=1 ;;
      write_checkpoint) write_checkpoint=1 ;;
      write_bitstream) write_bitstream=1 ;;
      report_timing_summary) report_timing_summary=1 ;;
      bit_cmp) bit_cmp=1 ;;
      msk_cmp) msk_cmp=1 ;;
      bgn_cmp) bgn_cmp=1 ;;
    esac
  done

  # write_bitstream auto-enables compare steps
  if [[ "$write_bitstream" == "1" ]]; then
    bit_cmp=1
    msk_cmp=1
    bgn_cmp=1
  else
    bit_cmp=0
    msk_cmp=0
    bgn_cmp=0
  fi

  write_or_replace_key "$cfg" "opt_design" "$opt_design"
  write_or_replace_key "$cfg" "place_design" "$place_design"
  write_or_replace_key "$cfg" "route_design" "$route_design"
  write_or_replace_key "$cfg" "phys_opt_design" "$phys_opt_design"
  write_or_replace_key "$cfg" "write_checkpoint" "$write_checkpoint"
  write_or_replace_key "$cfg" "write_bitstream" "$write_bitstream"
  write_or_replace_key "$cfg" "report_timing_summary" "$report_timing_summary"
  write_or_replace_key "$cfg" "bit_cmp" "$bit_cmp"
  write_or_replace_key "$cfg" "msk_cmp" "$msk_cmp"
  write_or_replace_key "$cfg" "bgn_cmp" "$bgn_cmp"
}

# ----------------------------
# Command builder
# ----------------------------
build_command() {
  local root="$1"
  local cfg="$2"
  local target="$3"
  local bg="$4"

  echo "cd $(printf '%q' "$root") && ./run.sh $(printf '%q' "$target") --flow-config $(printf '%q' "$cfg") --bg $(printf '%q' "$bg") --timeout $(printf '%q' "$TIMEOUT_SEC") --report-dst $(printf '%q' "$SHARE_ROOT") --copy"
}

# ----------------------------
# Read generated reports and append history
# ----------------------------
append_history_from_reports() {
  local number="$1"
  local server="$2"
  local test_type="$3"
  local test_target="$4"
  local stages="$5"
  local bg="$6"
  local cmd="$7"
  local report_dir="$8"

  local json_report="${report_dir}/execution_report.json"
  local result_path_file="${report_dir}/last_result_path.txt"
  local detail_path_file="${report_dir}/last_detail_path.txt"

  local host_name=""
  local start_time=""
  local end_time=""
  local version=""
  local parallel_max=""
  local time_limit=""
  local enabled_modules=""
  local total_cases="0"
  local runnable_cases="0"
  local skipped_cases="0"
  local pass_cases="0"
  local failed_cases="0"
  local timeout_cases="0"
  local elapsed_time="0"
  local status="UNKNOWN"
  local result_path=""
  local detail_path=""

  if [[ -f "$json_report" ]]; then
    mapfile -t parsed < <(
      python3 - <<PY
import json
with open("$json_report", "r", encoding="utf-8") as f:
    data = json.load(f)

meta = data.get("meta", {})
print(meta.get("host_name", ""))
print(meta.get("start_time", ""))
print(meta.get("end_time", ""))
print(meta.get("svn_version", ""))
print(meta.get("bg_max", ""))
print(meta.get("time_limit", ""))
print(meta.get("enabled_modules", ""))
print(data.get("total", 0))
print(data.get("runnable_cases", 0))
print(data.get("skipped_cases", 0))
print(data.get("pass_cases", 0))
print(data.get("failed_cases", 0))
print(data.get("timeout_cases", 0))
print(data.get("elapsed_time_sec", 0))
print(data.get("status", "UNKNOWN"))
PY
    )

    host_name="${parsed[0]:-}"
    start_time="${parsed[1]:-}"
    end_time="${parsed[2]:-}"
    version="${parsed[3]:-}"
    parallel_max="${parsed[4]:-}"
    time_limit="${parsed[5]:-}"
    enabled_modules="${parsed[6]:-}"
    total_cases="${parsed[7]:-0}"
    runnable_cases="${parsed[8]:-0}"
    skipped_cases="${parsed[9]:-0}"
    pass_cases="${parsed[10]:-0}"
    failed_cases="${parsed[11]:-0}"
    timeout_cases="${parsed[12]:-0}"
    elapsed_time="${parsed[13]:-0}"
    status="${parsed[14]:-UNKNOWN}"
  fi

  [[ -f "$result_path_file" ]] && result_path="$(cat "$result_path_file")"
  [[ -f "$detail_path_file" ]] && detail_path="$(cat "$detail_path_file")"

  local block_file
  block_file="$(mktemp)"

  cat > "$block_file" <<EOF
================ Statistics ================
Number              : ${number}
Host                : ${host_name}
Server              : ${server}
Start Time          : ${start_time}
End Time            : ${end_time}
Test Type           : ${test_type}
Test Target         : ${test_target}
Version             : ${version}
Parallel Max        : ${parallel_max}
Time Limit(s)       : ${time_limit}
Enabled Modules     : ${enabled_modules}
Total Cases         : ${total_cases}
Runnable Cases      : ${runnable_cases}
Skipped Cases       : ${skipped_cases}
Pass Cases          : ${pass_cases}
Failed Cases        : ${failed_cases}
Timeout Cases       : ${timeout_cases}
Elapsed Time(s)     : ${elapsed_time}
Status              : ${status}
Command             : ${cmd}
Result Path         : ${result_path}
Detail Path         : ${detail_path}
============================================

EOF

  append_history_block "$block_file"
  rm -f "$block_file"
}

# ----------------------------
# Run one task
# ----------------------------
run_one_task() {
  local task_line="$1"

  parse_task_line "$task_line"

  log "Picked task NUMBER=${NUMBER}, SERVER=${SERVER}, TEST_TYPE=${TEST_TYPE}, TARGET=${TEST_TARGET}"

  local root="${SERVER_ROOTS[$SERVER]:-}"
  if [[ -z "$root" ]]; then
    log "ERROR: no SERVER_ROOT configured for ${SERVER}"
    return 1
  fi

  local run_sh="${root}/run.sh"
  local cfg="${root}/flow_config"
  local report_dir="${root}/vivado_runner/runtime/reports/latest"

  if [[ ! -f "$run_sh" ]]; then
    log "ERROR: run.sh not found: $run_sh"
    return 1
  fi
  if [[ ! -f "$cfg" ]]; then
    log "ERROR: flow_config not found: $cfg"
    return 1
  fi

  apply_flow_config "$cfg" "$STAGES"

  local cmd
  cmd="$(build_command "$root" "$cfg" "$TEST_TARGET" "$BG")"

  local start_time
  start_time="$(date '+%Y-%m-%d %H:%M:%S')"

  create_server_lock "$NUMBER"
  write_task_running "$NUMBER" "$SERVER" "$TEST_TYPE" "$TEST_TARGET" "$STAGES" "$BG" "$cmd" "$start_time"

  log "Running task NUMBER=${NUMBER}"
  log "COMMAND: $cmd"

  set +e
  bash -lc "$cmd"
  local rc=$?
  set -e

  log "Task NUMBER=${NUMBER} finished with exit code ${rc}"

  append_history_from_reports "$NUMBER" "$SERVER" "$TEST_TYPE" "$TEST_TARGET" "$STAGES" "$BG" "$cmd" "$report_dir"

  mark_task_done_by_number "$NUMBER"
  clear_task_running
  remove_server_lock

  return 0
}

# ----------------------------
# Main loop
# ----------------------------
main_loop() {
  log "Dispatcher started on ${LOCAL_SERVER}"

  while true; do
    if server_lock_exists; then
      sleep "$POLL_INTERVAL"
      continue
    fi

    local claimed
    claimed="$(claim_first_pending_task || true)"

    if [[ -z "$claimed" ]]; then
      sleep "$POLL_INTERVAL"
      continue
    fi

    local line_no task_line
    line_no="${claimed%%|*}"
    task_line="${claimed#*|}"

    if [[ -n "$task_line" ]]; then
      run_one_task "$task_line" || log "ERROR: task execution failed"
    fi

    sleep "$POLL_INTERVAL"
  done
}

main_loop