#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PJTest task control tool.

This tool is used on the central scheduler host only.  It creates parent tasks
and splits them into task_examples.  Workers never use taskctl.py and never
access the SQLite database directly.

Usage:
    ./taskctl.py add <template_name> [target_dir] [--revision REV]
    ./taskctl.py list [--status STATUS] [--limit N]
    ./taskctl.py show [task_id]
    ./taskctl.py examples <task_id> [-v]
    ./taskctl.py attempts <example_id>
    ./taskctl.py workers
    ./taskctl.py check
    ./taskctl.py cancel <task_id> [--force]
    ./taskctl.py delete <task_id> [--force]
"""

import yaml  # 新增
import argparse
import json
import os
import re
import shlex
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

try:
    from urllib.request import urlopen
    from urllib.error import HTTPError, URLError
except ImportError:  # pragma: no cover, Python 2 fallback is not expected.
    from urllib2 import urlopen, HTTPError, URLError


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get(
    "PJTEST_DB_PATH",
    "/home/user3/PJTest/data/task_queue.db",
))
TEMPLATE_DIR = Path(os.environ.get(
    "PJTEST_TEMPLATE_DIR",
    str(BASE_DIR / "templates"),
))

DEFAULT_WORK_ROOT = "/home/user3/workspace/galaxcore/test2"

IGNORED_DIRS = {
    ".git",
    ".svn",
    "__pycache__",
    "output",
    "outputs",
    "result",
    "results",
    "report",
    "reports",
    "log",
    "logs",
}

DEFAULT_FLOW_CONFIG = {
    "report_timing_summary": 0,
    "opt_design": 0,
    "place_design": 1,
    "place_design_from_syn": 0,
    "phys_opt_design": 0,
    "route_design": 1,
    "route_design_from_place": 0,
    "write_checkpoint": 0,
    "write_bitstream": 1,
    "bit_cmp": 1,
    "msk_cmp": 1,
    "bgn_cmp": 1,
    "dcp_cmp": 0,
    "checksum_cmp": 0,
    "enable_copy": 1,
}


TERMINAL_EXAMPLE_STATUSES = set(["success", "failed", "timeout", "canceled"])
TERMINAL_TASK_STATUSES = set(["success", "failed", "canceled"])
SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get("PJTEST_SQLITE_BUSY_TIMEOUT_MS", "60000"))


def get_conn():
    """Create a SQLite connection with runtime pragmas."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=%d" % SQLITE_BUSY_TIMEOUT_MS)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def local_now():
    """Return local time string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_template_name(template_name):
    """Normalize a template name by removing optional .json suffix."""
    if template_name.endswith(".json"):
        return template_name[:-5]
    return template_name


def load_template(template_name):
    """Load template JSON from templates directory."""
    normalized = normalize_template_name(template_name)
    template_path = TEMPLATE_DIR / (normalized + ".json")

    if not template_path.is_file():
        raise FileNotFoundError("Template not found: %s" % template_path)

    with template_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("Template must be a JSON object: %s" % template_path)

    return normalized, data


def ensure_inside_work_root(work_root, target_path):
    """Ensure target_path is inside work_root."""
    root = Path(work_root).resolve()
    target = Path(target_path).resolve()

    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("Target escapes work_root: %s" % target)

    return root, target


def to_posix_relative(path, root):
    """Return a POSIX-style relative path."""
    rel_text = Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    return rel_text if rel_text else "."


def append_example_from_run_tcl(examples, root, run_tcl_path, source_label):
    """Append one run.tcl example from an absolute path."""
    path = Path(run_tcl_path).expanduser()

    if not path.is_absolute():
        raise ValueError(
            "List file entries must be absolute run.tcl paths: %s" % source_label
        )

    path = path.resolve()

    if path.name != "run.tcl":
        raise ValueError("List entry must point to run.tcl: %s" % path)

    if not path.is_file():
        raise ValueError("run.tcl file not found: %s" % path)

    ensure_inside_work_root(root, path)
    run_tcl_rel = to_posix_relative(path, root)
    examples.append({
        "target_arg": run_tcl_rel,
        "run_tcl_path": run_tcl_rel,
    })


def load_examples_from_list_file(work_root, list_file):
    """Load examples from an absolute list file.

    Each non-empty, non-comment line must be an absolute path to a run.tcl file
    under work_root.
    """
    root = Path(work_root).resolve()
    list_path = Path(list_file).expanduser()

    if not list_path.is_absolute():
        raise ValueError("Example list file must be an absolute path: %s" % list_file)

    list_path = list_path.resolve()

    if not list_path.is_file():
        raise ValueError("Example list file not found: %s" % list_path)

    examples = []
    seen = set()

    with list_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line_no, raw_line in enumerate(f, 1):
            line = raw_line.strip()

            if not line or line.startswith("#"):
                continue

            if line in seen:
                continue

            append_example_from_run_tcl(
                examples,
                root,
                line,
                "%s:%d" % (list_path, line_no),
            )
            seen.add(line)

    if not examples:
        raise ValueError("No run.tcl entries found in list file: %s" % list_path)

    return examples


def discover_examples(work_root, target_dir):
    """Discover run.tcl files from a directory or an absolute list file."""
    root = Path(work_root).resolve()
    target_input = Path(target_dir).expanduser()

    # File mode: the CLI argument itself must be an absolute list file.
    if target_input.is_absolute() and target_input.is_file():
        return load_examples_from_list_file(root, target_input)

    examples = []

    # Relative file targets are intentionally rejected.  A file argument must be
    # an absolute list file whose content is one absolute run.tcl path per line.
    if not target_input.is_absolute():
        cwd_file = target_input.resolve()
        work_root_file = (root / target_input).resolve()

        if cwd_file.is_file() or work_root_file.is_file():
            raise ValueError(
                "File target must be an absolute list file, not a relative file: %s"
                % target_dir
            )

    target_path = (
        target_input.resolve()
        if target_input.is_absolute()
        else (root / target_input).resolve()
    )
    root, target_path = ensure_inside_work_root(root, target_path)

    if target_path.is_file():
        raise ValueError(
            "Direct file targets are not supported. Use an absolute list file "
            "containing absolute run.tcl paths: %s" % target_dir
        )

    if not target_path.is_dir():
        raise ValueError("Target directory does not exist: %s" % target_path)

    for current_root, dirnames, filenames in os.walk(str(target_path)):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in IGNORED_DIRS and not d.startswith(".nfs")
        )

        if "run.tcl" not in filenames:
            continue

        run_tcl_path = Path(current_root) / "run.tcl"
        run_tcl_rel = to_posix_relative(run_tcl_path, root)
        examples.append({
            "target_arg": run_tcl_rel,
            "run_tcl_path": run_tcl_rel,
        })

    examples.sort(key=lambda item: item["target_arg"])

    if not examples:
        raise ValueError("No run.tcl found under: %s" % target_path)

    return examples


def build_run_command(work_root, target_arg):
    """Build the command displayed for this example."""
    return "cd %s && ./run.sh %s" % (
        shlex.quote(str(work_root)),
        shlex.quote(str(target_arg)),
    )


def merge_flow_config(template):
    """Merge default flow_config with template flow_config."""
    flow_config = dict(DEFAULT_FLOW_CONFIG)
    template_flow = template.get("flow_config", {})

    if template_flow is None:
        template_flow = {}

    if not isinstance(template_flow, dict):
        raise ValueError("template.flow_config must be a JSON object")

    flow_config.update(template_flow)
    return flow_config


def get_int_value(args_value, template, key, default_value):
    """Read an integer from CLI first, then template, then default."""
    if args_value is not None:
        return int(args_value)
    return int(template.get(key, default_value))


def parse_revision_arg(revision_arg):
    """Return (revision_policy, revision) from CLI --revision."""
    if revision_arg is None or str(revision_arg).strip() == "":
        return "latest", "latest"

    text = str(revision_arg).strip()
    if text.lower() == "latest":
        return "latest", "latest"

    if not re.match(r"^\d+$", text):
        raise ValueError("Revision must be a number or latest: %s" % text)

    return "fixed", text


def insert_event(cur, task_id, example_id, attempt_id, worker_name, event, message):
    """Insert one central event."""
    cur.execute(
        """
        INSERT INTO task_events (
            task_id,
            example_id,
            attempt_id,
            worker_name,
            event,
            message,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            example_id,
            attempt_id,
            worker_name,
            event,
            message,
            local_now(),
        ),
    )


