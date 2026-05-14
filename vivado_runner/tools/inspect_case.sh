#!/bin/bash
set -euo pipefail
case_dir="$1"
for file in .run_status .run_reason .run_runtime .run_stage .run_ret; do
  [ -f "$case_dir/$file" ] && echo "$file: $(cat "$case_dir/$file")"
done
