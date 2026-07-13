#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PJTest daily batch runner.

The runner reads Tasks.yaml, resolves one revision for the entire batch,
writes a YAML snapshot in which every task uses that fixed revision, and then
submits the tasks one by one with a short interval between adjacent tasks.

This guarantees that all tasks created by one daily batch use the same
GalaxCore revision.
"""

import argparse
import copy
import json
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
DEFAULT_DB_PATH = "/home/user3/PJTest/data/task_queue.db"
DEFAULT_YAML_PATH = "/home/user3/PJTest/Tasks.yaml"
DEFAULT_TASKCTL_PATH = "/home/user3/PJTest/taskctl.py"
DEFAULT_BATCH_STATUS_FILE = "/home/user3/PJTest/batch_status.json"
DEFAULT_LOG_FILE = "/home/user3/PJTest/logs/daily_runner.log"
DEFAULT_BATCH_SNAPSHOT_DIR = "/home/user3/PJTest/data/batch_snapshots"
DEFAULT_ZIP_DIR = "/home/xshare/zhouwei_runcache/GalaxCore/zip"
DEFAULT_TASK_SUBMIT_INTERVAL_SECONDS = 30.0

DB_PATH = Path(os.environ.get("PJTEST_DB_PATH", DEFAULT_DB_PATH))
YAML_PATH = Path(os.environ.get("PJTEST_YAML_PATH", DEFAULT_YAML_PATH))
TASKCTL_PATH = Path(os.environ.get("PJTEST_TASKCTL_PATH", DEFAULT_TASKCTL_PATH))
BATCH_STATUS_FILE = Path(
    os.environ.get("PJTEST_BATCH_STATUS_FILE", DEFAULT_BATCH_STATUS_FILE)
)
LOG_FILE = Path(os.environ.get("PJTEST_DAILY_LOG_FILE", DEFAULT_LOG_FILE))
BATCH_SNAPSHOT_DIR = Path(
    os.environ.get("PJTEST_BATCH_SNAPSHOT_DIR", DEFAULT_BATCH_SNAPSHOT_DIR)
)
TASK_SUBMIT_INTERVAL_SECONDS = float(
    os.environ.get(
        "PJTEST_TASK_SUBMIT_INTERVAL_SECONDS",
        DEFAULT_TASK_SUBMIT_INTERVAL_SECONDS,
    )
)

ZIP_RE = re.compile(r"^Galax[Cc]ore_(\d+)\.zip$")


def log_message(message):
    """Write one timestamped message to stdout and the daily log."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (timestamp, message)

    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    with LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(line + "\n")


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


def load_batch_status():
    """Load the current batch status, or return None when absent."""
    if not BATCH_STATUS_FILE.is_file():
        return None

    with BATCH_STATUS_FILE.open("r", encoding="utf-8") as status_file:
        return json.load(status_file)


def write_batch_status(status):
    """Atomically write the current batch status."""
    BATCH_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = BATCH_STATUS_FILE.with_name(
        ".%s.%d.tmp" % (BATCH_STATUS_FILE.name, os.getpid())
    )

    try:
        with temp_path.open("w", encoding="utf-8") as status_file:
            json.dump(status, status_file, indent=2, ensure_ascii=False)
            status_file.write("\n")

        os.replace(str(temp_path), str(BATCH_STATUS_FILE))
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


def finish_current_batch():
    """Mark the recorded batch complete when no unfinished examples remain."""
    status = load_batch_status()

    if not status or status.get("batch_end") is not None:
        return False

    if has_pending_or_running_examples():
        return False

    end_time = datetime.now()
    start_time = datetime.strptime(
        status["batch_start"],
        "%Y-%m-%d %H:%M:%S",
    )
    duration = (end_time - start_time).total_seconds()

    status["batch_end"] = end_time.strftime("%Y-%m-%d %H:%M:%S")
    status["duration_seconds"] = duration
    write_batch_status(status)

    log_message("Batch finished. Duration: %.2f seconds" % duration)
    return True


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


def resolve_batch_revision(data, task_list):
    """Resolve exactly one fixed revision for the complete batch."""
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
        batch_revision = requested
        revision_source = "top-level YAML revision"
    elif requested == "latest":
        batch_revision = None
        revision_source = "top-level latest"
    elif len(fixed_revisions) == 1:
        batch_revision = next(iter(fixed_revisions))
        revision_source = "task-level fixed revision"
    elif len(fixed_revisions) > 1:
        raise ValueError(
            "One batch cannot contain multiple fixed revisions: %s"
            % ", ".join(sorted(fixed_revisions, key=int))
        )
    else:
        batch_revision = None
        revision_source = "latest"

    available_zips = scan_available_zips()

    if not available_zips:
        raise FileNotFoundError(
            "No GalaxCore revision zip was found under: %s"
            % ", ".join(str(path) for path in get_zip_dirs())
        )

    if batch_revision is None:
        resolved_revision = max(available_zips)
        zip_path = available_zips[resolved_revision]
    else:
        resolved_revision = int(batch_revision)
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