def insert_task_and_examples(
    task_name,
    template_name,
    revision_policy,
    revision,
    suite,
    priority,
    max_retry,
    max_time,
    work_root,
    target_dir,
    flow_config,
    examples,
):
    """Insert one parent task and all child examples."""
    conn = get_conn()
    cur = conn.cursor()
    now = local_now()

    task_id = "task_" + uuid.uuid4().hex[:12]

    try:
        cur.execute("BEGIN IMMEDIATE")

        cur.execute(
            """
            INSERT INTO tasks (
                task_id,
                task_name,
                template_name,
                revision,
                revision_policy,
                resolved_zip_path,
                suite,
                target_worker,
                priority,
                max_retry,
                max_time,
                work_root,
                target_dir,
                split_mode,
                flow_config_json,
                status,
                total_examples,
                repeat_enabled,
                repeat_group,
                repeat_index,
                parent_task_id,
                created_at,
                updated_at,
                started_at,
                finished_at,
                message
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, 'any', ?, ?, ?, ?, ?, 'scan', ?,
                      'pending', ?, 0, NULL, 1, NULL, ?, ?, NULL, NULL, ?)
            """,
            (
                task_id,
                task_name,
                template_name,
                revision,
                revision_policy,
                suite,
                priority,
                max_retry,
                max_time,
                work_root,
                target_dir,
                json.dumps(flow_config, ensure_ascii=False, sort_keys=True),
                len(examples),
                now,
                now,
                "created with %d examples" % len(examples),
            ),
        )

        for seq, example in enumerate(examples, 1):
            example_id = "ex_" + uuid.uuid4().hex[:12]
            target_arg = example["target_arg"]
            run_tcl_path = example["run_tcl_path"]
            cmd = build_run_command(work_root, target_arg)

            cur.execute(
                """
                INSERT INTO task_examples (
                    example_id,
                    task_id,
                    seq,
                    platform,
                    target_arg,
                    run_tcl_path,
                    cmd,
                    status,
                    assigned_worker,
                    current_attempt_id,
                    retry_count,
                    max_retry,
                    created_at,
                    updated_at,
                    started_at,
                    finished_at,
                    exit_code,
                    failed_step,
                    failed_reason,
                    message,
                    log_file,
                    log_tail,
                    run_log_dir,
                    report_dir
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, 'pending', NULL, NULL, 0, ?,
                          ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                          NULL, NULL)
                """,
                (
                    example_id,
                    task_id,
                    seq,
                    target_arg,
                    run_tcl_path,
                    cmd,
                    max_retry,
                    now,
                    now,
                ),
            )

        insert_event(
            cur,
            task_id,
            None,
            None,
            None,
            "task_created",
            "Created task with %d examples" % len(examples),
        )

        conn.commit()
        return task_id

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def cmd_add(args):
    """Create one parent task and split it into examples."""
    template_name, template = load_template(args.template_name)
    target_dir = args.target_dir or "."

    work_root = template.get("work_root", DEFAULT_WORK_ROOT)
    suite = template.get("suite", "night_build")
    priority = get_int_value(args.priority, template, "priority", 100)
    max_retry = get_int_value(args.max_retry, template, "max_retry", 1)
    max_time = get_int_value(args.max_time, template, "max_time", 3600)

    task_name = args.name or template.get("task_name") or template_name
    revision_policy, revision = parse_revision_arg(args.revision)
    flow_config = merge_flow_config(template)

    print("Template        : %s" % template_name)
    print("Work root       : %s" % work_root)
    print("Target dir      : %s" % target_dir)
    print("Revision policy : %s" % revision_policy)
    if revision_policy == "latest":
        print("Revision        : resolved by scheduler when pulled")
    else:
        print("Revision        : %s" % revision)
    print("Scanning run.tcl files ...")

    examples = discover_examples(work_root, target_dir)
    print("Found           : %d examples" % len(examples))

    task_id = insert_task_and_examples(
        task_name=task_name,
        template_name=template_name,
        revision_policy=revision_policy,
        revision=revision,
        suite=suite,
        priority=priority,
        max_retry=max_retry,
        max_time=max_time,
        work_root=work_root,
        target_dir=target_dir,
        flow_config=flow_config,
        examples=examples,
    )

    print("")
    print("Task created    : %s" % task_id)
    print("Status          : pending")


def format_short(text, width):
    """Return full text for table display.

    The width argument is kept for compatibility with existing print format
    calls, but values are no longer truncated.  Long target paths are shown
    completely so debugging does not require a separate verbose mode.
    """
    if text is None:
        return "-"

    return str(text)


def format_revision(row):
    """Format revision policy and resolved revision for display."""
    policy = row["revision_policy"] or "fixed"
    revision = row["revision"]

    if policy == "latest":
        if revision and str(revision).lower() != "latest":
            return "latest->%s" % revision
        return "latest"

    return revision or "-"


def cmd_list(args):
    """List the most recent parent tasks with aggregate progress."""
    limit = int(args.limit)
    if limit <= 0:
        raise ValueError("--limit must be greater than 0")

    conn = get_conn()
    cur = conn.cursor()

    active_predicate = """
        EXISTS (
            SELECT 1
            FROM workers w
            WHERE w.status IN ('running', 'reporting')
              AND w.current_task_id = e.task_id
              AND w.current_example_id = e.example_id
              AND w.current_attempt_id = e.current_attempt_id
        )
    """

    # Select only the requested parent tasks first. This prevents SQLite from
    # aggregating every historical task and all of its examples before LIMIT.
    conditions = []
    params = []

    if args.status:
        conditions.append("status = ?")
        params.append(args.status)

    recent_tasks_query = """
        SELECT *
        FROM tasks
    """

    if conditions:
        recent_tasks_query += " WHERE " + " AND ".join(conditions)

    recent_tasks_query += """
        ORDER BY id DESC
        LIMIT ?
    """
    params.append(limit)

    query = """
        SELECT
            t.id,
            t.task_id,
            t.task_name,
            t.template_name,
            t.revision,
            t.revision_policy,
            t.suite,
            t.target_dir,
            t.status,
            t.created_at,
            t.priority,
            t.total_examples,
            COUNT(e.example_id) AS real_total,
            SUM(CASE WHEN e.status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
            SUM(CASE WHEN e.status = 'running' THEN 1 ELSE 0 END) AS db_running_count,
            SUM(
                CASE
                    WHEN e.status = 'running' AND %s THEN 1
                    ELSE 0
                END
            ) AS active_running_count,
            SUM(
                CASE
                    WHEN e.status = 'running' AND NOT (%s) THEN 1
                    ELSE 0
                END
            ) AS stale_running_count,
            SUM(CASE WHEN e.status = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(
                CASE
                    WHEN e.status IN ('failed', 'timeout') THEN 1
                    ELSE 0
                END
            ) AS failed_count,
            SUM(
                CASE
                    WHEN e.status IN (
                        'success',
                        'failed',
                        'timeout',
                        'canceled'
                    ) THEN 1
                    ELSE 0
                END
            ) AS done_count
        FROM (
            %s
        ) AS t
        LEFT JOIN task_examples e
            ON t.task_id = e.task_id
        GROUP BY t.id, t.task_id
        ORDER BY t.id DESC
    """ % (
        active_predicate,
        active_predicate,
        recent_tasks_query,
    )

    try:
        cur.execute(query, params)
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print("No tasks found.")
        return

    header = (
        "%-18s %-10s %-12s %-12s %-22s %5s %5s %7s %6s %7s %5s %7s %-13s %-8s %s"
        % (
            "TASK_ID",
            "TEMPLATE",
            "REV",
            "STATUS",
            "TARGET",
            "TOTAL",
            "DONE",
            "SUCCESS",
            "FAILED",
            "RUNNING",
            "STALE",
            "PENDING",
            "PROGRESS",
            "PRI",
            "CREATED_AT",
        )
    )
    print(header)
    print("-" * len(header))

    for row in rows:
        total = row["real_total"] or row["total_examples"] or 0
        done = row["done_count"] or 0
        success = row["success_count"] or 0
        failed = row["failed_count"] or 0
        running = row["active_running_count"] or 0
        stale = row["stale_running_count"] or 0
        pending = row["pending_count"] or 0

        if total > 0:
            progress = "%d/%d %.1f%%" % (done, total, done * 100.0 / total)
        else:
            progress = "-"

        print(
            "%-18s %-10s %-12s %-12s %-22s %5d %5d %7d %6d %7d %5d %7d %-13s %-8s %s"
            % (
                format_short(row["task_id"], 18),
                format_short(row["template_name"], 10),
                format_short(format_revision(row), 12),
                format_short(row["status"], 12),
                format_short(row["target_dir"], 22),
                total,
                done,
                success,
                failed,
                running,
                stale,
                pending,
                progress,
                row["priority"],
                row["created_at"],
            )
        )

