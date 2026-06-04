#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INTERNAL_ROOT="$SCRIPT_DIR/vivado_runner"
RUNTIME_DIR="$INTERNAL_ROOT/runtime"

find "$SCRIPT_DIR" -type f \( \
    -name '.run_status' -o \
    -name '.run_reason' -o \
    -name '.run_runtime' -o \
    -name '.run_stage' -o \
    -name '.run_ret' -o \
    -name 'output.bit' -o \
    -name 'output.msk' -o \
    -name 'output.msd' -o \
    -name 'output.rbd' -o \
    -name 'output_cmp.bgn' -o \
    -name 'golden_cmp.bgn' -o \
    -name 'output.dcp' -o \
    -name 'output_report_timing_summary.log' -o \
    -name 'mis_bit.txt' -o \
    -name 'mis_msk.txt' -o \
    -name 'mis_msd.txt' -o \
    -name 'mis_rbd.txt' -o \
    -name 'result_bgn.log' -o \
    -name 'mis_timing_summary.txt' -o \
    -name 'run' \
\) -delete

rm -rf "$RUNTIME_DIR/logs" "$RUNTIME_DIR/status" "$RUNTIME_DIR/tmp" "$RUNTIME_DIR/reports/latest" "$RUNTIME_DIR/reports/archive"/* "$RUNTIME_DIR/cache"
mkdir -p "$RUNTIME_DIR/logs" "$RUNTIME_DIR/status" "$RUNTIME_DIR/tmp" "$RUNTIME_DIR/reports/latest" "$RUNTIME_DIR/reports/archive" "$RUNTIME_DIR/cache"

echo "Cleaned runtime and case artifacts under: $SCRIPT_DIR"