def write_batch_yaml_snapshot(data, task_list, revision, start_time):
    """Write a persistent YAML snapshot with one fixed batch revision."""
    fixed_tasks = copy.deepcopy(task_list)

    for entry in fixed_tasks:
        entry["revision"] = str(revision)

    if isinstance(data, dict):
        snapshot_data = copy.deepcopy(data)
        snapshot_data.pop("Task", None)
        snapshot_data["tasks"] = fixed_tasks
    else:
        snapshot_data = {"tasks": fixed_tasks}

    snapshot_data["batch_revision"] = str(revision)
    snapshot_data["source_yaml"] = str(YAML_PATH)
    snapshot_data["snapshot_created_at"] = start_time.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    BATCH_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    filename = "batch_%s_r%s.yaml" % (
        start_time.strftime("%Y%m%d_%H%M%S"),
        revision,
    )
    snapshot_path = BATCH_SNAPSHOT_DIR / filename
    temp_path = snapshot_path.with_name(
        ".%s.%d.tmp" % (snapshot_path.name, os.getpid())
    )

    try:
        with temp_path.open("w", encoding="utf-8") as snapshot_file:
            yaml.safe_dump(
                snapshot_data,
                snapshot_file,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )

        os.replace(str(temp_path), str(snapshot_path))
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass

    return snapshot_path


def run_taskctl_apply(yaml_path, interval_seconds=None):
    """Submit snapshot tasks one by one, spacing adjacent submissions."""
    if interval_seconds is None:
        interval_seconds = TASK_SUBMIT_INTERVAL_SECONDS

    if interval_seconds < 0:
        raise ValueError("task submit interval cannot be negative")

    try:
        with Path(yaml_path).open("r", encoding="utf-8") as yaml_file:
            data = yaml.safe_load(yaml_file)

        if isinstance(data, dict) and "tasks" in data:
            task_list = data["tasks"]
        elif isinstance(data, list):
            task_list = data
        else:
            raise ValueError(
                "YAML must contain a list or a dictionary with a 'tasks' key."
            )

        if not isinstance(task_list, list) or not task_list:
            raise ValueError("YAML task list is empty.")
    except Exception as exc:
        log_message("Failed to load batch snapshot: %s" % exc)
        return False

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


def start_new_batch():
    """Resolve one revision and create every task from a fixed YAML snapshot."""
    if not YAML_PATH.is_file():
        log_message("YAML file not found: %s" % YAML_PATH)
        return False

    start_time = datetime.now()

    try:
        data, task_list = load_task_suite(YAML_PATH)
        revision, zip_path, revision_source = resolve_batch_revision(
            data,
            task_list,
        )
        snapshot_path = write_batch_yaml_snapshot(
            data,
            task_list,
            revision,
            start_time,
        )
    except Exception as exc:
        log_message("Batch preparation failed: %s" % exc)
        return False

    status = {
        "batch_start": start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "batch_end": None,
        "duration_seconds": None,
        "yaml_file": str(YAML_PATH),
        "batch_yaml_snapshot": str(snapshot_path),
        "batch_revision": revision,
        "revision_source": revision_source,
        "zip_path": str(zip_path),
        "task_count": len(task_list),
        "task_submit_interval_seconds": TASK_SUBMIT_INTERVAL_SECONDS,
    }
    write_batch_status(status)

    log_message("Started new batch at %s" % status["batch_start"])
    log_message(
        "Batch revision fixed to %s using %s"
        % (revision, zip_path)
    )
    log_message("Batch YAML snapshot: %s" % snapshot_path)

    success = run_taskctl_apply(snapshot_path)

    if not success:
        end_time = datetime.now()
        status["batch_end"] = end_time.strftime("%Y-%m-%d %H:%M:%S")
        status["duration_seconds"] = (
            end_time - start_time
        ).total_seconds()
        status["error"] = "task submission failed"
        write_batch_status(status)
        log_message("Batch creation failed, status updated.")
        return False

    log_message(
        "Batch creation completed successfully. "
        "All %d tasks use revision %s."
        % (len(task_list), revision)
    )
    return True


def main():
    """Run the daily batch workflow."""
    parser = argparse.ArgumentParser(
        description="PJTest daily batch runner"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only update the current batch status; do not create a batch.",
    )
    args = parser.parse_args()

    log_message("=== Daily runner started ===")

    if finish_current_batch():
        log_message("Previous batch was finished and recorded.")

    if args.check_only:
        log_message("Check-only mode, exiting.")
        return 0

    if has_pending_or_running_examples():
        log_message(
            "There are pending/running examples. "
            "Skipping new batch creation."
        )
        return 0

    status = load_batch_status()

    if status and status.get("batch_end") is None:
        log_message(
            "Batch status is active but the database has no unfinished "
            "examples. Forcing the old batch to finish."
        )
        status["batch_end"] = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        status["duration_seconds"] = 0
        status["error"] = "forced finish due to inconsistent status"
        write_batch_status(status)

    success = start_new_batch()

    log_message("=== Daily runner finished ===")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