def cmd_examples(args):
    """List examples for a parent task."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT task_id, template_name, revision, revision_policy, target_dir,
               status, total_examples, created_at, message
        FROM tasks
        WHERE task_id = ?
        """,
        (args.task_id,),
    )
    task = cur.fetchone()

    if not task:
        conn.close()
        print("Task not found: %s" % args.task_id)
        return

    query = """
        SELECT example_id, seq, target_arg, run_tcl_path, status,
               assigned_worker, current_attempt_id, retry_count, max_retry,
               exit_code, failed_step, failed_reason, started_at, finished_at,
               log_file, run_log_dir, report_dir, message
        FROM task_examples
        WHERE task_id = ?
    """
    params = [args.task_id]

    if args.status:
        query += " AND status = ?"
        params.append(args.status)

    query += " ORDER BY seq"

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    print("Task ID      : %s" % task["task_id"])
    print("Template     : %s" % task["template_name"])
    print("Revision     : %s" % format_revision(task))
    print("Target dir   : %s" % task["target_dir"])
    print("Task status  : %s" % task["status"])
    print("Created at   : %s" % task["created_at"])
    if task["message"]:
        print("Message      : %s" % task["message"])
    print("")

    if not rows:
        print("No examples found.")
        return

    header = "%-18s %4s %-10s %-10s %-7s %-6s %-45s" % (
        "EXAMPLE_ID",
        "SEQ",
        "STATUS",
        "WORKER",
        "RETRY",
        "EXIT",
        "TARGET_ARG",
    )
    print(header)
    print("-" * len(header))

    for row in rows:
        worker = row["assigned_worker"] or "-"
        retry = "%d/%d" % (row["retry_count"], row["max_retry"])
        exit_code = "-" if row["exit_code"] is None else str(row["exit_code"])

        print(
            "%-18s %4d %-10s %-10s %-7s %-6s %-45s"
            % (
                format_short(row["example_id"], 18),
                row["seq"],
                format_short(row["status"], 10),
                format_short(worker, 10),
                retry,
                exit_code,
                format_short(row["target_arg"], 45),
            )
        )

        if args.verbose:
            print("    run_tcl_path       : %s" % row["run_tcl_path"])
            if row["current_attempt_id"]:
                print("    current_attempt_id : %s" % row["current_attempt_id"])
            if row["failed_step"] or row["failed_reason"]:
                print("    failed_step        : %s" % (row["failed_step"] or "-"))
                print("    failed_reason      : %s" % (row["failed_reason"] or "-"))
            if row["message"]:
                print("    message            : %s" % row["message"])
            if row["log_file"]:
                print("    log_file           : %s" % row["log_file"])
            if row["run_log_dir"]:
                print("    run_log_dir        : %s" % row["run_log_dir"])
            if row["report_dir"]:
                print("    report_dir         : %s" % row["report_dir"])


def cmd_attempts(args):
    """List attempts for one example."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT attempt_id, attempt_no, task_id, example_id, worker_name,
               status, revision, started_at, finished_at, exit_code,
               timed_out, log_file, run_log_dir, report_dir, message
        FROM task_attempts
        WHERE example_id = ?
        ORDER BY attempt_no ASC, id ASC
        """,
        (args.example_id,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("No attempts found for example: %s" % args.example_id)
        return

    header = "%-18s %4s %-10s %-10s %-8s %-6s %-19s %-19s" % (
        "ATTEMPT_ID",
        "NO",
        "STATUS",
        "WORKER",
        "REV",
        "EXIT",
        "STARTED_AT",
        "FINISHED_AT",
    )
    print(header)
    print("-" * len(header))

    for row in rows:
        exit_code = "-" if row["exit_code"] is None else str(row["exit_code"])
        print(
            "%-18s %4d %-10s %-10s %-8s %-6s %-19s %-19s"
            % (
                format_short(row["attempt_id"], 18),
                row["attempt_no"],
                format_short(row["status"], 10),
                format_short(row["worker_name"], 10),
                format_short(row["revision"], 8),
                exit_code,
                row["started_at"] or "-",
                row["finished_at"] or "-",
            )
        )

        if args.verbose:
            if row["message"]:
                print("    message     : %s" % row["message"])
            if row["log_file"]:
                print("    log_file    : %s" % row["log_file"])
            if row["run_log_dir"]:
                print("    run_log_dir : %s" % row["run_log_dir"])
            if row["report_dir"]:
                print("    report_dir  : %s" % row["report_dir"])


def cmd_delete(args):
    """Delete one task and its examples."""
    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute("BEGIN IMMEDIATE")

        cur.execute(
            "SELECT task_id, status FROM tasks WHERE task_id = ?",
            (args.task_id,),
        )
        task = cur.fetchone()

        if not task:
            conn.rollback()
            print("Task not found: %s" % args.task_id)
            return

        cur.execute(
            """
            SELECT COUNT(*) AS running_count
            FROM task_examples
            WHERE task_id = ?
              AND status = 'running'
            """,
            (args.task_id,),
        )
        running_count = cur.fetchone()["running_count"]

        if running_count > 0 and not args.force:
            conn.rollback()
            print("Task has running examples. Use --force to delete it anyway.")
            return

        insert_event(
            cur,
            args.task_id,
            None,
            None,
            None,
            "task_deleted",
            "Task deleted by taskctl",
        )

        cur.execute("DELETE FROM tasks WHERE task_id = ?", (args.task_id,))
        conn.commit()

        print("Task deleted: %s" % args.task_id)

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()



SHARE_REPORT_DIR = Path(os.environ.get(
    "PJTEST_SHARE_REPORT_DIR",
    "/home/xshare/zw_cache/distributed_test_system/reports",
))


def resolve_task_id(conn, task_id=None):
    """Resolve omitted/latest task id to the newest task."""
    if task_id and task_id != "latest":
        return task_id

    cur = conn.cursor()
    cur.execute(
        """
        SELECT task_id
        FROM tasks
        ORDER BY id DESC
        LIMIT 1
        """
    )
    row = cur.fetchone()

    if not row:
        raise ValueError("No task found.")

    return row["task_id"]


def get_task_row(cur, task_id):
    """Fetch one task row."""
    cur.execute(
        """
        SELECT *
        FROM tasks
        WHERE task_id = ?
        """,
        (task_id,),
    )
    row = cur.fetchone()

    if not row:
        raise ValueError("Task not found: %s" % task_id)

    return row


def get_task_counts(cur, task_id):
    """Return aggregated example counts for one task."""
    cur.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
            SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) AS timeout_count,
            SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_count,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
            SUM(CASE WHEN status IN ('success', 'failed', 'timeout', 'canceled') THEN 1 ELSE 0 END) AS done_count
        FROM task_examples
        WHERE task_id = ?
        """,
        (task_id,),
    )

    row = cur.fetchone()
    result = {}

    for key in [
        "total",
        "success_count",
        "failed_count",
        "timeout_count",
        "running_count",
        "pending_count",
        "done_count",
    ]:
        result[key] = int(row[key] or 0)

    return result


def get_task_workers(cur, task_id):
    """Return workers that have worked on this task."""
    cur.execute(
        """
        SELECT DISTINCT assigned_worker
        FROM task_examples
        WHERE task_id = ?
          AND assigned_worker IS NOT NULL
          AND assigned_worker != ''
        ORDER BY assigned_worker
        """,
        (task_id,),
    )

    workers = [row["assigned_worker"] for row in cur.fetchall()]
    return ", ".join(workers) if workers else "-"


def progress_text(done, total):
    """Format progress text."""
    if total <= 0:
        return "-"

    return "%d/%d %.1f%%" % (done, total, done * 100.0 / total)


def print_task_stat(task, counts, workers):
    """Print one task statistic block."""
    total = counts["total"]
    done = counts["done_count"]

    print("Task ID      : %s" % task["task_id"])
    print("Template     : %s" % task["template_name"])
    print("Task name    : %s" % task["task_name"])
    print("Revision     : %s" % format_revision(task))
    print("Status       : %s" % task["status"])
    print("Target dir   : %s" % task["target_dir"])
    print("Total        : %d" % total)
    print("Done         : %d" % done)
    print("Success      : %d" % counts["success_count"])
    print("Failed       : %d" % counts["failed_count"])
    print("Timeout      : %d" % counts["timeout_count"])
    print("Running      : %d" % counts["running_count"])
    print("Pending      : %d" % counts["pending_count"])
    print("Progress     : %s" % progress_text(done, total))
    print("Workers      : %s" % workers)
    print("Created at   : %s" % task["created_at"])
    if "updated_at" in task.keys():
        print("Updated at   : %s" % task["updated_at"])
    if task["message"]:
        print("Message      : %s" % task["message"])


def cmd_stat(args):
    """Print aggregate statistics for one task."""
    conn = get_conn()
    cur = conn.cursor()

    try:
        task_id = resolve_task_id(conn, getattr(args, "task_id", None))
        task = get_task_row(cur, task_id)
        counts = get_task_counts(cur, task_id)
        workers = get_task_workers(cur, task_id)
    finally:
        conn.close()

    print_task_stat(task, counts, workers)

def json_loads_dict(value):
    """Load a JSON object safely."""
    if not value:
        return {}

    try:
        data = json.loads(value)
    except Exception:
        return {}

    return data if isinstance(data, dict) else {}


def print_key_value(title, rows):
    """Print aligned key/value rows."""
    print(title)
    print("=" * 100)

    for key, value in rows:
        if value is None or value == "":
            value = "-"
        print("%-18s: %s" % (key, value))

    print("")


def fetch_task_recent_examples(cur, task_id, statuses, limit):
    """Fetch recent examples by status list."""
    placeholders = ",".join("?" for _ in statuses)
    params = [task_id] + list(statuses) + [limit]

    cur.execute(
        """
        SELECT example_id, seq, target_arg, status, assigned_worker,
               current_attempt_id, retry_count, max_retry, exit_code,
               failed_step, failed_reason, started_at, finished_at,
               log_file, run_log_dir, report_dir, message
        FROM task_examples
        WHERE task_id = ?
          AND status IN (%s)
        ORDER BY updated_at DESC, seq ASC
        LIMIT ?
        """ % placeholders,
        params,
    )

    return cur.fetchall()


def fetch_task_recent_attempts(cur, task_id, limit):
    """Fetch recent attempts for one task."""
    cur.execute(
        """
        SELECT attempt_id, example_id, attempt_no, worker_name, status,
               revision, started_at, finished_at, exit_code, timed_out,
               log_file, run_log_dir, report_dir, message
        FROM task_attempts
        WHERE task_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (task_id, limit),
    )

    return cur.fetchall()


