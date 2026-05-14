#!/bin/bash

INPUT_TARGET="."
FLOW_CONFIG_PATH=""
ENABLE_COPY_REPORTS=0
CLI_BG_MAX=""
CLI_TIME_LIMIT=""
CLI_REPORT_DST_DIR=""
CLI_GALAXCORE_BIN=""
KEEP_RUNTIME=1

print_usage() {
cat <<USAGE
Usage:
  ./run.sh [path|run.tcl|list_file] [options]

Input:
  path        Directory to recursively search for run.tcl
  run.tcl     Single run.tcl file
  list_file   File containing run.tcl paths (one per line, supports relative/absolute paths)

Options:
  --flow-config <file>    Use external flow_config file
  --copy                  Copy final reports to export directory
  --bg <num>              Max parallel jobs
  --timeout <sec>         Per-case timeout in seconds
  --report-dst <dir>      Override copy destination directory
  --galaxcore <path>      Override GalaxCore binary path
  --clean-runtime         Remove runtime cache before run
  -h, --help              Show this help

Notes:
  - Ctrl+C / Ctrl+Z will both trigger safe shutdown
  - list_file paths:
      * Absolute paths are used directly
      * Relative paths are resolved relative to the list file location
USAGE
}

parse_args() {
    if [ "$#" -eq 0 ]; then
        INPUT_TARGET="."
        return 0
    fi

    # Handle help first
    if [[ "$1" == "-h" || "$1" == "--help" ]]; then
        print_usage
        exit 0
    fi

    # First non-option argument is input target
    if [[ "$1" != -* ]]; then
        INPUT_TARGET="$1"
        shift
    fi

    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                print_usage
                exit 0
                ;;
            --flow-config)
                FLOW_CONFIG_PATH="$2"
                shift 2
                ;;
            --copy)
                ENABLE_COPY_REPORTS=1
                shift
                ;;
            --bg)
                CLI_BG_MAX="$2"
                shift 2
                ;;
            --timeout)
                CLI_TIME_LIMIT="$2"
                shift 2
                ;;
            --report-dst)
                CLI_REPORT_DST_DIR="$2"
                shift 2
                ;;
            --galaxcore)
                CLI_GALAXCORE_BIN="$2"
                shift 2
                ;;
            --clean-runtime)
                KEEP_RUNTIME=0
                shift
                ;;
            *)
                echo "Unknown option: $1" >&2
                print_usage >&2
                exit 1
                ;;
        esac
    done
}
