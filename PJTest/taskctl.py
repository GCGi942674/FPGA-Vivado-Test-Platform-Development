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

import yaml
import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from config import (
    CONFIG_FILE,
    get_int,
    get_list,
    get_path,
    get_path_list,
    get_section,
    get_value,
)

try:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
except ImportError:  # pragma: no cover, Python 2 fallback is not expected.
    from urllib2 import Request, urlopen, HTTPError, URLError


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = get_path(
    "paths",
    "database",
    env_name="PJTEST_DB_PATH",
)
TEMPLATE_DIR = get_path(
    "paths",
    "template_dir",
    env_name="PJTEST_TEMPLATE_DIR",
)

DEFAULT_WORK_ROOT = get_value(
    "paths",
    "default_work_root",
)
DEFAULT_TARGET_DIR = get_value("task_defaults", "target_dir")
DEFAULT_SUITE = get_value("task_defaults", "suite")
DEFAULT_PRIORITY = get_int("task_defaults", "priority")
DEFAULT_MAX_RETRY = get_int("task_defaults", "max_retry", minimum=0)
DEFAULT_MAX_TIME = get_int("task_defaults", "max_time_sec", minimum=1)
DEFAULT_LIST_LIMIT = get_int("task_defaults", "list_limit", minimum=1)
DEFAULT_SHOW_LIMIT = get_int("task_defaults", "show_limit", minimum=1)

IGNORED_DIRS = set(get_list("scan", "ignored_dirs"))
DEFAULT_FLOW_CONFIG = dict(
    (key, int(value))
    for key, value in get_section("flow_config").items()
)

TERMINAL_EXAMPLE_STATUSES = set(["success", "failed", "timeout", "canceled"])
TERMINAL_TASK_STATUSES = set(["success", "failed", "canceled"])
DB_CONNECT_TIMEOUT_SEC = get_int(
    "database",
    "connect_timeout_sec",
    minimum=1,
)
SQLITE_BUSY_TIMEOUT_MS = get_int(
    "database",
    "busy_timeout_ms",
    env_name="PJTEST_SQLITE_BUSY_TIMEOUT_MS",
    minimum=1,
)
ADMIN_REQUEST_TIMEOUT_SEC = get_int(
    "http",
    "admin_request_timeout_sec",
    minimum=1,
)
HEALTH_CHECK_TIMEOUT_SEC = get_int(
    "http",
    "health_check_timeout_sec",
    minimum=1,
)