def fetch_task_active_workers(cur, task_id):
    """Fetch currently active workers for one task."""
    cur.execute(
        """
        SELECT worker_name, hostname, status, current_example_id,
               current_attempt_id, last_seen_at, message
        FROM workers
        WHERE current_task_id = ?
        ORDER BY worker_name
        """,
        (task_id,),
    )

    return cur.fetchall()


def print_flow_config(flow_config):
    """Print flow_config in aligned form."""
    print("Flow Config")
    print("=" * 100)

    if not flow_config:
        print("-")
        print("")
        return

    for key in sorted(flow_config.keys()):
        print("%-28s: %s" % (key, flow_config[key]))

    print("")


def print_show_examples(title, rows):
    """Print compact example rows."""
    print(title)
    print("=" * 100)

    if not rows:
        print("-")
        print("")
        return

    header = "%-18s %5s %-10s %-18s %-7s %-6s %s" % (
        "EXAMPLE_ID",
        "SEQ",
        "STATUS",
        "WORKER",
        "RETRY",
        "EXIT",
        "TARGET",
    )
    print(header)
    print("-" * len(header))

    for row in rows:
        retry = "%s/%s" % (
            row["retry_count"] if row["retry_count"] is not None else 0,
            row["max_retry"] if row["max_retry"] is not None else 0,
        )
        exit_code = "-" if row["exit_code"] is None else str(row["exit_code"])
        print("%-18s %5s %-10s %-18s %-7s %-6s %s" % (
            row["example_id"],
            row["seq"],
            row["status"],
            row["assigned_worker"] or "-",
            retry,
            exit_code,
            row["target_arg"] or "-",
        ))

        if row["failed_step"] or row["failed_reason"]:
            print("    failed_step   : %s" % (row["failed_step"] or "-"))
            print("    failed_reason : %s" % (row["failed_reason"] or "-"))
        if row["message"]:
            print("    message       : %s" % row["message"])
        if row["log_file"]:
            print("    log_file      : %s" % row["log_file"])
        if row["run_log_dir"]:
            print("    run_log_dir   : %s" % row["run_log_dir"])
        if row["report_dir"]:
            print("    report_dir    : %s" % row["report_dir"])

    print("")


def print_show_attempts(rows):
    """Print recent attempts."""
    print("Recent Attempts")
    print("=" * 100)

    if not rows:
        print("-")
        print("")
        return

    header = "%-18s %-18s %4s %-18s %-10s %-8s %-6s %-19s %-19s" % (
        "ATTEMPT_ID",
        "EXAMPLE_ID",
        "NO",
        "WORKER",
        "STATUS",
        "REV",
        "EXIT",
        "STARTED_AT",
        "FINISHED_AT",
    )
    print(header)
    print("-" * len(header))

    for row in rows:
        exit_code = "-" if row["exit_code"] is None else str(row["exit_code"])
        print("%-18s %-18s %4s %-18s %-10s %-8s %-6s %-19s %-19s" % (
            row["attempt_id"],
            row["example_id"],
            row["attempt_no"],
            row["worker_name"] or "-",
            row["status"] or "-",
            row["revision"] or "-",
            exit_code,
            row["started_at"] or "-",
            row["finished_at"] or "-",
        ))

        if row["message"]:
            print("    message     : %s" % row["message"])
        if row["log_file"]:
            print("    log_file    : %s" % row["log_file"])
        if row["run_log_dir"]:
            print("    run_log_dir : %s" % row["run_log_dir"])
        if row["report_dir"]:
            print("    report_dir  : %s" % row["report_dir"])

    print("")


def print_show_workers(rows):
    """Print active workers for this task."""
    print("Active Workers")
    print("=" * 100)

    if not rows:
        print("-")
        print("")
        return

    header = "%-18s %-18s %-10s %-18s %-18s %-19s %s" % (
        "WORKER",
        "HOSTNAME",
        "STATUS",
        "EXAMPLE_ID",
        "ATTEMPT_ID",
        "LAST_SEEN_AT",
        "MESSAGE",
    )
    print(header)
    print("-" * len(header))

    for row in rows:
        print("%-18s %-18s %-10s %-18s %-18s %-19s %s" % (
            row["worker_name"] or "-",
            row["hostname"] or "-",
            row["status"] or "-",
            row["current_example_id"] or "-",
            row["current_attempt_id"] or "-",
            row["last_seen_at"] or "-",
            row["message"] or "-",
        ))

    print("")


def cmd_show(args):
    """Show full task configuration and useful runtime details."""
    conn = get_conn()
    cur = conn.cursor()

    try:
        task_id = resolve_task_id(conn, getattr(args, "task_id", None))
        task = get_task_row(cur, task_id)
        counts = get_task_counts(cur, task_id)
        workers = get_task_workers(cur, task_id)
        active_workers = fetch_task_active_workers(cur, task_id)
        failed_rows = fetch_task_recent_examples(
            cur,
            task_id,
            ["failed", "timeout"],
            args.limit,
        )
        running_rows = fetch_task_recent_examples(
            cur,
            task_id,
            ["running"],
            args.limit,
        )
        recent_attempts = fetch_task_recent_attempts(cur, task_id, args.limit)
    finally:
        conn.close()

    total = counts["total"]
    done = counts["done_count"]
    flow_config = json_loads_dict(task["flow_config_json"])
    report_root = SHARE_REPORT_DIR
    report_dir = report_root / report_revision_dir(task) / task_id

    print_key_value(
        "Task Detail",
        [
            ("Task ID", task["task_id"]),
            ("Task Name", task["task_name"]),
            ("Template", task["template_name"]),
            ("Suite", task["suite"]),
            ("Status", task["status"]),
            ("Revision", format_revision(task)),
            ("Revision Policy", task["revision_policy"]),
            ("Resolved Zip", task["resolved_zip_path"]),
            ("Target Worker", task["target_worker"]),
            ("Priority", task["priority"]),
            ("Max Retry", task["max_retry"]),
            ("Max Time", task["max_time"]),
            ("Work Root", task["work_root"]),
            ("Target Dir", task["target_dir"]),
            ("Split Mode", task["split_mode"]),
            ("Total Examples", task["total_examples"]),
            ("Repeat Enabled", task["repeat_enabled"]),
            ("Repeat Group", task["repeat_group"]),
            ("Repeat Index", task["repeat_index"]),
            ("Parent Task ID", task["parent_task_id"]),
            ("Created At", task["created_at"]),
            ("Updated At", task["updated_at"]),
            ("Started At", task["started_at"]),
            ("Finished At", task["finished_at"]),
            ("Message", task["message"]),
            ("Report Dir", report_dir),
        ],
    )

    print_key_value(
        "Summary",
        [
            ("Total", total),
            ("Done", done),
            ("Success", counts["success_count"]),
            ("Failed", counts["failed_count"]),
            ("Timeout", counts["timeout_count"]),
            ("Running", counts["running_count"]),
            ("Pending", counts["pending_count"]),
            ("Progress", progress_text(done, total)),
            ("Workers", workers),
        ],
    )

    print_flow_config(flow_config)

    if args.raw_json:
        print("Raw flow_config_json")
        print("=" * 100)
        print(task["flow_config_json"] or "{}")
        print("")

    print_show_workers(active_workers)

    if not args.no_examples:
        print_show_examples("Running Examples", running_rows)
        print_show_examples("Recent Failures / Timeouts", failed_rows)

    if args.attempts:
        print_show_attempts(recent_attempts)

