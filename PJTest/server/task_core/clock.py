#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PJTest daily task submitter.

The runner reads task.yaml, resolves one revision for the complete invocation,
and submits the configured tasks one by one. Durable history lives in the task
database and the date-based clock log; no separate batch status or snapshot is
maintained.
"""

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml


# Paths can be overridden through environment variables.
SERVER_ROOT = Path(__file__).resolve().parents[1]
PJTEST_ROOT = SERVER_ROOT.parent
DEFAULT_DB_PATH = str(PJTEST_ROOT / "data" / "task_queue.db")
DEFAULT_YAML_PATH = str(SERVER_ROOT / "task.yaml")
DEFAULT_TASKCTL_PATH = str(SERVER_ROOT / "taskctl.py")
DEFAULT_LOG_DIR = str(PJTEST_ROOT / "logs" / "clock")
DEFAULT_ZIP_DIR = "/home/xshare/zhouwei_runcache/GalaxCore/zip"
DEFAULT_TASK_SUBMIT_INTERVAL_SECONDS = 30.0

DB_PATH = Path(os.environ.get("PJTEST_DB_PATH", DEFAULT_DB_PATH))
YAML_PATH = Path(os.environ.get("PJTEST_YAML_PATH", DEFAULT_YAML_PATH))
TASKCTL_PATH = Path(os.environ.get("PJTEST_TASKCTL_PATH", DEFAULT_TASKCTL_PATH))
LOG_DIR = Path(os.environ.get("PJTEST_CLOCK_LOG_DIR", DEFAULT_LOG_DIR))
TASK_SUBMIT_INTERVAL_SECONDS = float(
    os.environ.get(
        "PJTEST_TASK_SUBMIT_INTERVAL_SECONDS",
        DEFAULT_TASK_SUBMIT_INTERVAL_SECONDS,
    )
)

ZIP_RE = re.compile(r"^Galax[Cc]ore_(\d+)\.zip$")


def get_log_file(now=None):
    """Return the clock log path for one local calendar day."""
    now = now or datetime.now()
    return LOG_DIR / ("clock_%s.log" % now.strftime("%Y-%m-%d"))


def log_message(message):
    """Write one timestamped message to stdout and the daily log."""
    now = datetime.now()
    line = "[%s] %s" % (now.strftime("%Y-%m-%d %H:%M:%S"), message)
    log_file = get_log_file(now)

    print(line, flush=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    with log_file.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def get_db_connection():
    """Return a configured SQLite connection."""
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def has_pending_or_running_examples():
    """Return whether any example is currently pending or running."""
    conn = get_db_connection()

    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM task_examples
            WHERE status IN ('pending', 'running')
            """
        ).fetchone()
        return int(row["cnt"] or 0) > 0
    finally:
        conn.close()


def get_zip_dirs():
    """Return configured GalaxCore zip directories."""
    override = os.environ.get("GALAXCORE_ZIP_DIR")

    if not override:
        return [Path(DEFAULT_ZIP_DIR)]

    return [
        Path(item).expanduser()
        for item in override.split(os.pathsep)
        if item.strip()
    ]


def scan_available_zips():
    """Return available revisions mapped to zip paths."""
    available = {}

    for zip_dir in get_zip_dirs():
        if not zip_dir.is_dir():
            continue

        for item in zip_dir.iterdir():
            if not item.is_file():
                continue

            match = ZIP_RE.match(item.name)
            if not match:
                continue

            revision = int(match.group(1))
            available[revision] = item.resolve()

    return available


def normalize_revision(value, field_name):
    """Normalize one revision value to latest or a numeric string."""
    if value is None or str(value).strip() == "":
        return "latest"

    text = str(value).strip()

    if text.lower() == "latest":
        return "latest"

    if not re.match(r"^\d+$", text):
        raise ValueError(
            "%s must be a number or latest, got: %s" % (field_name, text)
        )

    return str(int(text))


def load_task_suite(yaml_path):
    """Load and validate the source YAML task list."""
    with yaml_path.open("r", encoding="utf-8") as yaml_file:
        data = yaml.safe_load(yaml_file)

    if isinstance(data, dict):
        if "tasks" in data:
            task_list = data["tasks"]
        elif "Task" in data:
            task_list = data["Task"]
        else:
            raise ValueError(
                "YAML must contain a 'tasks' list."
            )
    elif isinstance(data, list):
        task_list = data
    else:
        raise ValueError(
            "YAML must be a task list or a dictionary containing 'tasks'."
        )

    if not isinstance(task_list, list) or not task_list:
        raise ValueError("YAML task list is empty.")

    for index, entry in enumerate(task_list, 1):
        if not isinstance(entry, dict):
            raise ValueError(
                "Task entry %d must be a YAML mapping." % index
            )
        if not entry.get("template"):
            raise ValueError(
                "Task entry %d is missing 'template'." % index
            )

    return data, task_list