def get_conn():
    """Create a SQLite connection with runtime pragmas."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH), timeout=DB_CONNECT_TIMEOUT_SEC)
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
    split_mode="scan",
    result_json=None,
):
    """Insert one task and all distributable execution items atomically."""
    conn = get_conn()
    cur = conn.cursor()
    now = local_now()
    task_id = "task_" + uuid.uuid4().hex[:12]
    result_json = result_json if isinstance(result_json, dict) else {}

    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            """
            INSERT INTO tasks (
                task_id, task_name, template_name, revision, revision_policy,
                resolved_zip_path, suite, target_worker, priority, max_retry,
                max_time, work_root, target_dir, split_mode, flow_config_json,
                status, total_examples, repeat_enabled, repeat_group,
                repeat_index, parent_task_id, created_at, updated_at,
                started_at, finished_at, result_json, message
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, 'any', ?, ?, ?, ?, ?, ?, ?,
                      'pending', ?, 0, NULL, 1, NULL, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                task_id, task_name, template_name, revision, revision_policy,
                suite, priority, max_retry, max_time, work_root, target_dir,
                split_mode,
                json.dumps(flow_config, ensure_ascii=False, sort_keys=True),
                len(examples), now, now,
                json.dumps(result_json, ensure_ascii=False, sort_keys=True),
                "created with %d examples" % len(examples),
            ),
        )

        for seq, example in enumerate(examples, 1):
            example_id = "ex_" + uuid.uuid4().hex[:12]
            target_arg = example["target_arg"]
            run_tcl_path = example["run_tcl_path"]
            cmd = build_run_command(work_root, target_arg)
            example_revision = example.get("revision")
            example_zip = example.get("resolved_zip_path")

            cur.execute(
                """
                INSERT INTO task_examples (
                    example_id, task_id, seq, platform, target_arg,
                    run_tcl_path, cmd, revision, resolved_zip_path, status,
                    assigned_worker, current_attempt_id, retry_count, max_retry,
                    created_at, updated_at, started_at, finished_at, exit_code,
                    failed_step, failed_reason, message, log_file, log_tail,
                    run_log_dir, report_dir
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'pending', NULL, NULL,
                          0, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                          NULL, NULL, NULL)
                """,
                (
                    example_id, task_id, seq, target_arg, run_tcl_path, cmd,
                    str(example_revision) if example_revision is not None else None,
                    str(example_zip) if example_zip else None,
                    max_retry, now, now,
                ),
            )

        insert_event(
            cur, task_id, None, None, None, "task_created",
            "Created %s task with %d examples" % (split_mode, len(examples)),
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
    target_dir = args.target_dir or DEFAULT_TARGET_DIR

    work_root = template.get("work_root", DEFAULT_WORK_ROOT)
    suite = template.get("suite", DEFAULT_SUITE)
    priority = get_int_value(args.priority, template, "priority", DEFAULT_PRIORITY)
    max_retry = get_int_value(args.max_retry, template, "max_retry", DEFAULT_MAX_RETRY)
    max_time = get_int_value(args.max_time, template, "max_time", DEFAULT_MAX_TIME)

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



def resolve_single_run_tcl(work_root, run_tcl_input):
    """Resolve one run.tcl and return its path relative to work_root."""
    root = Path(work_root).expanduser().resolve()
    path = Path(run_tcl_input).expanduser()
    path = path.resolve() if path.is_absolute() else (root / path).resolve()

    if path.name != "run.tcl":
        raise ValueError("find target must be a file named run.tcl: %s" % path)
    if not path.is_file():
        raise ValueError("find target does not exist: %s" % path)

    ensure_inside_work_root(root, path)
    return to_posix_relative(path, root)


def scan_revision_zip_map():
    """Return revision -> stable ZIP snapshot using configured directory order."""
    revision_map = {}
    for zip_dir in get_zip_dirs():
        if not zip_dir.is_dir():
            continue
        for item in sorted(zip_dir.iterdir()):
            if not item.is_file():
                continue
            match = ZIP_RE.match(item.name)
            if not match:
                continue
            revision = int(match.group(1))
            revision_map.setdefault(revision, str(item.resolve()))
    return revision_map


def cmd_find(args):
    """Create one revision-scan task with one execution item per real ZIP."""
    template_name, template = load_template(args.template_name)
    find_config = template.get("find_fail")
    if not isinstance(find_config, dict):
        raise ValueError("template.find_fail must be a JSON object")

    confirm_fail = int(
        args.confirm_fail
        if args.confirm_fail is not None
        else find_config.get("confirm_fail", 1)
    )
    if confirm_fail != 1:
        raise ValueError(
            "revision-scan v1 requires confirm_fail=1; dynamic confirmation "
            "examples will be added in a later version"
        )

    good_revision = int(
        args.good if args.good is not None else find_config.get("good_revision")
    )
    bad_revision = int(
        args.bad if args.bad is not None else find_config.get("bad_revision")
    )
    if good_revision >= bad_revision:
        raise ValueError("good revision must be smaller than bad revision")

    work_root = template.get("work_root", DEFAULT_WORK_ROOT)
    target_arg = resolve_single_run_tcl(work_root, args.run_tcl)
    revision_zip_map = scan_revision_zip_map()

    if good_revision not in revision_zip_map:
        raise ValueError("good revision ZIP not found: %s" % good_revision)
    if bad_revision not in revision_zip_map:
        raise ValueError("bad revision ZIP not found: %s" % bad_revision)

    revisions = sorted(
        revision for revision in revision_zip_map
        if good_revision <= revision <= bad_revision
    )
    if len(revisions) < 2:
        raise ValueError("at least two existing revisions are required")

    suite = template.get("suite", DEFAULT_SUITE)
    priority = get_int_value(args.priority, template, "priority", DEFAULT_PRIORITY)
    max_retry = get_int_value(args.max_retry, template, "max_retry", DEFAULT_MAX_RETRY)
    max_time = get_int_value(args.max_time, template, "max_time", DEFAULT_MAX_TIME)
    task_name = args.name or template.get("task_name") or template_name
    flow_config = merge_flow_config(template)

    examples = [
        {
            "target_arg": target_arg,
            "run_tcl_path": target_arg,
            "revision": revision,
            "resolved_zip_path": revision_zip_map[revision],
        }
        for revision in revisions
    ]

    result_json = {
        "scan_status": "pending",
        "good_revision": good_revision,
        "bad_revision": bad_revision,
        "candidate_revisions": revisions,
        "confirm_fail": confirm_fail,
    }
    revision_display = "%s..%s" % (revisions[0], revisions[-1])

    task_id = insert_task_and_examples(
        task_name=task_name,
        template_name=template_name,
        revision_policy="per_example",
        revision=revision_display,
        suite=suite,
        priority=priority,
        max_retry=max_retry,
        max_time=max_time,
        work_root=work_root,
        target_dir=target_arg,
        flow_config=flow_config,
        examples=examples,
        split_mode="revision_scan",
        result_json=result_json,
    )

    print("Template         : %s" % template_name)
    print("Task type       : revision_scan")
    print("Target          : %s" % target_arg)
    print("Revision range  : %s" % revision_display)
    print("Revision count  : %d" % len(revisions))
    print("Good boundary   : %s" % good_revision)
    print("Bad boundary    : %s" % bad_revision)
    print("")
    print("Task created    : %s" % task_id)
    print("Status          : pending")
    print("Inspect         : ./taskctl.py show %s" % task_id)
    print("Examples        : ./taskctl.py examples %s" % task_id)

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
    """Format task-level revision or a per-example scan range."""
    keys = row.keys() if hasattr(row, "keys") else []
    split_mode = row["split_mode"] if "split_mode" in keys else None
    policy = row["revision_policy"] or "fixed"
    revision = row["revision"]

    if split_mode == "revision_scan" or policy == "per_example":
        return revision or "per-example"
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
            t.split_mode,
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
        SELECT example_id, seq, revision, resolved_zip_path, target_arg, run_tcl_path, status,
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

    header = "%-18s %4s %-10s %-10s %-10s %-7s %-6s %-45s" % (
        "EXAMPLE_ID",
        "SEQ",
        "REV",
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
            "%-18s %4d %-10s %-10s %-10s %-7s %-6s %-45s"
            % (
                format_short(row["example_id"], 18),
                row["seq"],
                format_short(row["revision"] or "-", 10),
                format_short(row["status"], 10),
                format_short(worker, 10),
                retry,
                exit_code,
                format_short(row["target_arg"], 45),
            )
        )

        if args.verbose:
            print("    run_tcl_path       : %s" % row["run_tcl_path"])
            print("    revision           : %s" % (row["revision"] or "-"))
            print("    resolved_zip_path  : %s" % (row["resolved_zip_path"] or "-"))
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



SHARE_REPORT_DIR = get_path(
    "paths",
    "report_root",
    env_name="PJTEST_SHARE_REPORT_DIR",
)
REPORT_RETENTION_DAYS = get_int(
    "reports",
    "retention_days",
    env_name="PJTEST_REPORT_RETENTION_DAYS",
    minimum=1,
)
REPORT_OLD_DIR_NAME = get_value("reports", "old_dir_name")
REPORT_SUMMARY_DIR_NAME = get_value("reports", "summary_dir_name")


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
        SELECT example_id, seq, revision, resolved_zip_path, target_arg, status, assigned_worker,
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

    header = "%-18s %5s %-10s %-10s %-18s %-7s %-6s %s" % (
        "EXAMPLE_ID",
        "SEQ",
        "REV",
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
        print("%-18s %5s %-10s %-10s %-18s %-7s %-6s %s" % (
            row["example_id"],
            row["seq"],
            row["revision"] or "-",
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
    report_dir = get_task_report_directory(report_root, task)

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

    result_json = json_loads_dict(task["result_json"] if "result_json" in task.keys() else "{}")
    if task["split_mode"] == "revision_scan":
        print_key_value(
            "Revision Scan Result",
            [
                ("Scan Status", result_json.get("scan_status", "pending")),
                ("Good Boundary", result_json.get("good_revision")),
                ("Bad Boundary", result_json.get("bad_revision")),
                ("Last Good", result_json.get("last_good_revision")),
                ("First Bad", result_json.get("first_bad_revision")),
                ("First Bad Example", result_json.get("first_bad_example_id")),
                ("Non-monotonic", ",".join(str(x) for x in result_json.get("non_monotonic_revisions", [])) or "-"),
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
        SELECT example_id, seq, revision, resolved_zip_path, target_arg, run_tcl_path, status,
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

    header = "%-18s %4s %-10s %-10s %-10s %-7s %-6s %-45s" % (
        "EXAMPLE_ID", "SEQ", "REV", "STATUS", "WORKER",
        "RETRY", "EXIT", "TARGET_ARG",
    )
    print(header)
    print("-" * len(header))

    for row in rows:
        worker = row["assigned_worker"] or "-"
        retry = "%d/%d" % (row["retry_count"], row["max_retry"])
        exit_code = "-" if row["exit_code"] is None else str(row["exit_code"])
        print(
            "%-18s %4d %-10s %-10s %-10s %-7s %-6s %-45s"
            % (
                format_short(row["example_id"], 18), row["seq"],
                format_short(row["revision"] or "-", 10),
                format_short(row["status"], 10), format_short(worker, 10),
                retry, exit_code, format_short(row["target_arg"], 45),
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
        SELECT example_id, seq, revision, resolved_zip_path, target_arg, run_tcl_path, status,
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
    elapsed_seconds, elapsed_time = get_task_elapsed(task)

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
        "updated_at       %s" % (task["updated_at"] or ""),
        "started_at       %s" % (task["started_at"] or ""),
        "finished_at      %s" % (task["finished_at"] or ""),
        "elapsed_seconds  %s" % (elapsed_seconds if elapsed_seconds is not None else "-"),
        "elapsed_time     %s" % elapsed_time,
    ]

    if get_task_field(task, "split_mode") == "revision_scan":
        result = json_loads_dict(get_task_field(task, "result_json", "{}"))
        lines.extend([
            "scan_status      %s" % clean_report_text(result.get("scan_status")),
            "last_good        %s" % clean_report_text(result.get("last_good_revision")),
            "first_bad        %s" % clean_report_text(result.get("first_bad_revision")),
            "non_monotonic    %s" % clean_report_text(result.get("non_monotonic_revisions")),
        ])

    lines.append("")
    return "\n".join(lines)


def build_status_summary(rows):
    """Build status_summary file content."""
    lines = []
    lines.append(
        "SEQ\tEXAMPLE_ID\tREVISION\tSTATUS\tWORKER\tRETRY\tEXIT\tTARGET_ARG\tZIP_PATH\tLOG_FILE\tMESSAGE"
    )

    for row in rows:
        retry = "%d/%d" % (row["retry_count"], row["max_retry"])
        exit_code = "-" if row["exit_code"] is None else str(row["exit_code"])
        worker = row["assigned_worker"] or "-"

        lines.append(
            "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s"
            % (
                row["seq"],
                clean_report_text(row["example_id"]),
                clean_report_text(row["revision"] or "-"),
                clean_report_text(row["status"]),
                clean_report_text(worker),
                clean_report_text(retry),
                clean_report_text(exit_code),
                clean_report_text(row["target_arg"]),
                clean_report_text(row["resolved_zip_path"] or "-"),
                clean_report_text(row["log_file"]),
                clean_report_text(row["message"]),
            )
        )

    lines.append("")
    return "\n".join(lines)


def build_list_file(rows, status):
    """Build a path list; revision scans prefix each repeated target."""
    selected = [row for row in rows if row["status"] == status]
    lines = []
    for row in selected:
        if row["revision"]:
            lines.append("r%s\t%s" % (row["revision"], row["target_arg"]))
        else:
            lines.append(row["target_arg"])
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
    if get_task_field(task, "split_mode") == "revision_scan":
        return "scan_%s" % sanitize_report_component(task["revision"], "revision_scan")
    revision = task["revision"]
    revision_text = format_revision(task)
    if revision and re.match(r"^\d+$", str(revision)):
        return "r%s" % revision
    return sanitize_report_component(revision_text, "unknown_revision")

def get_task_field(task, key, default=None):
    """Return one task field from a dict or sqlite row."""
    try:
        value = task[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def get_task_report_dir_name(task):
    """Return a readable report directory name such as place_task_xxx."""
    template_name = (
        get_task_field(task, "template_name")
        or get_task_field(task, "task_name")
        or "task"
    )
    task_id = get_task_field(task, "task_id", "unknown_task")
    return "%s_%s" % (
        sanitize_report_component(template_name, "task"),
        sanitize_report_component(task_id, "unknown_task"),
    )


def parse_report_time(value):
    """Parse a task timestamp stored by PJTest."""
    if not value:
        return None
    return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")


def get_report_cutoff_date():
    """Return the oldest calendar date kept in the active report tree."""
    return datetime.now().date() - timedelta(days=REPORT_RETENTION_DAYS - 1)


def should_archive_task_report(task):
    """Return True when a terminal task is older than the retention window."""
    if get_task_field(task, "status") not in TERMINAL_TASK_STATUSES:
        return False

    try:
        created_time = parse_report_time(get_task_field(task, "created_at"))
    except Exception:
        return False

    return bool(created_time and created_time.date() < get_report_cutoff_date())


def get_task_report_directory(report_root, task):
    """Return the active or archived report directory for one task."""
    revision_dir = report_revision_dir(task)
    task_dir_name = get_task_report_dir_name(task)

    if should_archive_task_report(task):
        return Path(report_root) / REPORT_OLD_DIR_NAME / revision_dir / task_dir_name

    return Path(report_root) / revision_dir / task_dir_name


def move_report_tree(source, destination):
    """Move a report directory while preserving files already at destination."""
    source = Path(source)
    destination = Path(destination)

    if not source.exists() or source == destination:
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.move(str(source), str(destination))
        return True

    if source.is_dir() and destination.is_dir():
        for child in source.iterdir():
            target = destination / child.name
            if child.is_dir() and target.is_dir():
                move_report_tree(child, target)
            else:
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(str(target))
                    else:
                        target.unlink()
                shutil.move(str(child), str(target))
        try:
            source.rmdir()
        except OSError:
            pass
        return True

    if destination.is_dir():
        shutil.rmtree(str(destination))
    else:
        destination.unlink()
    shutil.move(str(source), str(destination))
    return True


def prepare_task_report_directory(report_root, task):
    """Normalize legacy names and place one task in active or Old storage."""
    report_root = Path(report_root)
    revision_dir = report_revision_dir(task)
    task_id = str(get_task_field(task, "task_id", "unknown_task"))
    task_dir_name = get_task_report_dir_name(task)

    active_base = report_root / revision_dir
    old_base = report_root / REPORT_OLD_DIR_NAME / revision_dir
    target_dir = get_task_report_directory(report_root, task)

    candidates = [
        active_base / task_id,
        old_base / task_id,
        active_base / task_dir_name,
        old_base / task_dir_name,
    ]
    for candidate in candidates:
        if candidate != target_dir and candidate.exists():
            move_report_tree(candidate, target_dir)

    return target_dir


def cleanup_empty_report_directories(report_root):
    """Remove empty revision directories without touching Old or Summary."""
    report_root = Path(report_root)
    if not report_root.is_dir():
        return

    for child in report_root.iterdir():
        if child.name in (REPORT_OLD_DIR_NAME, REPORT_SUMMARY_DIR_NAME):
            continue
        if child.is_dir():
            try:
                child.rmdir()
            except OSError:
                pass

    old_root = report_root / REPORT_OLD_DIR_NAME
    if old_root.is_dir():
        for child in old_root.iterdir():
            if child.is_dir():
                try:
                    child.rmdir()
                except OSError:
                    pass


def maintain_report_directories(report_root):
    """Normalize names and archive terminal reports outside the retention window."""
    report_root = Path(report_root)
    report_root.mkdir(parents=True, exist_ok=True)
    (report_root / REPORT_OLD_DIR_NAME).mkdir(parents=True, exist_ok=True)
    (report_root / REPORT_SUMMARY_DIR_NAME).mkdir(parents=True, exist_ok=True)

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT task_id, task_name, template_name, revision,
                   revision_policy, status, created_at
            FROM tasks
            ORDER BY id ASC
            """
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    for task in rows:
        prepare_task_report_directory(report_root, task)

    cleanup_empty_report_directories(report_root)


def get_task_elapsed(task):
    """Return task elapsed seconds and a readable duration string."""
    started_at = get_task_field(task, "started_at")
    if not started_at:
        return None, "-"

    try:
        start_time = parse_report_time(started_at)
        end_time = parse_report_time(get_task_field(task, "finished_at"))
        if end_time is None:
            end_time = datetime.now()
    except Exception:
        return None, "-"

    elapsed_seconds = max(0, int((end_time - start_time).total_seconds()))
    days, remainder = divmod(elapsed_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    if days:
        elapsed_text = "%dd %02d:%02d:%02d" % (days, hours, minutes, seconds)
    else:
        elapsed_text = "%02d:%02d:%02d" % (hours, minutes, seconds)

    return elapsed_seconds, elapsed_text


def is_latest_template_task(task):
    """Return True when this task is the newest task for its template."""
    template_name = get_task_field(task, "template_name")
    task_id = get_task_field(task, "task_id")
    if not template_name or not task_id:
        return False

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT task_id
            FROM tasks
            WHERE template_name = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (template_name,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    return bool(row and row["task_id"] == task_id)


def write_latest_template_summary(report_root, task, content):
    """Overwrite the latest stat_summary file for one template."""
    if not is_latest_template_task(task):
        return False

    template_name = sanitize_report_component(
        get_task_field(task, "template_name")
        or get_task_field(task, "task_name")
        or "task",
        "task",
    )
    summary_path = (
        Path(report_root)
        / REPORT_SUMMARY_DIR_NAME
        / ("%s_stat_summary" % template_name)
    )
    write_text_file(summary_path, content)
    return True


def write_task_report(task_id, out_dir=None):
    """Generate final report files from database for one task.

    Reports are written under either:
        <report_root>/<revision>/<template>_<task_id>/
        <report_root>/Old/<revision>/<template>_<task_id>/
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
    prepare_task_report_directory(report_root, task)
    task_dir = get_task_report_directory(report_root, task)
    stat_summary = build_stat_summary(task, counts, workers)

    files = {
        "stat_summary": stat_summary,
        "status_summary": build_status_summary(rows),
        "list_pass_to_run": build_list_file(rows, "success"),
        "list_fail_to_run": build_list_file(rows, "failed"),
        "timeout_list": build_list_file(rows, "timeout"),
    }

    task_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in files.items():
        write_text_file(task_dir / filename, content)

    write_latest_template_summary(report_root, task, stat_summary)
    maintain_report_directories(report_root)
    task_dir = get_task_report_directory(report_root, task)
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

    # Support either a top-level list or a mapping with a tasks key.
    # Format 1: [{template: place, target: .}, ...]
    # Format 2: {tasks: [...]}
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
        target = entry.get("target", DEFAULT_TARGET_DIR)

        if not template:
            print("[%d/%d] SKIP: missing 'template' field" % (idx, total))
            failed += 1
            continue

        # Build an argparse-like object and reuse the add command implementation.
        class Args:
            pass
        task_args = Args()
        task_args.template_name = template
        task_args.target_dir = target
        task_args.revision = entry.get("revision")  # None selects the latest revision.
        task_args.name = entry.get("name")
        task_args.priority = entry.get("priority")
        task_args.max_retry = entry.get("max_retry")
        task_args.max_time = entry.get("max_time")

        try:
            # Reuse the normal add command path.
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
    or get_value("scheduler", "url")
).rstrip("/")

DEFAULT_ZIP_DIRS = get_path_list("paths", "zip_dirs")

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
        with urlopen(health_url, timeout=HEALTH_CHECK_TIMEOUT_SEC) as resp:
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
    print("Config file             : %s" % CONFIG_FILE)

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

            cur.execute("PRAGMA table_info(task_examples)")
            example_columns = set(row[1] for row in cur.fetchall())
            missing_revision_columns = sorted(
                {"revision", "resolved_zip_path"} - example_columns
            )
            if missing_revision_columns:
                print_check_item(
                    "revision scan schema",
                    False,
                    "missing task_examples columns: %s" % ", ".join(missing_revision_columns),
                )
                ok_all = False
            else:
                print_check_item("revision scan schema", True, "task_examples revision snapshot columns exist")

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


def compute_revision_scan_result(cur, task_id, existing_result=None):
    """Compute first-bad metadata after all scan examples are terminal."""
    cur.execute(
        """
        SELECT example_id, revision, status, message, log_file
        FROM task_examples
        WHERE task_id = ?
        ORDER BY CAST(revision AS INTEGER), seq, id
        """,
        (task_id,),
    )
    rows = cur.fetchall()
    result = dict(existing_result or {})
    result["candidate_revisions"] = [int(row["revision"]) for row in rows]

    if not rows:
        result["scan_status"] = "no_examples"
        return "failed", "Revision scan has no examples.", result

    first = rows[0]
    last = rows[-1]
    if first["status"] != "success":
        result["scan_status"] = "invalid_good_boundary"
        return "failed", "Good boundary r%s did not pass." % first["revision"], result
    if last["status"] not in ("failed", "timeout"):
        result["scan_status"] = "invalid_bad_boundary"
        return "failed", "Bad boundary r%s did not fail." % last["revision"], result

    first_bad = None
    last_good = None
    success_after_bad = []
    for row in rows:
        if row["status"] in ("failed", "timeout") and first_bad is None:
            first_bad = row
        elif row["status"] == "success":
            if first_bad is None:
                last_good = row
            else:
                success_after_bad.append(int(row["revision"]))

    if first_bad is None:
        result["scan_status"] = "no_failure_found"
        return "failed", "Revision scan completed without a failing revision.", result

    result.update({
        "scan_status": "non_monotonic" if success_after_bad else "failure_found",
        "last_good_revision": int(last_good["revision"]) if last_good else None,
        "first_bad_revision": int(first_bad["revision"]),
        "last_good_example_id": last_good["example_id"] if last_good else None,
        "first_bad_example_id": first_bad["example_id"],
        "first_bad_message": first_bad["message"] or "",
        "first_bad_log_file": first_bad["log_file"] or "",
        "non_monotonic_revisions": success_after_bad,
    })
    message = "Revision scan found first bad r%s; last good r%s" % (
        first_bad["revision"], last_good["revision"] if last_good else "-",
    )
    if success_after_bad:
        message += "; non-monotonic success after failure: %s" % ",".join(
            str(value) for value in success_after_bad
        )
    return "success", message, result

def refresh_parent_task_status(cur, task_id):
    """Refresh one task after an administrative database edit."""
    cur.execute(
        """
        SELECT t.task_id, t.status, t.split_mode, t.result_json,
               t.started_at, t.finished_at,
               COUNT(e.example_id) AS total,
               SUM(CASE WHEN e.status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
               SUM(CASE WHEN e.status = 'running' THEN 1 ELSE 0 END) AS running_count,
               SUM(CASE WHEN e.status = 'success' THEN 1 ELSE 0 END) AS success_count,
               SUM(CASE WHEN e.status IN ('failed', 'timeout') THEN 1 ELSE 0 END) AS failed_count,
               SUM(CASE WHEN e.status = 'canceled' THEN 1 ELSE 0 END) AS canceled_count,
               SUM(CASE WHEN e.status IN ('success', 'failed', 'timeout', 'canceled') THEN 1 ELSE 0 END) AS done_count
        FROM tasks t LEFT JOIN task_examples e ON t.task_id = e.task_id
        WHERE t.task_id = ? GROUP BY t.task_id
        """,
        (task_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError("Task not found: %s" % task_id)

    old_status = row["status"]
    total = int(row["total"] or 0)
    pending = int(row["pending_count"] or 0)
    running = int(row["running_count"] or 0)
    success = int(row["success_count"] or 0)
    failed = int(row["failed_count"] or 0)
    canceled = int(row["canceled_count"] or 0)
    done = int(row["done_count"] or 0)
    result_json = json_loads_dict(row["result_json"])
    result_json.update({"total": total, "done": done, "success": success,
                        "failed": failed, "running": running, "pending": pending})

    if total <= 0:
        new_status, message = "failed", "No examples found."
    elif old_status == "canceling" and (running > 0 or pending > 0):
        new_status, message = "canceling", "Task canceling. progress=%d/%d" % (done, total)
    elif running > 0:
        new_status, message = "running", "Task is running. progress=%d/%d" % (done, total)
    elif pending > 0:
        new_status = "running" if done > 0 else "pending"
        message = "Task is partially done. progress=%d/%d" % (done, total) if done else "Task is pending. progress=0/%d" % total
    elif canceled > 0:
        new_status, message = "canceled", "Task canceled. canceled=%d total=%d" % (canceled, total)
    elif row["split_mode"] == "revision_scan":
        new_status, message, result_json = compute_revision_scan_result(cur, task_id, result_json)
    elif failed > 0:
        new_status, message = "failed", "Task finished with failures. success=%d failed=%d total=%d" % (success, failed, total)
    elif success == total:
        new_status, message = "success", "Task finished successfully. total=%d" % total
    else:
        new_status, message = "running", "Task status is being reconciled. progress=%d/%d" % (done, total)

    now = local_now()
    started_at = row["started_at"]
    finished_at = row["finished_at"]
    if new_status == "running" and not started_at:
        started_at = now
    if new_status in TERMINAL_TASK_STATUSES and not finished_at:
        finished_at = now
    if new_status not in TERMINAL_TASK_STATUSES:
        finished_at = None

    cur.execute(
        """
        UPDATE tasks SET status = ?, started_at = ?, finished_at = ?,
                         updated_at = ?, result_json = ?, message = ?
        WHERE task_id = ?
        """,
        (new_status, started_at, finished_at, now,
         json.dumps(result_json, ensure_ascii=False, sort_keys=True), message, task_id),
    )
    if old_status != new_status:
        insert_event(cur, task_id, None, None, None, "task_status_changed", "%s -> %s" % (old_status, new_status))
    return new_status

def request_scheduler_report_refresh(task_id, scheduler_url=None):
    """Ask the scheduler to refresh reports outside this command process."""
    base_url = (scheduler_url or DEFAULT_SCHEDULER_URL).rstrip("/")
    url = base_url + "/api/task/refresh-report"
    body = json.dumps({"task_id": task_id}).encode("utf-8")
    req = Request(url, data=body)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Content-Length", str(len(body)))

    with urlopen(req, timeout=ADMIN_REQUEST_TIMEOUT_SEC) as response:
        raw = response.read().decode("utf-8", errors="replace")

    data = json.loads(raw) if raw.strip() else {}
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "scheduler rejected report refresh")
    return data


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
    """Manually repair one example and keep parent/worker state consistent."""
    status = args.status
    exit_code = args.exit_code
    if exit_code is None:
        exit_code = default_exit_code_for_status(status)

    if status in ("failed", "timeout") and exit_code == 0:
        raise ValueError(
            "%s status cannot use exit code 0; omit --exit-code or use a non-zero value"
            % status
        )
    if status == "success" and exit_code not in (None, 0):
        raise ValueError("success status must use exit code 0")

    conn = get_conn()
    cur = conn.cursor()
    now = local_now()
    task_id = None

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

        task_id = example["task_id"]
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

        if example["assigned_worker"]:
            cur.execute(
                """
                UPDATE workers
                SET status = 'idle',
                    current_task_id = NULL,
                    current_example_id = NULL,
                    current_attempt_id = NULL,
                    updated_at = ?,
                    message = ?
                WHERE worker_name = ?
                  AND current_task_id = ?
                  AND current_example_id = ?
                  AND current_attempt_id = ?
                """,
                (
                    now,
                    "assignment cleared by repair-example",
                    example["assigned_worker"],
                    task_id,
                    args.example_id,
                    attempt_id,
                ),
            )

        insert_event(
            cur,
            task_id,
            args.example_id,
            attempt_id,
            example["assigned_worker"],
            "example_manual_repair",
            message,
        )

        new_task_status = refresh_parent_task_status(cur, task_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    report_message = "queued"
    try:
        request_scheduler_report_refresh(task_id)
    except Exception as scheduler_exc:
        try:
            write_task_report(task_id)
            report_message = "local fallback (%s)" % scheduler_exc
        except Exception as local_exc:
            report_message = "failed: scheduler=%s local=%s" % (
                scheduler_exc,
                local_exc,
            )

    print("Example repaired: %s status=%s exit_code=%s" % (
        args.example_id,
        status,
        exit_code if exit_code is not None else "-",
    ))
    print("Task status    : %s" % new_task_status)
    print("Report refresh : %s" % report_message)

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
        default=DEFAULT_TARGET_DIR,
        help="target directory under work_root, default from config",
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

    p_find = sub.add_parser("find", help="create one vertical revision-scan task")
    p_find.add_argument("template_name", help="template containing find_fail settings")
    p_find.add_argument("run_tcl", help="one run.tcl path, absolute or relative to work_root")
    p_find.add_argument("--good", type=int, default=None, help="override known good revision")
    p_find.add_argument("--bad", type=int, default=None, help="override known bad revision")
    p_find.add_argument("--confirm-fail", type=int, default=None, help="must be 1 in revision-scan v1")
    p_find.add_argument("--name", help="task display name")
    p_find.add_argument("--priority", type=int, help="priority, default from template")
    p_find.add_argument("--max-retry", type=int, help="retry count per revision")
    p_find.add_argument("--max-time", type=int, help="timeout seconds per revision")
    p_find.set_defaults(func=cmd_find)

    p_list = sub.add_parser("list", help="list recent tasks")
    p_list.add_argument("--status", help="filter by task status")
    p_list.add_argument(
        "-n",
        "--limit",
        type=int,
        default=DEFAULT_LIST_LIMIT,
        help="number of recent tasks to show, default from config",
    )
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="show full task detail and configuration")
    p_show.add_argument("task_id", nargs="?", default=None, help="task id, default: latest")
    p_show.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_SHOW_LIMIT,
        help="max recent examples/attempts to show, default from config",
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
        help="output report root, default from config",
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
        help="scheduler URL, default from config or environment",
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