def status_label_to_db_status(command):
    """Convert shortcut command to database status."""
    mapping = {
        "pass": "success",
        "fail": "failed",
        "timeout": "timeout",
        "running": "running",
        "pending": "pending",
    }
    return mapping[command]


def fetch_examples_by_status(cur, task_id, status):
    """Fetch examples by status."""
    cur.execute(
        """
        SELECT example_id, seq, target_arg, run_tcl_path, status,
               assigned_worker, current_attempt_id, retry_count, max_retry,
               exit_code, failed_step, failed_reason, started_at, finished_at,
               log_file, run_log_dir, report_dir, message
        FROM task_examples
        WHERE task_id = ?
          AND status = ?
        ORDER BY seq
        """,
        (task_id, status),
    )
    return cur.fetchall()


def print_examples_rows(rows, verbose=False, names_only=False):
    """Print example rows in table or path-only format."""
    if names_only:
        for row in rows:
            print(row["target_arg"])
        return

    if not rows:
        print("No examples found.")
        return

    header = "%-18s %4s %-10s %-10s %-7s %-6s %-45s" % (
        "EXAMPLE_ID",
        "SEQ",
        "STATUS",
        "WORKER",
        "RETRY",
        "EXIT",
        "TARGET_ARG",
    )
    print(header)
    print("-" * len(header))

    for row in rows:
        worker = row["assigned_worker"] or "-"
        retry = "%d/%d" % (row["retry_count"], row["max_retry"])
        exit_code = "-" if row["exit_code"] is None else str(row["exit_code"])

        print(
            "%-18s %4d %-10s %-10s %-7s %-6s %-45s"
            % (
                format_short(row["example_id"], 18),
                row["seq"],
                format_short(row["status"], 10),
                format_short(worker, 10),
                retry,
                exit_code,
                format_short(row["target_arg"], 45),
            )
        )

        if verbose:
            print("    run_tcl_path       : %s" % row["run_tcl_path"])
            if row["current_attempt_id"]:
                print("    current_attempt_id : %s" % row["current_attempt_id"])
            if row["failed_step"] or row["failed_reason"]:
                print("    failed_step        : %s" % (row["failed_step"] or "-"))
                print("    failed_reason      : %s" % (row["failed_reason"] or "-"))
            if row["message"]:
                print("    message            : %s" % row["message"])
            if row["log_file"]:
                print("    log_file           : %s" % row["log_file"])
            if row["run_log_dir"]:
                print("    run_log_dir        : %s" % row["run_log_dir"])
            if row["report_dir"]:
                print("    report_dir         : %s" % row["report_dir"])


def cmd_status_shortcut(args):
    """Handle pass/fail/timeout/running/pending shortcut commands."""
    status = status_label_to_db_status(args.command)

    conn = get_conn()
    cur = conn.cursor()

    try:
        task_id = resolve_task_id(conn, getattr(args, "task_id", None))
        task = get_task_row(cur, task_id)
        rows = fetch_examples_by_status(cur, task_id, status)
    finally:
        conn.close()

    if not args.names_only:
        print("Task ID      : %s" % task["task_id"])
        print("Template     : %s" % task["template_name"])
        print("Revision     : %s" % format_revision(task))
        print("Filter       : %s" % status)
        print("Count        : %d" % len(rows))
        print("")

    print_examples_rows(
        rows,
        verbose=args.verbose,
        names_only=args.names_only,
    )


def clean_report_text(value):
    """Sanitize report fields for one-line text files."""
    if value is None:
        return "-"

    text = str(value)
    text = text.replace("\t", " ")
    text = text.replace("\r", " ")
    text = text.replace("\n", " ")
    return text