def resolve_run_revision(data, task_list):
    """Resolve exactly one fixed revision for this clock invocation."""
    requested = None

    if isinstance(data, dict):
        if "batch_revision" in data:
            requested = normalize_revision(
                data.get("batch_revision"),
                "batch_revision",
            )
        elif "revision" in data:
            requested = normalize_revision(
                data.get("revision"),
                "revision",
            )

    fixed_revisions = set()

    for index, entry in enumerate(task_list, 1):
        entry_revision = normalize_revision(
            entry.get("revision"),
            "tasks[%d].revision" % index,
        )
        if entry_revision != "latest":
            fixed_revisions.add(entry_revision)

    if requested and requested != "latest":
        run_revision = requested
        revision_source = "top-level YAML revision"
    elif requested == "latest":
        run_revision = None
        revision_source = "top-level latest"
    elif len(fixed_revisions) == 1:
        run_revision = next(iter(fixed_revisions))
        revision_source = "task-level fixed revision"
    elif len(fixed_revisions) > 1:
        raise ValueError(
            "One clock invocation cannot contain multiple fixed revisions: %s"
            % ", ".join(sorted(fixed_revisions, key=int))
        )
    else:
        run_revision = None
        revision_source = "latest"

    available_zips = scan_available_zips()

    if not available_zips:
        raise FileNotFoundError(
            "No GalaxCore revision zip was found under: %s"
            % ", ".join(str(path) for path in get_zip_dirs())
        )

    if run_revision is None:
        resolved_revision = max(available_zips)
        zip_path = available_zips[resolved_revision]
    else:
        resolved_revision = int(run_revision)
        zip_path = available_zips.get(resolved_revision)

        if zip_path is None:
            raise FileNotFoundError(
                "GalaxCore_%d.zip was not found under: %s"
                % (
                    resolved_revision,
                    ", ".join(str(path) for path in get_zip_dirs()),
                )
            )

    return str(resolved_revision), zip_path, revision_source


def submit_tasks(task_list, revision, interval_seconds=None):
    """Submit tasks one by one with one revision pinned for the whole run."""
    if interval_seconds is None:
        interval_seconds = TASK_SUBMIT_INTERVAL_SECONDS

    if interval_seconds < 0:
        raise ValueError("task submit interval cannot be negative")

    success = 0
    failed = 0
    total = len(task_list)

    log_message(
        "Submitting %d tasks one by one; interval between tasks: %.1f seconds"
        % (total, interval_seconds)
    )

    for index, entry in enumerate(task_list, 1):
        if not isinstance(entry, dict):
            failed += 1
            log_message(
                "[%d/%d] Invalid task entry, expected a mapping."
                % (index, total)
            )
        else:
            entry = dict(entry)
            entry["revision"] = str(revision)
            template = entry.get("template")
            target = entry.get("target")

            if not template:
                failed += 1
                log_message(
                    "[%d/%d] Missing task template, submission skipped."
                    % (index, total)
                )
            else:
                command = [
                    sys.executable,
                    str(TASKCTL_PATH),
                    "add",
                    str(template),
                ]
                if target is not None:
                    command.append(str(target))

                option_map = (
                    ("revision", "--revision"),
                    ("name", "--name"),
                    ("priority", "--priority"),
                    ("max_retry", "--max-retry"),
                    ("max_time", "--max-time"),
                )
                for field_name, option_name in option_map:
                    value = entry.get(field_name)
                    if value is not None:
                        command.extend([option_name, str(value)])

                log_message(
                    "[%d/%d] Running: %s"
                    % (index, total, " ".join(command))
                )

                try:
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        universal_newlines=True,
                        bufsize=1,
                    )

                    if process.stdout is not None:
                        for line in process.stdout:
                            log_message("  %s" % line.rstrip())

                    return_code = process.wait()
                    if return_code == 0:
                        success += 1
                        log_message(
                            "[%d/%d] Task submitted successfully: %s"
                            % (index, total, template)
                        )
                    else:
                        failed += 1
                        log_message(
                            "[%d/%d] taskctl add exited with code %d: %s"
                            % (index, total, return_code, template)
                        )
                except Exception as exc:
                    failed += 1
                    log_message(
                        "[%d/%d] Failed to run taskctl add for %s: %s"
                        % (index, total, template, exc)
                    )

        # Delay only between adjacent task submissions, never after the last one.
        if index < total:
            log_message(
                "Waiting %.1f seconds before submitting the next task."
                % interval_seconds
            )
            time.sleep(interval_seconds)

    log_message(
        "Task submission summary: SUCCESS=%d FAILED=%d"
        % (success, failed)
    )
    return failed == 0


def submit_daily_tasks():
    """Resolve one revision and submit every task from task.yaml."""
    if not YAML_PATH.is_file():
        log_message("YAML file not found: %s" % YAML_PATH)
        return False

    start_time = datetime.now()

    try:
        data, task_list = load_task_suite(YAML_PATH)
        revision, zip_path, revision_source = resolve_run_revision(
            data,
            task_list,
        )
    except Exception as exc:
        log_message("Daily task preparation failed: %s" % exc)
        return False

    log_message("Task YAML: %s" % YAML_PATH)
    log_message(
        "Revision fixed to %s using %s (%s)"
        % (revision, zip_path, revision_source)
    )

    success = submit_tasks(task_list, revision)
    elapsed = (datetime.now() - start_time).total_seconds()

    if not success:
        log_message("Daily task submission failed after %.2f seconds." % elapsed)
        return False

    log_message(
        "Daily task submission completed in %.2f seconds. "
        "All %d tasks use revision %s."
        % (elapsed, len(task_list), revision)
    )
    return True


def main():
    """Run the daily task submission workflow."""
    parser = argparse.ArgumentParser(
        description="PJTest daily task runner"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only report whether unfinished examples exist; do not submit tasks.",
    )
    args = parser.parse_args()

    log_message("=== Daily task runner started ===")

    if args.check_only:
        active = has_pending_or_running_examples()
        log_message(
            "Check-only result: unfinished examples %s."
            % ("exist" if active else "do not exist")
        )
        log_message("=== Daily task runner finished: CHECKED ===")
        return 0

    if has_pending_or_running_examples():
        log_message(
            "There are pending/running examples. "
            "Skipping daily task submission."
        )
        log_message("=== Daily task runner finished: SKIPPED ===")
        return 0

    success = submit_daily_tasks()

    log_message(
        "=== Daily task runner finished: %s ==="
        % ("SUCCESS" if success else "FAILED")
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
