#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility entry point for the vertical PJTest revision-scan task."""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def default_taskctl_path():
    value = os.environ.get("PJTEST_TASKCTL")
    if value:
        return value
    local = Path(__file__).resolve().parents[1] / "taskctl.py"
    return str(local if local.is_file() else Path.cwd() / "taskctl.py")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Create one distributed revision-scan PJTest task."
    )
    parser.add_argument("template", help="template name, for example find_fail")
    parser.add_argument("run_tcl", help="one run.tcl path")
    parser.add_argument("--good", type=int, default=None)
    parser.add_argument("--bad", type=int, default=None)
    parser.add_argument("--confirm-fail", type=int, default=None)
    parser.add_argument("--priority", type=int, default=None)
    parser.add_argument("--max-retry", type=int, default=None)
    parser.add_argument("--max-time", type=int, default=None)
    parser.add_argument("--name", default=None)
    parser.add_argument("--taskctl", default=default_taskctl_path())
    return parser


def main():
    args = build_parser().parse_args()
    argv = [sys.executable, args.taskctl, "find", args.template, args.run_tcl]
    for option, value in [
        ("--good", args.good),
        ("--bad", args.bad),
        ("--confirm-fail", args.confirm_fail),
        ("--priority", args.priority),
        ("--max-retry", args.max_retry),
        ("--max-time", args.max_time),
        ("--name", args.name),
    ]:
        if value is not None:
            argv.extend([option, str(value)])
    return subprocess.call(argv)


if __name__ == "__main__":
    raise SystemExit(main())