def write_text_file(path, content):
    """Write text using atomic replace and a unique temporary file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex[:12]))

    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write(content)

        os.replace(str(tmp_path), str(path))
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def fetch_all_examples(cur, task_id):
    """Fetch all examples for report generation."""
    cur.execute(
        """
        SELECT example_id, seq, target_arg, run_tcl_path, status,
               assigned_worker, retry_count, max_retry, exit_code,
               started_at, finished_at, log_file, run_log_dir,
               report_dir, message
        FROM task_examples
        WHERE task_id = ?
        ORDER BY seq
        """,
        (task_id,),
    )
    return cur.fetchall()


def build_stat_summary(task, counts, workers):
    """Build stat_summary file content."""
    total = counts["total"]
    done = counts["done_count"]

    lines = [
        "task_id          %s" % task["task_id"],
        "template         %s" % task["template_name"],
        "task_name        %s" % task["task_name"],
        "revision         %s" % format_revision(task),
        "status           %s" % task["status"],
        "target_dir       %s" % task["target_dir"],
        "total            %d" % total,
        "done             %d" % done,
        "success          %d" % counts["success_count"],
        "failed           %d" % counts["failed_count"],
        "timeout          %d" % counts["timeout_count"],
        "running          %d" % counts["running_count"],
        "pending          %d" % counts["pending_count"],
        "progress         %s" % progress_text(done, total),
        "workers          %s" % workers,
        "created_at       %s" % task["created_at"],
    ]

    if "updated_at" in task.keys():
        lines.append("updated_at       %s" % task["updated_at"])

    lines.append("")
    return "\n".join(lines)


def build_status_summary(rows):
    """Build status_summary file content."""
    lines = []
    lines.append(
        "SEQ\tEXAMPLE_ID\tSTATUS\tWORKER\tRETRY\tEXIT\tTARGET_ARG\tLOG_FILE\tMESSAGE"
    )

    for row in rows:
        retry = "%d/%d" % (row["retry_count"], row["max_retry"])
        exit_code = "-" if row["exit_code"] is None else str(row["exit_code"])
        worker = row["assigned_worker"] or "-"

        lines.append(
            "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s"
            % (
                row["seq"],
                clean_report_text(row["example_id"]),
                clean_report_text(row["status"]),
                clean_report_text(worker),
                clean_report_text(retry),
                clean_report_text(exit_code),
                clean_report_text(row["target_arg"]),
                clean_report_text(row["log_file"]),
                clean_report_text(row["message"]),
            )
        )

    lines.append("")
    return "\n".join(lines)


def build_list_file(rows, status):
    """Build a path list for one status."""
    selected = [row for row in rows if row["status"] == status]
    lines = [row["target_arg"] for row in selected]
    return "\n".join(lines) + ("\n" if lines else "")


def sanitize_report_component(value, default_value="unknown"):
    """Return a filesystem-safe report path component."""
    text = str(value or "").strip()
    if not text:
        text = default_value

    text = text.replace("latest->", "latest_")
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    text = text.strip("._-")
    return text or default_value


def report_revision_dir(task):
    """Return revision directory name used below report root."""
    revision = task["revision"]
    revision_text = format_revision(task)

    if revision and re.match(r"^\d+$", str(revision)):
        return "r%s" % revision

    return sanitize_report_component(revision_text, "unknown_revision")


def write_task_report(task_id, out_dir=None):
    """Generate final report files from database for one task.

    Reports are written only under:
        <report_root>/<revision>/<task_id>/

    Nothing is written directly into /home/xshare/zw_cache/distributed_test_system.
    """
    conn = get_conn()
    cur = conn.cursor()

    try:
        task_id = resolve_task_id(conn, task_id)
        task = get_task_row(cur, task_id)
        counts = get_task_counts(cur, task_id)
        workers = get_task_workers(cur, task_id)
        rows = fetch_all_examples(cur, task_id)
    finally:
        conn.close()

    report_root = Path(out_dir) if out_dir else SHARE_REPORT_DIR
    revision_dir = report_revision_dir(task)
    task_dir = report_root / revision_dir / task_id

    files = {
        "stat_summary": build_stat_summary(task, counts, workers),
        "status_summary": build_status_summary(rows),
        "list_pass_to_run": build_list_file(rows, "success"),
        "list_fail_to_run": build_list_file(rows, "failed"),
        "timeout_list": build_list_file(rows, "timeout"),
    }

    task_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in files.items():
        write_text_file(task_dir / filename, content)

    return report_root, task_dir, files


def cmd_report(args):
    """Generate report files from database."""
    report_root, task_dir, files = write_task_report(args.task_id, args.out)

    print("Report generated.")
    print("Report root   : %s" % report_root)
    print("Task dir      : %s" % task_dir)
    print("Files         :")
    for filename in sorted(files.keys()):
        print("  %s" % filename)

def cmd_apply(args):
    """Batch create tasks from a YAML suite file."""
    yaml_path = Path(args.file).expanduser().resolve()
    if not yaml_path.is_file():
        print("ERROR: YAML file not found: %s" % yaml_path)
        sys.exit(1)

    with yaml_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # 支持两种格式：
    # 1. 直接是一个列表：[ {template: place, target: .}, ... ]
    # 2. 包含 tasks 字段的字典：{ tasks: [ ... ] }
    if isinstance(data, dict) and "tasks" in data:
        task_list = data["tasks"]
    elif isinstance(data, list):
        task_list = data
    else:
        print("ERROR: YAML must contain a list or a dict with 'tasks' key.")
        sys.exit(1)

    total = len(task_list)
    success = 0
    failed = 0

    print("Applying %d tasks from %s ..." % (total, yaml_path))
    print("-" * 60)

    for idx, entry in enumerate(task_list, 1):
        template = entry.get("template")
        target = entry.get("target", ".")

        if not template:
            print("[%d/%d] SKIP: missing 'template' field" % (idx, total))
            failed += 1
            continue

        # 构造模拟的 argparse.Namespace 对象，模拟 ./taskctl.py add 的参数
        class Args:
            pass
        task_args = Args()
        task_args.template_name = template
        task_args.target_dir = target
        task_args.revision = entry.get("revision")  # None 表示 latest
        task_args.name = entry.get("name")
        task_args.priority = entry.get("priority")
        task_args.max_retry = entry.get("max_retry")
        task_args.max_time = entry.get("max_time")

        try:
            # 复用 cmd_add 的核心逻辑
            cmd_add(task_args)
            success += 1
            print("[%d/%d] OK: %s %s" % (idx, total, template, target))
        except Exception as exc:
            failed += 1
            print("[%d/%d] FAIL: %s %s - %s" % (idx, total, template, target, exc))

    print("-" * 60)
    print("Summary: SUCCESS=%d FAILED=%d" % (success, failed))
    if failed > 0:
        sys.exit(1)


# ============================================================
# Environment checks / worker status / task cancel
# ============================================================

DEFAULT_SCHEDULER_URL = (
    os.environ.get("PJTEST_SCHEDULER_URL")
    or os.environ.get("SCHEDULER_URL")
    or "http://192.168.10.11:8888"
).rstrip("/")

DEFAULT_ZIP_DIRS = [
    Path("/home/xshare/zhouwei_runcache/GalaxCore/zip"),
]

ZIP_RE = re.compile(r"^Galax[Cc]ore_(\d+)\.zip$")


def get_zip_dirs():
    """Return GalaxCore zip directories from environment or default path."""
    override = os.environ.get("GALAXCORE_ZIP_DIR")

    if override:
        return [Path(item).expanduser() for item in override.split(os.pathsep) if item]

    return list(DEFAULT_ZIP_DIRS)


def find_latest_zip_for_check():
    """Find the latest GalaxCore zip for environment checking."""
    best = None

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
            if best is None or revision > best[0]:
                best = (revision, item)

    return best


def print_check_item(name, ok, detail):
    """Print one check result line."""
    status = "OK" if ok else "FAIL"
    print("%-24s %-6s %s" % (name, status, detail))


def table_exists(cur, table_name):
    """Return True if a table exists in the SQLite database."""
    cur.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    )
    return cur.fetchone() is not None


def can_write_directory(path):
    """Return whether a directory is writable by creating a temporary file."""
    path = Path(path)

    if not path.is_dir():
        return False, "directory not found: %s" % path

    test_file = path / ".pjtest_write_check.tmp"

    try:
        with test_file.open("w", encoding="utf-8") as f:
            f.write(local_now() + "\n")
        test_file.unlink()
        return True, "writable: %s" % path
    except Exception as exc:
        try:
            if test_file.exists():
                test_file.unlink()
        except Exception:
            pass
        return False, "not writable: %s (%s)" % (path, exc)


def check_scheduler_health(url):
    """Check scheduler health API."""
    health_url = url.rstrip("/") + "/api/health"

    try:
        with urlopen(health_url, timeout=3) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw.strip() else {}
        if data.get("ok"):
            return True, "%s service=%s time=%s" % (
                health_url,
                data.get("service", "-"),
                data.get("time", "-"),
            )
        return False, "%s unexpected response: %s" % (health_url, raw.strip())
    except HTTPError as exc:
        return False, "%s HTTP %s" % (health_url, exc.code)
    except URLError as exc:
        return False, "%s unavailable: %s" % (health_url, exc)
    except Exception as exc:
        return False, "%s error: %s" % (health_url, exc)


def cmd_check(args):
    """Check central PJTest environment."""
    print("PJTest environment check")
    print("=" * 80)

    ok_all = True

    db_exists = DB_PATH.is_file()
    print_check_item("database file", db_exists, str(DB_PATH))
    ok_all = ok_all and db_exists

    required_tables = [
        "tasks",
        "task_examples",
        "task_attempts",
        "workers",
        "task_events",
    ]

    if db_exists:
        try:
            conn = get_conn()
            cur = conn.cursor()

            missing_tables = []
            for table_name in required_tables:
                if not table_exists(cur, table_name):
                    missing_tables.append(table_name)

            if missing_tables:
                print_check_item(
                    "database tables",
                    False,
                    "missing: %s" % ", ".join(missing_tables),
                )
                ok_all = False
            else:
                print_check_item(
                    "database tables",
                    True,
                    "all required tables exist",
                )

            cur.execute("SELECT COUNT(*) AS count FROM tasks")
            task_count = cur.fetchone()["count"]
            cur.execute("SELECT COUNT(*) AS count FROM task_examples")
            example_count = cur.fetchone()["count"]
            cur.execute("SELECT COUNT(*) AS count FROM workers")
            worker_count = cur.fetchone()["count"]

            print_check_item(
                "database counts",
                True,
                "tasks=%s examples=%s workers=%s" % (
                    task_count,
                    example_count,
                    worker_count,
                ),
            )

            conn.close()
        except Exception as exc:
            print_check_item("database open", False, str(exc))
            ok_all = False

    template_ok = TEMPLATE_DIR.is_dir()
    print_check_item("template dir", template_ok, str(TEMPLATE_DIR))
    ok_all = ok_all and template_ok

    zip_dirs = get_zip_dirs()
    existing_zip_dirs = [path for path in zip_dirs if path.is_dir()]
    print_check_item(
        "zip dirs",
        bool(existing_zip_dirs),
        ", ".join(str(path) for path in zip_dirs),
    )
    ok_all = ok_all and bool(existing_zip_dirs)

    latest_zip = find_latest_zip_for_check()
    if latest_zip:
        print_check_item(
            "latest zip",
            True,
            "revision=%s path=%s" % (latest_zip[0], latest_zip[1]),
        )
    else:
        print_check_item("latest zip", False, "no GalaxCore_xxxxx.zip found")
        ok_all = False

    share_ok, share_detail = can_write_directory(SHARE_REPORT_DIR)
    print_check_item("share report dir", share_ok, share_detail)
    ok_all = ok_all and share_ok

    scheduler_url = args.scheduler or DEFAULT_SCHEDULER_URL
    scheduler_ok, scheduler_detail = check_scheduler_health(scheduler_url)
    print_check_item("scheduler health", scheduler_ok, scheduler_detail)
    ok_all = ok_all and scheduler_ok

    print("=" * 80)
    print("RESULT: %s" % ("OK" if ok_all else "FAILED"))

    if not ok_all:
        sys.exit(1)


def cmd_workers(args):
    """List worker status from the database."""
    conn = get_conn()
    cur = conn.cursor()

    query = """
        SELECT worker_name, hostname, status, current_task_id,
               current_example_id, current_attempt_id, last_seen_at,
               started_at, updated_at, message
        FROM workers
    """
    params = []

    if args.status:
        query += " WHERE status = ?"
        params.append(args.status)

    query += " ORDER BY worker_name ASC"

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("No workers found.")
        return

    header = "%-12s %-16s %-10s %-18s %-18s %-18s %-19s %s" % (
        "WORKER",
        "HOSTNAME",
        "STATUS",
        "TASK_ID",
        "EXAMPLE_ID",
        "ATTEMPT_ID",
        "LAST_SEEN_AT",
        "MESSAGE",
    )
    print(header)
    print("-" * len(header))

    for row in rows:
        print(
            "%-12s %-16s %-10s %-18s %-18s %-18s %-19s %s"
            % (
                row["worker_name"] or "-",
                row["hostname"] or "-",
                row["status"] or "-",
                row["current_task_id"] or "-",
                row["current_example_id"] or "-",
                row["current_attempt_id"] or "-",
                row["last_seen_at"] or "-",
                row["message"] or "-",
            )
        )


def get_task_cancel_counts(cur, task_id):
    """Return counts used by cancel command."""
    cur.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
            SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_count,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
            SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) AS timeout_count,
            SUM(CASE WHEN status = 'canceled' THEN 1 ELSE 0 END) AS canceled_count
        FROM task_examples
        WHERE task_id = ?
        """,
        (task_id,),
    )
    row = cur.fetchone()

    result = {}
    for key in [
        "total",
        "pending_count",
        "running_count",
        "success_count",
        "failed_count",
        "timeout_count",
        "canceled_count",
    ]:
        result[key] = int(row[key] or 0)

    return result


