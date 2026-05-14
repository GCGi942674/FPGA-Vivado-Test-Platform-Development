#!/bin/bash
set -euo pipefail
find "${1:-.}" -type f \( -name '.run_status' -o -name '.run_reason' -o -name '.run_runtime' -o -name '.run_stage' -o -name '.run_ret' \) -delete