def fetch_stale_examples(cur, task_id=None):
    """Return running examples that are not matched by the current worker table."""
    sql = """
        SELECT
            e.example_id,
            e.task_id,
            e.seq,
            e.target_arg,
            e.status,
            e.assigned_worker,
            e.current_attempt_id,
            e.retry_count,
            e.max_retry,
            e.started_at,
            e.updated_at,
            e.message,
            w.status AS worker_status,
            w.current_task_id AS worker_task_id,
            w.current_example_id AS worker_example_id,
            w.current_attempt_id AS worker_attempt_id,
            w.last_seen_at AS worker_last_seen_at,
            w.message AS worker_message
        FROM task_examples e
        LEFT JOIN workers w
            ON w.worker_name = e.assigned_worker
        WHERE e.status = 'running'
          AND NOT (
              w.status IN ('running', 'reporting')
              AND w.current_task_id = e.task_id
              AND w.current_example_id = e.example_id
              AND w.current_attempt_id = e.current_attempt_id
          )
    """
    params = []
    if task_id:
        sql += " AND e.task_id = ?"
        params.append(task_id)
    sql += " ORDER BY e.task_id, e.seq"
    cur.execute(sql, params)
    return cur.fetchall()


def print_stale_rows(rows, verbose=False):
    """Print stale running rows."""
    if not rows:
        print("No stale running examples found.")
        return

    header = "%-18s %5s %-18s %-18s %-10s %-19s %s" % (
        "EXAMPLE_ID",
        "SEQ",
        "TASK_ID",
        "WORKER",
        "W_STATUS",
        "W_LAST_SEEN",
        "TARGET",
    )
    print(header)
    print("-" * len(header))

    for row in rows:
        print("%-18s %5s %-18s %-18s %-10s %-19s %s" % (
            row["example_id"],
            row["seq"],
            row["task_id"],
            row["assigned_worker"] or "-",
            row["worker_status"] or "-",
            row["worker_last_seen_at"] or "-",
            row["target_arg"] or "-",
        ))
        if verbose:
            print("    current_attempt_id : %s" % (row["current_attempt_id"] or "-"))
            print("    worker_task_id     : %s" % (row["worker_task_id"] or "-"))
            print("    worker_example_id  : %s" % (row["worker_example_id"] or "-"))
            print("    worker_attempt_id  : %s" % (row["worker_attempt_id"] or "-"))
            if row["message"]:
                print("    example_message    : %s" % row["message"])
            if row["worker_message"]:
                print("    worker_message     : %s" % row["worker_message"])


def cmd_stale(args):
    """List stale running examples."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        task_id = None
        if getattr(args, "task_id", None):
            task_id = resolve_task_id(conn, args.task_id)
        rows = fetch_stale_examples(cur, task_id)
    finally:
        conn.close()

    print_stale_rows(rows, verbose=args.verbose)


def count_one(cur, sql, params=None):
    """Return the first COUNT(*) result."""
    cur.execute(sql, params or [])
    row = cur.fetchone()
    return int(row[0] or 0)


def cmd_diagnose(args):
    """Diagnose common scheduler/database consistency problems."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        task_id = None
        if getattr(args, "task_id", None):
            task_id = resolve_task_id(conn, args.task_id)

        task_filter = ""
        params = []
        if task_id:
            task_filter = " AND task_id = ?"
            params = [task_id]

        active_workers = count_one(
            cur,
            """
            SELECT COUNT(*)
            FROM workers
            WHERE status IN ('running', 'reporting')
              AND current_example_id IS NOT NULL
            """,
        )
        db_running = count_one(
            cur,
            "SELECT COUNT(*) FROM task_examples WHERE status = 'running'" + task_filter,
            params,
        )
        stale_rows = fetch_stale_examples(cur, task_id)

        orphan_params = []
        orphan_filter = ""
        if task_id:
            orphan_filter = " AND a.task_id = ?"
            orphan_params.append(task_id)
        orphan_attempts = count_one(
            cur,
            """
            SELECT COUNT(*)
            FROM task_attempts a
            LEFT JOIN task_examples e
                ON e.current_attempt_id = a.attempt_id
            WHERE a.status = 'running'
              AND (e.example_id IS NULL OR e.status != 'running')
            """ + orphan_filter,
            orphan_params,
        )

        cur.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM workers
            GROUP BY status
            ORDER BY status
            """
        )
        worker_status_rows = cur.fetchall()

    finally:
        conn.close()

    print("PJTest diagnose")
    print("=" * 80)
    print("Task filter          : %s" % (task_id or "all"))
    print("Active workers       : %d" % active_workers)
    print("DB running examples  : %d" % db_running)
    print("Stale running        : %d" % len(stale_rows))
    print("Orphan attempts      : %d" % orphan_attempts)
    print("Worker statuses      : %s" % ", ".join(
        "%s=%s" % (row["status"] or "-", row["count"]) for row in worker_status_rows
    ))
    print("=" * 80)

    if stale_rows:
        print("Stale examples:")
        print_stale_rows(stale_rows[:50], verbose=False)
        if len(stale_rows) > 50:
            print("... %d more" % (len(stale_rows) - 50))

    if len(stale_rows) or orphan_attempts:
        sys.exit(2)


def default_exit_code_for_status(status):
    """Return a conservative default exit code for manual repair."""
    if status == "success":
        return 0
    if status == "timeout":
        return 124
    if status == "canceled":
        return None
    return 1


def cmd_repair_example(args):
    """Manually repair one example and its current attempt."""
    status = args.status
    exit_code = args.exit_code
    if exit_code is None:
        exit_code = default_exit_code_for_status(status)

    conn = get_conn()
    cur = conn.cursor()
    now = local_now()

    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT * FROM task_examples WHERE example_id = ?", (args.example_id,))
        example = cur.fetchone()
        if not example:
            conn.rollback()
            print("Example not found: %s" % args.example_id)
            return

        if example["status"] in TERMINAL_EXAMPLE_STATUSES and not args.force:
            conn.rollback()
            print("Example is already terminal: %s status=%s" % (args.example_id, example["status"]))
            print("Use --force to overwrite it.")
            return

        timed_out = 1 if status == "timeout" else 0
        failed_step = None
        failed_reason = None
        if status == "timeout":
            failed_step = "timeout"
            failed_reason = "manual repair timeout"
        elif status == "failed":
            failed_step = "manual_repair"
            failed_reason = "manual repair failed exit_code_%s" % exit_code
        elif status == "canceled":
            failed_step = None
            failed_reason = "manual repair canceled"

        message = args.message or "manual repair by taskctl: status=%s exit_code=%s" % (
            status,
            exit_code if exit_code is not None else "-",
        )

        attempt_id = example["current_attempt_id"]
        if attempt_id:
            cur.execute(
                """
                UPDATE task_attempts
                SET status = ?,
                    exit_code = ?,
                    timed_out = ?,
                    finished_at = ?,
                    message = ?
                WHERE attempt_id = ?
                """,
                (status, exit_code, timed_out, now, message, attempt_id),
            )

        cur.execute(
            """
            UPDATE task_examples
            SET status = ?,
                exit_code = ?,
                updated_at = ?,
                finished_at = ?,
                failed_step = ?,
                failed_reason = ?,
                message = ?
            WHERE example_id = ?
            """,
            (
                status,
                exit_code,
                now,
                now,
                failed_step,
                failed_reason,
                message,
                args.example_id,
            ),
        )

        insert_event(
            cur,
            example["task_id"],
            args.example_id,
            attempt_id,
            example["assigned_worker"],
            "example_manual_repair",
            message,
        )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print("Example repaired: %s status=%s exit_code=%s" % (
        args.example_id,
        status,
        exit_code if exit_code is not None else "-",
    ))


def cmd_cancel(args):
    """Cancel a task safely.

    Default mode cancels only pending examples and moves the task to canceling
    if there are still running examples.  Running examples are allowed to
    finish and report naturally.  --force cancels both pending and running DB
    rows immediately.
    """
    conn = get_conn()
    cur = conn.cursor()
    now = local_now()

    try:
        cur.execute("BEGIN IMMEDIATE")

        task_id = resolve_task_id(conn, args.task_id)
        task = get_task_row(cur, task_id)
        counts = get_task_cancel_counts(cur, task_id)
        running_count = counts["running_count"]
        pending_count = counts["pending_count"]

        if task["status"] in TERMINAL_TASK_STATUSES and not args.force:
            conn.rollback()
            print("Task is already terminal: %s status=%s" % (task_id, task["status"]))
            print("Use --force only if you really want to mark it canceled.")
            return

        cancel_statuses = ["pending"]
        if args.force:
            cancel_statuses.append("running")

        placeholders = ",".join("?" for _ in cancel_statuses)
        params = [now, now, "canceled by taskctl", task_id] + cancel_statuses

        cur.execute(
            """
            UPDATE task_examples
            SET status = 'canceled',
                updated_at = ?,
                finished_at = ?,
                message = ?,
                failed_step = NULL,
                failed_reason = NULL,
                exit_code = NULL
            WHERE task_id = ?
              AND status IN (%s)
            """ % placeholders,
            params,
        )
        canceled_examples = cur.rowcount

        if args.force:
            cur.execute(
                """
                UPDATE task_attempts
                SET status = 'canceled',
                    finished_at = ?,
                    message = ?
                WHERE task_id = ?
                  AND status = 'running'
                """,
                (
                    now,
                    "canceled by taskctl --force",
                    task_id,
                ),
            )
            canceled_attempts = cur.rowcount
            new_task_status = "canceled"
            finished_at = now
            task_message = "Task force-canceled by taskctl. canceled_examples=%d" % canceled_examples
        else:
            canceled_attempts = 0
            if running_count > 0:
                new_task_status = "canceling"
                finished_at = None
                task_message = "Task canceling by taskctl. pending_canceled=%d waiting_running=%d" % (
                    canceled_examples,
                    running_count,
                )
            else:
                new_task_status = "canceled"
                finished_at = now
                task_message = "Task canceled by taskctl. canceled_examples=%d" % canceled_examples

        cur.execute(
            """
            UPDATE tasks
            SET status = ?,
                updated_at = ?,
                finished_at = ?,
                message = ?
            WHERE task_id = ?
            """,
            (
                new_task_status,
                now,
                finished_at,
                task_message,
                task_id,
            ),
        )

        insert_event(
            cur,
            task_id,
            None,
            None,
            None,
            "task_canceled" if new_task_status == "canceled" else "task_canceling",
            "%s canceled_examples=%d canceled_attempts=%d" % (
                task_message,
                canceled_examples,
                canceled_attempts,
            ),
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    print("Task status   : %s" % new_task_status)
    print("Task ID       : %s" % task_id)
    print("Pending before: %d" % pending_count)
    print("Running before: %d" % running_count)
    print("Examples marked canceled : %d" % canceled_examples)
    if args.force:
        print("Running attempts canceled: %d" % canceled_attempts)

def build_parser():
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description="PJTest task control tool")
    sub = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add", help="create a batch task")
    p_add.add_argument("template_name", help="template name, for example: place")
    p_add.add_argument(
        "target_dir",
        nargs="?",
        default=".",
        help="target directory under work_root, default: .",
    )
    p_add.add_argument(
        "--revision",
        help="fixed revision number; omit or use latest for scheduler-resolved latest zip",
    )
    p_add.add_argument("--name", help="task display name")
    p_add.add_argument("--priority", type=int, help="priority, default from template")
    p_add.add_argument("--max-retry", type=int, help="retry count per example")
    p_add.add_argument("--max-time", type=int, help="timeout seconds per example")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="list recent tasks")
    p_list.add_argument("--status", help="filter by task status")
    p_list.add_argument(
        "-n",
        "--limit",
        type=int,
        default=5,
        help="number of recent tasks to show, default: 5",
    )
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="show full task detail and configuration")
    p_show.add_argument("task_id", nargs="?", default=None, help="task id, default: latest")
    p_show.add_argument(
        "--limit",
        type=int,
        default=20,
        help="max recent examples/attempts to show, default: 20",
    )
    p_show.add_argument(
        "--attempts",
        action="store_true",
        help="also show recent attempts",
    )
    p_show.add_argument(
        "--raw-json",
        action="store_true",
        help="also print raw flow_config_json",
    )
    p_show.add_argument(
        "--no-examples",
        action="store_true",
        help="do not print running/failure example sections",
    )
    p_show.set_defaults(func=cmd_show)
    
    p_examples = sub.add_parser("examples", help="list examples of a task")
    p_examples.add_argument("task_id", help="task id")
    p_examples.add_argument("--status", help="filter examples by status")
    p_examples.add_argument("-v", "--verbose", action="store_true")
    p_examples.set_defaults(func=cmd_examples)

    p_attempts = sub.add_parser("attempts", help="list attempts of one example")
    p_attempts.add_argument("example_id", help="example id")
    p_attempts.add_argument("-v", "--verbose", action="store_true")
    p_attempts.set_defaults(func=cmd_attempts)

    p_delete = sub.add_parser("delete", help="delete a task")
    p_delete.add_argument("task_id", help="task id")
    p_delete.add_argument("--force", action="store_true", help="delete running task too")
    p_delete.set_defaults(func=cmd_delete)

    p_stat = sub.add_parser("stat", help="show task aggregate statistics")
    p_stat.add_argument("task_id", nargs="?", default=None, help="task id, default: latest")
    p_stat.set_defaults(func=cmd_stat)

    for command_name in ["pass", "fail", "timeout", "running", "pending"]:
        p_status = sub.add_parser(command_name, help="list %s examples" % command_name)
        p_status.add_argument(
            "task_id",
            nargs="?",
            default=None,
            help="task id, default: latest",
        )
        p_status.add_argument(
            "--names-only",
            action="store_true",
            help="print target_arg only",
        )
        p_status.add_argument("-v", "--verbose", action="store_true")
        p_status.set_defaults(func=cmd_status_shortcut)

    p_report = sub.add_parser("report", help="generate report files from database")
    p_report.add_argument("task_id", nargs="?", default=None, help="task id, default: latest")
    p_report.add_argument(
        "--out",
        help="output report root, default: /home/xshare/zw_cache/distributed_test_system/reports",
    )
    p_report.set_defaults(func=cmd_report)

    p_workers = sub.add_parser("workers", help="list worker status")
    p_workers.add_argument("--status", help="filter workers by status")
    p_workers.set_defaults(func=cmd_workers)


    p_stale = sub.add_parser("stale", help="list stale running examples")
    p_stale.add_argument("task_id", nargs="?", default=None, help="task id, default: all")
    p_stale.add_argument("-v", "--verbose", action="store_true")
    p_stale.set_defaults(func=cmd_stale)

    p_diagnose = sub.add_parser("diagnose", help="diagnose database/worker consistency")
    p_diagnose.add_argument("task_id", nargs="?", default=None, help="task id, default: all")
    p_diagnose.set_defaults(func=cmd_diagnose)

    p_repair = sub.add_parser("repair-example", help="manually repair one example status")
    p_repair.add_argument("example_id", help="example id")
    p_repair.add_argument("--status", required=True, choices=["success", "failed", "timeout", "canceled"])
    p_repair.add_argument("--exit-code", type=int, default=None)
    p_repair.add_argument("--message", default=None)
    p_repair.add_argument("--force", action="store_true")
    p_repair.set_defaults(func=cmd_repair_example)

    p_check = sub.add_parser("check", help="check PJTest environment")
    p_check.add_argument(
        "--scheduler",
        help="scheduler URL, default: env SCHEDULER_URL/PJTEST_SCHEDULER_URL or http://192.168.10.11:8888",
    )
    p_check.set_defaults(func=cmd_check)

    p_cancel = sub.add_parser("cancel", help="cancel a task safely")
    p_cancel.add_argument("task_id", nargs="?", default=None, help="task id, default: latest")
    p_cancel.add_argument(
        "--force",
        action="store_true",
        help="also mark running examples/attempts canceled in DB",
    )
    p_cancel.set_defaults(func=cmd_cancel)

    p_apply = sub.add_parser("apply", help="create tasks from a YAML suite file")
    p_apply.add_argument("file", help="YAML file containing task definitions")
    p_apply.set_defaults(func=cmd_apply)

    return parser

def main():
    """Program entry."""
    parser = build_parser()
    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("")
        print("Interrupted.")
        sys.exit(130)
    except Exception as exc:
        print("ERROR: %s" % exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
