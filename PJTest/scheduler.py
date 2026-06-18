#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PJTest central HTTP scheduler.

Run this only on the central host, usually pudong.  Workers do not access the
SQLite database directly.  A worker repeatedly asks this scheduler for one
pending test example, executes it, and reports the result back.

Main APIs:
    POST /api/worker/register
    POST /api/worker/heartbeat
    POST /api/task/pull
    POST /api/task/report
    GET  /api/tasks
    GET  /api/examples?task_id=...
    GET  /api/workers
    GET  /api/build/latest

Usage:
    ./scheduler.py --host 0.0.0.0 --port 9000
"""

import argparse
import json
import os
import re
import shlex
import shutil
import socketserver
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


DB_PATH = Path(os.environ.get(
    "PJTEST_DB_PATH",
    "/home/user3/PJTest/data/task_queue.db",
))

DEFAULT_ZIP_DIRS = [
    Path("/home/xshare/zw_cache/distributed_test_system/GalaxCore_bin/zip"),
]

ZIP_RE = re.compile(r"^Galax[Cc]ore_(\d+)\.zip$")
TERMINAL_TASK_STATUSES = set(["success", "failed", "canceled"])
TERMINAL_EXAMPLE_STATUSES = set(["success", "failed", "timeout", "canceled"])

REPORT_ROOT = Path(os.environ.get(
    "PJTEST_SHARE_REPORT_DIR",
    "/home/xshare/zw_cache/distributed_test_system/reports",
))

# 公共日志目录（用于存放已完成任务的报告副本）
RUN_LOG_DIR = Path(os.environ.get(
    "PJTEST_RUN_LOG_DIR",
    os.path.expanduser("~xiaonan/Share/zw_cache/run_log"),
))

# The share root is intentionally kept clean.  Scheduler-generated task
# summaries are written only under:
#     REPORT_ROOT / <revision> / <task_id> /
# Do not write flat latest files such as execution_report.json or
# stat_summary into /home/xshare/zw_cache/distributed_test_system.

SCHEDULER_LOG_ROOT = Path(os.environ.get(
    "PJTEST_SCHEDULER_LOG_ROOT",
    "/home/user3/PJTest/logs/scheduler",
))

SCHEDULER_DEBUG = os.environ.get(
    "PJTEST_SCHEDULER_DEBUG",
    "0",
) != "0"

LOG_LOCK = threading.Lock()
DB_WRITE_LOCK = threading.RLock()
DB_LOCK_RETRIES = int(os.environ.get("PJTEST_DB_LOCK_RETRIES", "8"))
DB_LOCK_RETRY_BASE_SEC = float(os.environ.get("PJTEST_DB_LOCK_RETRY_BASE_SEC", "0.25"))
SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get("PJTEST_SQLITE_BUSY_TIMEOUT_MS", "60000"))
REQUEUE_STALE_RUNNING = os.environ.get("PJTEST_REQUEUE_STALE_RUNNING", "0") != "0"



class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """Threaded HTTP server for concurrent worker requests."""

    daemon_threads = True


def get_conn():
    """Create a SQLite connection with runtime pragmas."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=%d" % SQLITE_BUSY_TIMEOUT_MS)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_database_pragmas():
    """Initialize SQLite database mode once at scheduler startup."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    try:
        conn.execute("PRAGMA busy_timeout=%d" % SQLITE_BUSY_TIMEOUT_MS)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
    finally:
        conn.close()


def is_database_locked_error(exc):
    """Return True when an exception is caused by SQLite write locking."""
    return isinstance(exc, sqlite3.OperationalError) and "database is locked" in str(exc).lower()


def run_db_write_with_retry(func, context):
    """Run one database write operation with SQLite lock retry."""
    last_exc = None

    for attempt in range(1, DB_LOCK_RETRIES + 1):
        try:
            return func()
        except sqlite3.OperationalError as exc:
            if not is_database_locked_error(exc):
                raise

            last_exc = exc
            sleep_sec = DB_LOCK_RETRY_BASE_SEC * attempt
            log_scheduler(
                "WARN",
                "%s database is locked, retry %d/%d after %.2fs" % (
                    context,
                    attempt,
                    DB_LOCK_RETRIES,
                    sleep_sec,
                ),
            )
            time.sleep(sleep_sec)

    raise last_exc


def local_now():
    """Return local time string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_scheduler_log_path():
    """Return today's scheduler main log file path."""
    date_tag = datetime.now().strftime("%Y-%m-%d")
    return SCHEDULER_LOG_ROOT / ("scheduler_%s.log" % date_tag)


def get_task_log_path(task_id):
    """Return task-specific scheduler log file path."""
    return SCHEDULER_LOG_ROOT / "tasks" / ("%s.log" % task_id)


def append_log_line(path, line):
    """Append one line to a log file safely."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def log_scheduler(level, message, task_id=None, also_stdout=True):
    """Append one scheduler log line to the main log and optional task log."""
    level = str(level).upper()
    line = "[%s] [%s] %s" % (local_now(), level, message)

    try:
        with LOG_LOCK:
            append_log_line(get_scheduler_log_path(), line)
            if task_id:
                append_log_line(get_task_log_path(task_id), line)
    except Exception as exc:
        sys.stderr.write("[%s] [WARN] failed to write scheduler log: %s\n" % (local_now(), exc))
        sys.stderr.flush()

    if also_stdout:
        print(line)
        sys.stdout.flush()


def log_exception(context, exc, task_id=None):
    """Log an exception with traceback."""
    log_scheduler("ERROR", "%s: %s" % (context, exc), task_id=task_id)
    trace = traceback.format_exc().rstrip()
    if trace:
        for line in trace.splitlines():
            log_scheduler("ERROR", line, task_id=task_id, also_stdout=False)


def parse_local_time(value):
    """Parse a local time string."""
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def json_dumps(data):
    """Dump JSON as UTF-8 bytes with a trailing newline."""
    return (json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def read_json(handler):
    """Read JSON body from a BaseHTTPRequestHandler."""
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}

    body = handler.rfile.read(length).decode("utf-8")
    if not body.strip():
        return {}

    return json.loads(body)


def row_to_dict(row):
    """Convert sqlite row to dict."""
    if row is None:
        return None
    return dict((key, row[key]) for key in row.keys())


def json_loads_dict(value):
    """Load a JSON object from text."""
    if not value:
        return {}

    try:
        data = json.loads(value)
    except Exception:
        return {}

    return data if isinstance(data, dict) else {}


def get_zip_dirs():
    """Return configured zip directories."""
    override = os.environ.get("GALAXCORE_ZIP_DIR")
    if override:
        return [Path(item) for item in override.split(os.pathsep) if item]
    return list(DEFAULT_ZIP_DIRS)


def find_latest_compiled_zip():
    """Return (revision, zip_path) for the largest compiled GalaxCore zip."""
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
                best = (revision, str(item))

    return best


def find_zip_for_revision(revision):
    """Return zip path for a specific revision, or None."""
    if revision is None or str(revision).strip() == "":
        return None

    revision_text = str(revision)
    expected_names = [
        "GalaxCore_%s.zip" % revision_text,
        "Galaxcore_%s.zip" % revision_text,
    ]

    for zip_dir in get_zip_dirs():
        if not zip_dir.is_dir():
            continue

        for name in expected_names:
            path = zip_dir / name
            if path.is_file():
                return str(path)

        for item in zip_dir.iterdir():
            match = ZIP_RE.match(item.name)
            if match and match.group(1) == revision_text:
                return str(item)

    return None


def insert_event(cur, task_id, example_id, attempt_id, worker_name, event, message):
    """Insert one scheduler event."""
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

    log_parts = ["event=%s" % event]
    if worker_name:
        log_parts.append("worker=%s" % worker_name)
    if task_id:
        log_parts.append("task=%s" % task_id)
    if example_id:
        log_parts.append("example=%s" % example_id)
    if attempt_id:
        log_parts.append("attempt=%s" % attempt_id)
    if message:
        log_parts.append("message=%s" % message)

    log_scheduler("EVENT", " ".join(log_parts), task_id=task_id, also_stdout=False)


def upsert_worker(
    cur,
    worker_name,
    hostname=None,
    status="idle",
    task_id=None,
    example_id=None,
    attempt_id=None,
    capabilities=None,
    message=None,
):
    """Insert or update a worker row."""
    now = local_now()
    capabilities_json = None

    if capabilities is not None:
        capabilities_json = json.dumps(capabilities, ensure_ascii=False, sort_keys=True)

    cur.execute(
        "SELECT worker_name, started_at FROM workers WHERE worker_name = ?",
        (worker_name,),
    )
    row = cur.fetchone()

    if row:
        if hostname is None:
            cur.execute("SELECT hostname FROM workers WHERE worker_name = ?", (worker_name,))
            hostname = cur.fetchone()["hostname"]

        if capabilities_json is None:
            cur.execute(
                "SELECT capabilities_json FROM workers WHERE worker_name = ?",
                (worker_name,),
            )
            capabilities_json = cur.fetchone()["capabilities_json"]

        cur.execute(
            """
            UPDATE workers
            SET hostname = ?,
                status = ?,
                current_task_id = ?,
                current_example_id = ?,
                current_attempt_id = ?,
                capabilities_json = ?,
                last_seen_at = ?,
                updated_at = ?,
                message = ?
            WHERE worker_name = ?
            """,
            (
                hostname,
                status,
                task_id,
                example_id,
                attempt_id,
                capabilities_json,
                now,
                now,
                message,
                worker_name,
            ),
        )
    else:
        cur.execute(
            """
            INSERT INTO workers (
                worker_name,
                hostname,
                status,
                current_task_id,
                current_example_id,
                current_attempt_id,
                capabilities_json,
                last_seen_at,
                started_at,
                updated_at,
                message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                worker_name,
                hostname,
                status,
                task_id,
                example_id,
                attempt_id,
                capabilities_json or "{}",
                now,
                now,
                now,
                message,
            ),
        )


def refresh_one_task_status(cur, task_id):
    """Refresh parent task status from child examples."""
    cur.execute(
        """
        SELECT
            t.task_id,
            t.status,
            t.started_at,
            t.finished_at,
            COUNT(e.example_id) AS total,
            SUM(CASE WHEN e.status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
            SUM(CASE WHEN e.status = 'running' THEN 1 ELSE 0 END) AS running_count,
            SUM(CASE WHEN e.status = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN e.status IN ('failed', 'timeout') THEN 1 ELSE 0 END) AS failed_count,
            SUM(CASE WHEN e.status = 'canceled' THEN 1 ELSE 0 END) AS canceled_count,
            SUM(CASE WHEN e.status IN ('success', 'failed', 'timeout', 'canceled') THEN 1 ELSE 0 END) AS done_count
        FROM tasks t
        LEFT JOIN task_examples e
            ON t.task_id = e.task_id
        WHERE t.task_id = ?
        GROUP BY t.task_id
        """,
        (task_id,),
    )
    row = cur.fetchone()
    if not row:
        return False

    old_status = row["status"]
    total = row["total"] or 0
    pending = row["pending_count"] or 0
    running = row["running_count"] or 0
    success = row["success_count"] or 0
    failed = row["failed_count"] or 0
    canceled = row["canceled_count"] or 0
    done = row["done_count"] or 0

    if total <= 0:
        new_status = "failed"
        message = "No examples found."
    elif old_status == "canceling" and running > 0:
        new_status = "canceling"
        message = "Task canceling. waiting_running=%d progress=%d/%d" % (
            running,
            done,
            total,
        )
    elif old_status == "canceling" and pending > 0:
        new_status = "canceling"
        message = "Task canceling. pending examples should be canceled. pending=%d" % pending
    elif running > 0:
        new_status = "running"
        message = "Task is running. progress=%d/%d" % (done, total)
    elif pending > 0:
        if done > 0:
            new_status = "running"
            message = "Task is partially done. progress=%d/%d" % (done, total)
        else:
            new_status = "pending"
            message = "Task is pending. progress=0/%d" % total
    elif canceled > 0:
        new_status = "canceled"
        message = "Task canceled. canceled=%d done=%d total=%d" % (
            canceled,
            done,
            total,
        )
    elif failed > 0:
        new_status = "failed"
        message = "Task finished with failures. success=%d failed=%d total=%d" % (
            success,
            failed,
            total,
        )
    elif success == total:
        new_status = "success"
        message = "Task finished successfully. total=%d" % total
    else:
        new_status = "running"
        message = "Task status is being reconciled. progress=%d/%d" % (done, total)

    now = local_now()
    started_at = row["started_at"]
    finished_at = row["finished_at"]

    set_started_at = started_at
    set_finished_at = finished_at

    if new_status == "running" and not started_at:
        set_started_at = now

    if new_status in TERMINAL_TASK_STATUSES and not finished_at:
        set_finished_at = now

    if new_status not in TERMINAL_TASK_STATUSES:
        set_finished_at = None

    cur.execute(
        """
        UPDATE tasks
        SET status = ?,
            started_at = ?,
            finished_at = ?,
            updated_at = ?,
            message = ?
        WHERE task_id = ?
        """,
        (
            new_status,
            set_started_at,
            set_finished_at,
            now,
            message,
            task_id,
        ),
    )

    if old_status != new_status:
        insert_event(
            cur,
            task_id,
            None,
            None,
            None,
            "task_status_changed",
            "%s -> %s" % (old_status, new_status),
        )
        return True  # 状态发生了变化

    return False


def refresh_all_task_statuses(cur):
    """Refresh all non-terminal parent tasks."""
    cur.execute(
        """
        SELECT task_id
        FROM tasks
        WHERE status NOT IN ('success', 'failed', 'canceled')
        ORDER BY id
        """
    )
    rows = cur.fetchall()

    changed = 0
    for row in rows:
        if refresh_one_task_status(cur, row["task_id"]):
            changed += 1

    return changed


def resolve_task_build(cur, task_row):
    """Resolve revision and zip path for a task row."""
    revision_policy = task_row["revision_policy"] or "fixed"
    revision = task_row["revision"]
    zip_path = task_row["resolved_zip_path"]

    if revision_policy == "latest":
        if revision and str(revision).lower() != "latest" and zip_path and Path(zip_path).is_file():
            return str(revision), zip_path, None

        latest = find_latest_compiled_zip()
        if not latest:
            return None, None, "no compiled GalaxCore zip found"

        resolved_revision, resolved_zip_path = latest
        cur.execute(
            """
            UPDATE tasks
            SET revision = ?,
                resolved_zip_path = ?,
                updated_at = ?,
                message = ?
            WHERE task_id = ?
            """,
            (
                str(resolved_revision),
                resolved_zip_path,
                local_now(),
                "latest revision resolved to %s" % resolved_revision,
                task_row["task_id"],
            ),
        )
        return str(resolved_revision), resolved_zip_path, None

    if not revision:
        return None, None, "fixed revision task has no revision"

    if zip_path and Path(zip_path).is_file():
        return str(revision), zip_path, None

    fixed_zip = find_zip_for_revision(revision)
    if not fixed_zip:
        return None, None, "compiled GalaxCore zip not found for revision %s" % revision

    cur.execute(
        """
        UPDATE tasks
        SET resolved_zip_path = ?,
            updated_at = ?
        WHERE task_id = ?
        """,
        (
            fixed_zip,
            local_now(),
            task_row["task_id"],
        ),
    )
    return str(revision), fixed_zip, None


def select_pending_example(cur, worker_name):
    """Select one pending example for a worker."""
    cur.execute(
        """
        SELECT
            e.example_id AS example_id,
            e.task_id AS task_id,
            e.seq AS seq,
            e.platform AS platform,
            e.target_arg AS target_arg,
            e.run_tcl_path AS run_tcl_path,
            e.cmd AS cmd,
            e.retry_count AS retry_count,
            e.max_retry AS example_max_retry,

            t.task_name AS task_name,
            t.template_name AS template_name,
            t.revision AS revision,
            t.revision_policy AS revision_policy,
            t.resolved_zip_path AS resolved_zip_path,
            t.suite AS suite,
            t.target_worker AS target_worker,
            t.priority AS priority,
            t.max_retry AS task_max_retry,
            t.max_time AS max_time,
            t.work_root AS work_root,
            t.target_dir AS target_dir,
            t.flow_config_json AS flow_config_json
        FROM task_examples e
        JOIN tasks t
            ON t.task_id = e.task_id
        WHERE e.status = 'pending'
          AND t.status IN ('pending', 'running')
          AND (t.target_worker = 'any' OR t.target_worker = ?)
        ORDER BY t.priority DESC, t.created_at ASC, e.seq ASC
        LIMIT 1
        """,
        (worker_name,),
    )
    return cur.fetchone()


def build_run_command(work_root, target_arg):
    """Build the exact run.sh command for one example."""
    return "cd %s && ./run.sh %s" % (
        shlex.quote(str(work_root)),
        shlex.quote(str(target_arg)),
    )


def build_example_payload(row, revision, zip_path, attempt_id, attempt_no):
    """Build JSON payload returned to worker.

    The dispatch unit is a single run.tcl example.  Even if older DB rows
    stored target_arg as the parent directory, the scheduler sends run_tcl_path
    as the real target so the worker runs exactly one test.
    """
    work_root = row["work_root"]
    install_root = str(Path(work_root).resolve().parent)
    run_tcl_path = row["run_tcl_path"] or row["target_arg"]
    target_arg = run_tcl_path or row["target_arg"] or "."
    cmd = build_run_command(work_root, target_arg)

    return {
        "task_id": row["task_id"],
        "example_id": row["example_id"],
        "attempt_id": attempt_id,
        "attempt_no": attempt_no,
        "task_name": row["task_name"],
        "template_name": row["template_name"],
        "suite": row["suite"],
        "revision": revision,
        "revision_policy": row["revision_policy"] or "fixed",
        "zip_path": zip_path,
        "galaxcore_zip_path": zip_path,
        "work_root": work_root,
        "install_root": install_root,
        "target_arg": target_arg,
        "run_tcl_path": run_tcl_path,
        "cmd": cmd,
        "max_time": row["max_time"],
        "retry_count": row["retry_count"] or 0,
        "max_retry": row["example_max_retry"] or row["task_max_retry"] or 0,
        "flow_config": json_loads_dict(row["flow_config_json"]),
    }


def _claim_example_for_worker_once(worker_name, hostname=None, capabilities=None):
    """Claim one pending example and return worker payload without retry."""
    conn = get_conn()
    cur = conn.cursor()
    now = local_now()

    try:
        cur.execute("BEGIN IMMEDIATE")

        upsert_worker(
            cur,
            worker_name=worker_name,
            hostname=hostname,
            status="idle",
            task_id=None,
            example_id=None,
            attempt_id=None,
            capabilities=capabilities,
            message="worker requested task",
        )

        row = select_pending_example(cur, worker_name)
        if not row:
            conn.commit()
            if SCHEDULER_DEBUG:
                log_scheduler(
                    "DEBUG",
                    "pull no_task worker=%s" % worker_name,
                    also_stdout=False,
                )
            return None, None

        revision, zip_path, error = resolve_task_build(cur, row)
        if error:
            cur.execute(
                """
                UPDATE tasks
                SET updated_at = ?,
                    message = ?
                WHERE task_id = ?
                """,
                (
                    now,
                    error,
                    row["task_id"],
                ),
            )
            insert_event(
                cur,
                row["task_id"],
                row["example_id"],
                None,
                worker_name,
                "pull_skipped",
                error,
            )
            conn.commit()
            return None, error

        attempt_id = "att_" + uuid.uuid4().hex[:12]
        attempt_no = int(row["retry_count"] or 0) + 1

        cur.execute(
            """
            UPDATE task_examples
            SET status = 'running',
                assigned_worker = ?,
                current_attempt_id = ?,
                updated_at = ?,
                started_at = ?,
                finished_at = NULL,
                exit_code = NULL,
                failed_step = NULL,
                failed_reason = NULL,
                message = ?
            WHERE example_id = ?
              AND status = 'pending'
            """,
            (
                worker_name,
                attempt_id,
                now,
                now,
                "claimed by worker %s" % worker_name,
                row["example_id"],
            ),
        )

        if cur.rowcount != 1:
            conn.rollback()
            return None, "example was claimed by another worker"

        cur.execute(
            """
            UPDATE tasks
            SET status = 'running',
                started_at = CASE WHEN started_at IS NULL THEN ? ELSE started_at END,
                updated_at = ?,
                message = ?
            WHERE task_id = ?
            """,
            (
                now,
                now,
                "running on worker %s" % worker_name,
                row["task_id"],
            ),
        )

        cur.execute(
            """
            INSERT INTO task_attempts (
                attempt_id,
                example_id,
                task_id,
                attempt_no,
                worker_name,
                status,
                revision,
                zip_path,
                started_at,
                message
            ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (
                attempt_id,
                row["example_id"],
                row["task_id"],
                attempt_no,
                worker_name,
                revision,
                zip_path,
                now,
                "attempt started",
            ),
        )

        upsert_worker(
            cur,
            worker_name=worker_name,
            hostname=hostname,
            status="running",
            task_id=row["task_id"],
            example_id=row["example_id"],
            attempt_id=attempt_id,
            capabilities=capabilities,
            message="running example %s" % row["example_id"],
        )

        insert_event(
            cur,
            row["task_id"],
            row["example_id"],
            attempt_id,
            worker_name,
            "example_claimed",
            "Example claimed by worker %s" % worker_name,
        )

        payload = build_example_payload(row, revision, zip_path, attempt_id, attempt_no)
        report_task_id = row["task_id"]
        log_scheduler(
            "INFO",
            "pull assigned worker=%s task=%s example=%s attempt=%s seq=%s revision=%s target=%s"
            % (
                worker_name,
                row["task_id"],
                row["example_id"],
                attempt_id,
                row["seq"],
                revision,
                payload["target_arg"],
            ),
            task_id=row["task_id"],
        )
        conn.commit()
        # 任务拉取时刷新报告（可能任务状态变为 running）
        write_share_reports_for_task(report_task_id)
        return payload, None

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def claim_example_for_worker(worker_name, hostname=None, capabilities=None):
    """Claim one pending example and return worker payload with DB lock retry."""
    return run_db_write_with_retry(
        lambda: _claim_example_for_worker_once(worker_name, hostname, capabilities),
        "pull worker=%s" % worker_name,
    )


def _finish_attempt_once(data):
    """Handle one worker report without retry."""
    worker_name = data.get("worker") or data.get("worker_name")
    task_id = data.get("task_id")
    example_id = data.get("example_id")
    attempt_id = data.get("attempt_id")
    status = data.get("status")

    log_scheduler(
        "INFO",
        "report received worker=%s task=%s example=%s attempt=%s status=%s exit=%s"
        % (
            worker_name or "-",
            task_id or "-",
            example_id or "-",
            attempt_id or "-",
            status or "-",
            data.get("exit_code"),
        ),
        task_id=task_id,
    )

    if not worker_name:
        return {"ok": False, "error": "missing worker"}, 400
    if not task_id:
        return {"ok": False, "error": "missing task_id"}, 400
    if not example_id:
        return {"ok": False, "error": "missing example_id"}, 400
    if not attempt_id:
        return {"ok": False, "error": "missing attempt_id"}, 400
    if status not in ("success", "failed", "timeout"):
        return {"ok": False, "error": "invalid status"}, 400

    exit_code = data.get("exit_code")
    timed_out = 1 if data.get("timed_out") else 0
    message = data.get("message") or data.get("error_message") or ""
    log_file = data.get("log_file")
    log_tail = data.get("log_tail")
    run_log_dir = data.get("run_log_dir")
    report_dir = data.get("report_dir")

    conn = get_conn()
    cur = conn.cursor()
    now = local_now()

    try:
        cur.execute("BEGIN IMMEDIATE")

        cur.execute(
            """
            SELECT *
            FROM task_attempts
            WHERE attempt_id = ?
            """,
            (attempt_id,),
        )
        attempt = cur.fetchone()

        if not attempt:
            conn.rollback()
            return {"ok": False, "error": "attempt not found"}, 404

        if attempt["status"] != "running":
            conn.rollback()
            return {
                "ok": True,
                "task_id": task_id,
                "example_id": example_id,
                "attempt_id": attempt_id,
                "status": attempt["status"],
                "message": "attempt already finished",
            }, 200

        if attempt["worker_name"] != worker_name:
            conn.rollback()
            return {
                "ok": False,
                "error": "attempt worker mismatch",
                "attempt_worker": attempt["worker_name"],
                "request_worker": worker_name,
            }, 400

        cur.execute(
            """
            SELECT *
            FROM task_examples
            WHERE example_id = ?
            """,
            (example_id,),
        )
        example = cur.fetchone()

        if not example:
            conn.rollback()
            return {"ok": False, "error": "example not found"}, 404

        retry_count = int(example["retry_count"] or 0)
        max_retry = int(example["max_retry"] or 0)

        cur.execute(
            """
            UPDATE task_attempts
            SET status = ?,
                finished_at = ?,
                exit_code = ?,
                timed_out = ?,
                log_file = ?,
                log_tail = ?,
                run_log_dir = ?,
                report_dir = ?,
                message = ?
            WHERE attempt_id = ?
            """,
            (
                status,
                now,
                exit_code,
                timed_out,
                log_file,
                log_tail,
                run_log_dir,
                report_dir,
                message,
                attempt_id,
            ),
        )

        final_example_status = status
        requeued = False

        if status in ("failed", "timeout") and retry_count < max_retry:
            new_retry_count = retry_count + 1
            final_example_status = "pending"
            requeued = True

            cur.execute(
                """
                UPDATE task_examples
                SET status = 'pending',
                    assigned_worker = NULL,
                    current_attempt_id = NULL,
                    retry_count = ?,
                    updated_at = ?,
                    started_at = NULL,
                    finished_at = NULL,
                    exit_code = ?,
                    failed_reason = ?,
                    message = ?,
                    log_file = ?,
                    log_tail = ?,
                    run_log_dir = ?,
                    report_dir = ?
                WHERE example_id = ?
                """,
                (
                    new_retry_count,
                    now,
                    exit_code,
                    status,
                    "requeued after %s, retry=%d/%d" % (
                        status,
                        new_retry_count,
                        max_retry,
                    ),
                    log_file,
                    log_tail,
                    run_log_dir,
                    report_dir,
                    example_id,
                ),
            )

            insert_event(
                cur,
                task_id,
                example_id,
                attempt_id,
                worker_name,
                "example_requeued",
                "Example requeued after %s. retry=%d/%d" % (
                    status,
                    new_retry_count,
                    max_retry,
                ),
            )

        else:
            failed_reason = None
            if status == "timeout":
                failed_reason = "timeout"
            elif status == "failed":
                failed_reason = "exit_code_%s" % exit_code

            cur.execute(
                """
                UPDATE task_examples
                SET status = ?,
                    assigned_worker = ?,
                    current_attempt_id = ?,
                    updated_at = ?,
                    finished_at = ?,
                    exit_code = ?,
                    failed_reason = ?,
                    message = ?,
                    log_file = ?,
                    log_tail = ?,
                    run_log_dir = ?,
                    report_dir = ?
                WHERE example_id = ?
                """,
                (
                    status,
                    worker_name,
                    attempt_id,
                    now,
                    now,
                    exit_code,
                    failed_reason,
                    message or status,
                    log_file,
                    log_tail,
                    run_log_dir,
                    report_dir,
                    example_id,
                ),
            )

            insert_event(
                cur,
                task_id,
                example_id,
                attempt_id,
                worker_name,
                "example_%s" % status,
                message or status,
            )

        upsert_worker(
            cur,
            worker_name=worker_name,
            status="idle",
            task_id=None,
            example_id=None,
            attempt_id=None,
            message="last report: %s" % status,
        )

        refresh_one_task_status(cur, task_id)
        conn.commit()
        # 每次报告后刷新共享报告
        write_share_reports_for_task(task_id)

        log_scheduler(
            "INFO",
            "report handled worker=%s task=%s example=%s attempt=%s status=%s final=%s requeued=%s exit=%s"
            % (
                worker_name,
                task_id,
                example_id,
                attempt_id,
                status,
                final_example_status,
                requeued,
                exit_code,
            ),
            task_id=task_id,
        )

        return {
            "ok": True,
            "task_id": task_id,
            "example_id": example_id,
            "attempt_id": attempt_id,
            "status": final_example_status,
            "requeued": requeued,
        }, 200

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def finish_attempt(data):
    """Handle one worker report with DB lock retry."""
    task_id = data.get("task_id")
    example_id = data.get("example_id")
    return run_db_write_with_retry(
        lambda: _finish_attempt_once(data),
        "report task=%s example=%s" % (task_id or "-", example_id or "-"),
    )


def requeue_or_fail_stale_example(cur, worker_name, example):
    """Requeue or fail one example that was owned by a stale worker."""
    now = local_now()
    retry_count = int(example["retry_count"] or 0)
    max_retry = int(example["max_retry"] or 0)
    attempt_id = example["current_attempt_id"]

    if attempt_id:
        cur.execute(
            """
            UPDATE task_attempts
            SET status = 'worker_lost',
                finished_at = ?,
                message = ?
            WHERE attempt_id = ?
              AND status = 'running'
            """,
            (
                now,
                "worker heartbeat timeout",
                attempt_id,
            ),
        )

    if retry_count < max_retry:
        new_retry_count = retry_count + 1
        cur.execute(
            """
            UPDATE task_examples
            SET status = 'pending',
                assigned_worker = NULL,
                current_attempt_id = NULL,
                retry_count = ?,
                updated_at = ?,
                started_at = NULL,
                finished_at = NULL,
                failed_reason = 'worker_offline',
                message = ?
            WHERE example_id = ?
            """,
            (
                new_retry_count,
                now,
                "requeued because worker %s became offline" % worker_name,
                example["example_id"],
            ),
        )
        event_name = "example_requeued"
        message = "Worker offline. retry=%d/%d" % (new_retry_count, max_retry)
    else:
        cur.execute(
            """
            UPDATE task_examples
            SET status = 'failed',
                assigned_worker = ?,
                updated_at = ?,
                finished_at = ?,
                failed_reason = 'worker_offline',
                message = ?
            WHERE example_id = ?
            """,
            (
                worker_name,
                now,
                now,
                "failed because worker %s became offline and retry limit reached" % worker_name,
                example["example_id"],
            ),
        )
        event_name = "example_failed"
        message = "Worker offline and retry limit reached."

    insert_event(
        cur,
        example["task_id"],
        example["example_id"],
        attempt_id,
        worker_name,
        event_name,
        message,
    )



def atomic_write_text(path, text):
    """Write a text file atomically using a unique temporary file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex[:12]))

    try:
        with open(str(tmp_path), "w", encoding="utf-8") as f:
            f.write(text)

        os.replace(str(tmp_path), str(path))
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def atomic_write_json(path, data):
    """Write a JSON file atomically."""
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, text)


def safe_int(value, default=0):
    """Convert SQLite aggregate values to int safely."""
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def get_task_report_data(task_id):
    """Build a complete report dict for one task from SQLite."""
    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT *
            FROM tasks
            WHERE task_id = ?
            """,
            (task_id,),
        )
        task = cur.fetchone()
        if not task:
            return None

        cur.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_count,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) AS timeout_count,
                SUM(CASE WHEN status = 'canceled' THEN 1 ELSE 0 END) AS canceled_count,
                SUM(CASE WHEN status IN ('success', 'failed', 'timeout', 'canceled') THEN 1 ELSE 0 END) AS done_count
            FROM task_examples
            WHERE task_id = ?
            """,
            (task_id,),
        )
        summary_row = cur.fetchone()

        cur.execute(
            """
            SELECT *
            FROM task_examples
            WHERE task_id = ?
            ORDER BY seq ASC, id ASC
            """,
            (task_id,),
        )
        example_rows = cur.fetchall()

        cur.execute(
            """
            SELECT *
            FROM task_attempts
            WHERE task_id = ?
            ORDER BY id DESC
            LIMIT 200
            """,
            (task_id,),
        )
        attempt_rows = cur.fetchall()

    finally:
        conn.close()

    total = safe_int(summary_row["total"])
    done = safe_int(summary_row["done_count"])
    pending = safe_int(summary_row["pending_count"])
    running = safe_int(summary_row["running_count"])
    success = safe_int(summary_row["success_count"])
    failed = safe_int(summary_row["failed_count"])
    timeout = safe_int(summary_row["timeout_count"])
    canceled = safe_int(summary_row["canceled_count"])
    failed_total = failed + timeout
    progress_percent = 0.0

    if total > 0:
        progress_percent = round(done * 100.0 / total, 2)

    revision = task["revision"]
    revision_policy = task["revision_policy"] or "fixed"
    revision_display = str(revision or "")

    if revision_policy == "latest":
        if revision and str(revision).lower() != "latest":
            revision_display = "latest->%s" % revision
        else:
            revision_display = "latest"

    examples = []
    failures = []

    for row in example_rows:
        item = {
            "seq": row["seq"],
            "example_id": row["example_id"],
            "task_id": row["task_id"],
            "platform": row["platform"],
            "target_arg": row["target_arg"],
            "run_tcl_path": row["run_tcl_path"],
            "cmd": row["cmd"],
            "status": row["status"],
            "assigned_worker": row["assigned_worker"],
            "current_attempt_id": row["current_attempt_id"],
            "retry_count": row["retry_count"],
            "max_retry": row["max_retry"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "exit_code": row["exit_code"],
            "failed_step": row["failed_step"],
            "failed_reason": row["failed_reason"],
            "message": row["message"],
            "log_file": row["log_file"],
            "run_log_dir": row["run_log_dir"],
            "report_dir": row["report_dir"],
        }
        examples.append(item)

        if row["status"] in ("failed", "timeout"):
            failures.append(item)

    attempts = []
    for row in attempt_rows:
        attempts.append(row_to_dict(row))

    return {
        "ok": True,
        "generated_at": local_now(),
        "task": {
            "task_id": task["task_id"],
            "task_name": task["task_name"],
            "template_name": task["template_name"],
            "revision": revision,
            "revision_policy": revision_policy,
            "revision_display": revision_display,
            "resolved_zip_path": task["resolved_zip_path"],
            "suite": task["suite"],
            "target_worker": task["target_worker"],
            "status": task["status"],
            "priority": task["priority"],
            "max_retry": task["max_retry"],
            "max_time": task["max_time"],
            "work_root": task["work_root"],
            "target_dir": task["target_dir"],
            "split_mode": task["split_mode"],
            "flow_config": json_loads_dict(task["flow_config_json"]),
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
            "started_at": task["started_at"],
            "finished_at": task["finished_at"],
            "message": task["message"],
        },
        "summary": {
            "total": total,
            "done": done,
            "success": success,
            "failed": failed,
            "timeout": timeout,
            "failed_total": failed_total,
            "canceled": canceled,
            "running": running,
            "pending": pending,
            "progress": "%d/%d" % (done, total),
            "progress_percent": progress_percent,
        },
        "examples": examples,
        "failures": failures,
        "recent_attempts": attempts,
    }


def format_task_report_text(report):
    """Format one task report as readable text."""
    task = report["task"]
    summary = report["summary"]
    lines = []

    lines.append("PJTest Execution Report")
    lines.append("=" * 100)
    lines.append("Generated At : %s" % report["generated_at"])
    lines.append("Task ID      : %s" % task["task_id"])
    lines.append("Task Name    : %s" % (task["task_name"] or ""))
    lines.append("Template     : %s" % (task["template_name"] or ""))
    lines.append("Status       : %s" % task["status"])
    lines.append("Revision     : %s" % task["revision_display"])
    lines.append("Zip Path     : %s" % (task["resolved_zip_path"] or ""))
    lines.append("Work Root    : %s" % task["work_root"])
    lines.append("Target       : %s" % task["target_dir"])
    lines.append("Worker Limit : %s" % task["target_worker"])
    lines.append("Created At   : %s" % task["created_at"])
    lines.append("Started At   : %s" % (task["started_at"] or ""))
    lines.append("Finished At  : %s" % (task["finished_at"] or ""))
    lines.append("Message      : %s" % (task["message"] or ""))
    lines.append("")
    lines.append("Summary")
    lines.append("-" * 100)
    lines.append(
        "TOTAL=%d DONE=%d SUCCESS=%d FAILED=%d TIMEOUT=%d RUNNING=%d PENDING=%d PROGRESS=%s %.2f%%"
        % (
            summary["total"],
            summary["done"],
            summary["success"],
            summary["failed"],
            summary["timeout"],
            summary["running"],
            summary["pending"],
            summary["progress"],
            summary["progress_percent"],
        )
    )
    lines.append("")
    lines.append("Examples")
    lines.append("-" * 100)
    lines.append("%-6s %-14s %-10s %-10s %-7s %-6s %s" % (
        "SEQ",
        "EXAMPLE_ID",
        "STATUS",
        "WORKER",
        "RETRY",
        "EXIT",
        "TARGET_ARG",
    ))
    lines.append("-" * 100)

    for example in report["examples"]:
        retry = "%s/%s" % (
            example.get("retry_count") if example.get("retry_count") is not None else 0,
            example.get("max_retry") if example.get("max_retry") is not None else 0,
        )
        exit_code = example.get("exit_code")
        lines.append("%-6s %-14s %-10s %-10s %-7s %-6s %s" % (
            example.get("seq") or "",
            example.get("example_id") or "",
            example.get("status") or "",
            example.get("assigned_worker") or "-",
            retry,
            exit_code if exit_code is not None else "-",
            example.get("target_arg") or "",
        ))

    if report["failures"]:
        lines.append("")
        lines.append("Failures")
        lines.append("-" * 100)
        for example in report["failures"]:
            lines.append("[%s] seq=%s worker=%s exit=%s target=%s" % (
                example.get("status") or "",
                example.get("seq") or "",
                example.get("assigned_worker") or "-",
                example.get("exit_code") if example.get("exit_code") is not None else "-",
                example.get("target_arg") or "",
            ))
            if example.get("failed_reason"):
                lines.append("    failed_reason: %s" % example.get("failed_reason"))
            if example.get("message"):
                lines.append("    message      : %s" % example.get("message"))
            if example.get("log_file"):
                lines.append("    log_file     : %s" % example.get("log_file"))

    lines.append("")
    return "\n".join(str(line) for line in lines)


def sanitize_report_component(value, default_value="unknown"):
    """Return a filesystem-safe report path component."""
    text = str(value or "").strip()
    if not text:
        text = default_value

    text = text.replace("latest->", "latest_")
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    text = text.strip("._-")
    return text or default_value


def get_report_revision_dir(report):
    """Return the revision directory name used below REPORT_ROOT."""
    task = report["task"]
    revision = task.get("revision")
    revision_display = task.get("revision_display") or revision

    if revision and re.match(r"^\d+$", str(revision)):
        return "r%s" % revision

    return sanitize_report_component(revision_display, "unknown_revision")


def clean_report_text(value):
    """Sanitize report fields for one-line text files."""
    if value is None:
        return "-"

    text = str(value)
    text = text.replace("\t", " ")
    text = text.replace("\r", " ")
    text = text.replace("\n", " ")
    return text


def build_stat_summary_text(report):
    """Build stat_summary content for one task."""
    task = report["task"]
    summary = report["summary"]

    lines = [
        "generated_at     %s" % report["generated_at"],
        "task_id          %s" % task["task_id"],
        "template         %s" % (task["template_name"] or ""),
        "task_name        %s" % (task["task_name"] or ""),
        "revision         %s" % (task["revision_display"] or ""),
        "status           %s" % task["status"],
        "target_dir       %s" % (task["target_dir"] or ""),
        "total            %d" % summary["total"],
        "done             %d" % summary["done"],
        "success          %d" % summary["success"],
        "failed           %d" % summary["failed"],
        "timeout          %d" % summary["timeout"],
        "running          %d" % summary["running"],
        "pending          %d" % summary["pending"],
        "progress         %s %.2f%%" % (summary["progress"], summary["progress_percent"]),
        "created_at       %s" % (task["created_at"] or ""),
        "updated_at       %s" % (task["updated_at"] or ""),
        "started_at       %s" % (task["started_at"] or ""),
        "finished_at      %s" % (task["finished_at"] or ""),
        "message          %s" % clean_report_text(task["message"]),
        "",
    ]
    return "\n".join(lines)


def build_status_list_text(report, status):
    """Build target_arg list for examples matching status."""
    lines = []
    for example in report["examples"]:
        if example.get("status") == status:
            lines.append(example.get("target_arg") or example.get("run_tcl_path") or "")

    lines = [line for line in lines if line]
    return "\n".join(lines) + ("\n" if lines else "")


def get_task_report_paths(report):
    """Return final task report paths under reports/<revision>/<task_id>."""
    task = report["task"]
    revision_dir = get_report_revision_dir(report)
    task_dir = REPORT_ROOT / revision_dir / str(task["task_id"])
    return {
        "task_dir": task_dir,
        "stat_summary": task_dir / "stat_summary",
        "list_pass_to_run": task_dir / "list_pass_to_run",
        "list_fail_to_run": task_dir / "list_fail_to_run",
        "timeout_list": task_dir / "timeout_list",
    }


def write_task_share_reports(task_id):
    """Write final task report files to reports/<revision>/<task_id>."""
    report = get_task_report_data(task_id)
    if not report:
        return False

    paths = get_task_report_paths(report)
    files = {
        "stat_summary": build_stat_summary_text(report),
        "list_pass_to_run": build_status_list_text(report, "success"),
        "list_fail_to_run": build_status_list_text(report, "failed"),
        "timeout_list": build_status_list_text(report, "timeout"),
    }

    for name, content in files.items():
        atomic_write_text(paths[name], content)

    return True


def copy_task_reports_to_run_log(task_id, report=None):
    """
    只有当 task 同时满足“终态”和“统计强一致”时，才执行拷贝。
    作用层级：动作层（defensive gate），不影响状态机层。
    """
    # 1. 如果没有传入 report，从数据库重新读取（确保获取最新快照）
    if report is None:
        report = get_task_report_data(task_id)
        if not report:
            log_scheduler("WARN", "report data not found for task %s" % task_id, task_id=task_id)
            return False

    task_status = report["task"]["status"]
    summary = report["summary"]

    # 2. 终态检查（状态机层已经保证，但这里作为第一道门）
    if task_status not in TERMINAL_TASK_STATUSES:
        log_scheduler("DEBUG", "task not terminal, skip run_log copy: %s" % task_status, task_id=task_id)
        return False

    # 3. 强一致性校验（分布式防御层，关键新增）
    if not (summary["done"] == summary["total"] and
            summary["running"] == 0 and
            summary["pending"] == 0):
        log_scheduler(
            "WARN",
            "task terminal but stats inconsistent (done=%d total=%d running=%d pending=%d), skip copy" % (
                summary["done"], summary["total"], summary["running"], summary["pending"]
            ),
            task_id=task_id
        )
        return False

    # 4. 获取源目录和目标目录
    paths = get_task_report_paths(report)
    src_dir = paths["task_dir"]
    if not src_dir.exists():
        log_scheduler("WARN", "source report dir not found for run_log copy: %s" % src_dir, task_id=task_id)
        return False

    revision_dir = get_report_revision_dir(report)
    dst_dir = RUN_LOG_DIR / revision_dir / str(task_id)

    # 5. 如果目标已存在，跳过（幂等性）
    if dst_dir.exists():
        log_scheduler("INFO", "run_log already exists, skip: %s" % dst_dir, task_id=task_id)
        return True

    # 6. 执行拷贝
    try:
        RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_dir, dst_dir, symlinks=True)
        log_scheduler("INFO", "copied task reports to run_log: %s" % dst_dir, task_id=task_id)
        return True
    except Exception as exc:
        log_scheduler("ERROR", "failed to copy run_log: %s" % exc, task_id=task_id)
        return False


REPORT_WRITE_LOCK = threading.Lock()


def write_share_reports_for_task(task_id):
    """Write task-specific shared reports safely."""
    try:
        with REPORT_WRITE_LOCK:
            ok = write_task_share_reports(task_id)
        log_scheduler(
            "INFO",
            "share reports refreshed task=%s report_root=%s"
            % (task_id, REPORT_ROOT),
            task_id=task_id,
            also_stdout=False,
        )
        # 调用带防御校验的拷贝函数
        copy_task_reports_to_run_log(task_id)
        return ok
    except Exception as exc:
        log_exception("failed to write share reports for %s" % task_id, exc, task_id=task_id)
        return False


def get_tasks_summary_data(limit=200):
    """Build a global summary for all recent tasks."""
    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT
                t.task_id,
                t.task_name,
                t.template_name,
                t.revision,
                t.revision_policy,
                t.resolved_zip_path,
                t.status,
                t.priority,
                t.target_dir,
                t.target_worker,
                t.created_at,
                t.started_at,
                t.finished_at,
                t.message,
                COUNT(e.example_id) AS total,
                SUM(CASE WHEN e.status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN e.status = 'running' THEN 1 ELSE 0 END) AS running_count,
                SUM(CASE WHEN e.status = 'success' THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN e.status IN ('failed', 'timeout') THEN 1 ELSE 0 END) AS failed_count,
                SUM(CASE WHEN e.status IN ('success', 'failed', 'timeout', 'canceled') THEN 1 ELSE 0 END) AS done_count
            FROM tasks t
            LEFT JOIN task_examples e
                ON t.task_id = e.task_id
            GROUP BY t.task_id
            ORDER BY t.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    tasks = []
    for row in rows:
        total = safe_int(row["total"])
        done = safe_int(row["done_count"])
        progress_percent = 0.0
        if total > 0:
            progress_percent = round(done * 100.0 / total, 2)

        revision = row["revision"]
        revision_policy = row["revision_policy"] or "fixed"
        revision_display = str(revision or "")
        if revision_policy == "latest":
            if revision and str(revision).lower() != "latest":
                revision_display = "latest->%s" % revision
            else:
                revision_display = "latest"

        tasks.append({
            "task_id": row["task_id"],
            "task_name": row["task_name"],
            "template_name": row["template_name"],
            "revision": revision,
            "revision_policy": revision_policy,
            "revision_display": revision_display,
            "resolved_zip_path": row["resolved_zip_path"],
            "status": row["status"],
            "priority": row["priority"],
            "target_dir": row["target_dir"],
            "target_worker": row["target_worker"],
            "total": total,
            "done": done,
            "success": safe_int(row["success_count"]),
            "failed": safe_int(row["failed_count"]),
            "running": safe_int(row["running_count"]),
            "pending": safe_int(row["pending_count"]),
            "progress": "%d/%d" % (done, total),
            "progress_percent": progress_percent,
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "message": row["message"],
        })

    return {
        "ok": True,
        "generated_at": local_now(),
        "tasks": tasks,
    }


def format_tasks_summary_text(data):
    """Format global task summary text."""
    lines = []
    lines.append("PJTest Tasks Summary")
    lines.append("=" * 140)
    lines.append("Generated At : %s" % data["generated_at"])
    lines.append("")
    lines.append("%-16s %-10s %-12s %-10s %-8s %-6s %-7s %-7s %-7s %-8s %-10s %s" % (
        "TASK_ID",
        "TEMPLATE",
        "REV",
        "STATUS",
        "TOTAL",
        "DONE",
        "SUCCESS",
        "FAILED",
        "RUNNING",
        "PENDING",
        "PROGRESS",
        "TARGET",
    ))
    lines.append("-" * 140)

    for task in data["tasks"]:
        lines.append("%-16s %-10s %-12s %-10s %-8s %-6s %-7s %-7s %-7s %-8s %-10s %s" % (
            task.get("task_id") or "",
            task.get("template_name") or task.get("task_name") or "",
            task.get("revision_display") or "",
            task.get("status") or "",
            task.get("total"),
            task.get("done"),
            task.get("success"),
            task.get("failed"),
            task.get("running"),
            task.get("pending"),
            "%s %.1f%%" % (task.get("progress"), task.get("progress_percent")),
            task.get("target_dir") or "",
        ))

    lines.append("")
    return "\n".join(str(line) for line in lines)


def write_global_share_reports():
    """Global flat reports are intentionally disabled."""
    return True


def _reconcile_once_without_retry(worker_timeout):
    """Mark stale workers offline and refresh task aggregates without retry."""
    conn = get_conn()
    cur = conn.cursor()
    threshold = datetime.now() - timedelta(seconds=worker_timeout)
    threshold_text = threshold.strftime("%Y-%m-%d %H:%M:%S")

    stale_count = 0
    affected_examples = 0
    affected_task_ids = set()

    try:
        cur.execute("BEGIN IMMEDIATE")

        cur.execute(
            """
            SELECT worker_name, current_task_id, current_example_id, last_seen_at
            FROM workers
            WHERE status != 'offline'
              AND last_seen_at < ?
            """,
            (threshold_text,),
        )
        stale_workers = cur.fetchall()

        for worker in stale_workers:
            stale_count += 1
            worker_name = worker["worker_name"]

            if REQUEUE_STALE_RUNNING:
                cur.execute(
                    """
                    SELECT example_id, task_id, retry_count, max_retry, current_attempt_id
                    FROM task_examples
                    WHERE assigned_worker = ?
                      AND status = 'running'
                    ORDER BY id
                    """,
                    (worker_name,),
                )
                examples = cur.fetchall()

                for example in examples:
                    requeue_or_fail_stale_example(cur, worker_name, example)
                    affected_examples += 1
                    affected_task_ids.add(example["task_id"])
                    refresh_one_task_status(cur, example["task_id"])
            else:
                insert_event(
                    cur,
                    worker["current_task_id"],
                    worker["current_example_id"],
                    None,
                    worker_name,
                    "worker_stale",
                    "heartbeat timeout; running examples left unchanged",
                )

            cur.execute(
                """
                UPDATE workers
                SET status = 'offline',
                    current_task_id = NULL,
                    current_example_id = NULL,
                    current_attempt_id = NULL,
                    updated_at = ?,
                    message = ?
                WHERE worker_name = ?
                """,
                (
                    local_now(),
                    "worker heartbeat timeout. last_seen_at=%s" % worker["last_seen_at"],
                    worker_name,
                ),
            )

        changed_tasks = refresh_all_task_statuses(cur)
        conn.commit()

        for affected_task_id in sorted(affected_task_ids):
            write_share_reports_for_task(affected_task_id)
        if changed_tasks and not affected_task_ids:
            write_global_share_reports()

        return stale_count, affected_examples, changed_tasks

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def reconcile_once(worker_timeout):
    """Mark stale workers offline and refresh task aggregates with DB lock retry."""
    return run_db_write_with_retry(
        lambda: _reconcile_once_without_retry(worker_timeout),
        "reconcile",
    )


def reconcile_loop(stop_event, interval, worker_timeout):
    """Background scheduler reconciliation loop."""
    while not stop_event.is_set():
        try:
            stale_count, affected_examples, changed_tasks = reconcile_once(worker_timeout)
            if stale_count or affected_examples or changed_tasks:
                log_scheduler(
                    "WARN",
                    "reconcile stale_workers=%d affected_examples=%d changed_tasks=%d"
                    % (stale_count, affected_examples, changed_tasks),
                )
        except Exception as exc:
            log_exception("reconcile failed", exc)

        stop_event.wait(interval)


class SchedulerHandler(BaseHTTPRequestHandler):
    """HTTP API handler for scheduler."""

    server_version = "PJTestScheduler/1.0"

    def log_message(self, fmt, *args):
        message = fmt % args
        if "/api/health" in message and not SCHEDULER_DEBUG:
            return
        log_scheduler("HTTP", message)

    def send_json(self, data, status=200):
        """Send JSON response."""
        body = json_dumps(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_error(self, exc):
        log_exception("HTTP handler error path=%s" % getattr(self, "path", "-"), exc)
        self.send_json({"ok": False, "error": str(exc)}, status=500)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/api/health":
                self.send_json({"ok": True, "service": "scheduler", "time": local_now()})
                return

            if path == "/api/build/latest":
                self.handle_latest_build()
                return

            if path == "/api/tasks":
                self.handle_list_tasks(parsed)
                return

            if path == "/api/examples":
                self.handle_list_examples(parsed)
                return

            if path == "/api/workers":
                self.handle_list_workers()
                return

            self.send_json({"ok": False, "error": "not found"}, status=404)

        except Exception as exc:
            self.handle_error(exc)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/api/worker/register":
                self.handle_worker_register()
                return

            if path == "/api/worker/heartbeat":
                self.handle_worker_heartbeat()
                return

            if path == "/api/task/pull":
                self.handle_task_pull()
                return

            if path in ("/api/task/report", "/api/task/update"):
                self.handle_task_report()
                return

            self.send_json({"ok": False, "error": "not found"}, status=404)

        except Exception as exc:
            self.handle_error(exc)

    def handle_latest_build(self):
        """Return latest compiled build."""
        latest = find_latest_compiled_zip()
        if not latest:
            self.send_json(
                {
                    "ok": False,
                    "error": "no compiled GalaxCore zip found",
                    "zip_dirs": [str(path) for path in get_zip_dirs()],
                },
                status=404,
            )
            return

        self.send_json({"ok": True, "revision": latest[0], "zip_path": latest[1]})

    def handle_worker_register(self):
        """Register or refresh one worker."""
        data = read_json(self)
        worker_name = data.get("worker") or data.get("worker_name")
        hostname = data.get("hostname") or data.get("host")
        capabilities = data.get("capabilities")

        if not worker_name:
            self.send_json({"ok": False, "error": "missing worker"}, status=400)
            return

        def write_register():
            conn = get_conn()
            cur = conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                upsert_worker(
                    cur,
                    worker_name=worker_name,
                    hostname=hostname,
                    status="idle",
                    task_id=None,
                    example_id=None,
                    attempt_id=None,
                    capabilities=capabilities,
                    message="registered",
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        run_db_write_with_retry(write_register, "register worker=%s" % worker_name)

        log_scheduler(
            "INFO",
            "worker registered worker=%s hostname=%s" % (worker_name, hostname or "-"),
        )
        self.send_json({"ok": True, "worker": worker_name, "status": "registered"})

    def handle_worker_heartbeat(self):
        """Update worker heartbeat."""
        data = read_json(self)
        worker_name = data.get("worker") or data.get("worker_name")
        hostname = data.get("hostname") or data.get("host")
        status = data.get("status") or "idle"
        task_id = data.get("task_id") or data.get("current_task_id")
        example_id = data.get("example_id") or data.get("current_example_id")
        attempt_id = data.get("attempt_id") or data.get("current_attempt_id")
        message = data.get("message")

        if not worker_name:
            self.send_json({"ok": False, "error": "missing worker"}, status=400)
            return

        def write_heartbeat():
            conn = get_conn()
            cur = conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                upsert_worker(
                    cur,
                    worker_name=worker_name,
                    hostname=hostname,
                    status=status,
                    task_id=task_id,
                    example_id=example_id,
                    attempt_id=attempt_id,
                    message=message,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        run_db_write_with_retry(write_heartbeat, "heartbeat worker=%s" % worker_name)

        self.send_json(
            {
                "ok": True,
                "worker": worker_name,
                "status": status,
                "task_id": task_id,
                "example_id": example_id,
                "attempt_id": attempt_id,
            }
        )

    def handle_task_pull(self):
        """Assign one pending example to one worker."""
        data = read_json(self)
        worker_name = data.get("worker") or data.get("worker_name")
        hostname = data.get("hostname") or data.get("host")
        capabilities = data.get("capabilities")

        if not worker_name:
            self.send_json({"ok": False, "error": "missing worker"}, status=400)
            return

        task, message = claim_example_for_worker(worker_name, hostname, capabilities)
        if task is None and message and SCHEDULER_DEBUG:
            log_scheduler(
                "DEBUG",
                "pull skipped worker=%s message=%s" % (worker_name, message),
            )
        self.send_json({"ok": True, "task": task, "message": message})

    def handle_task_report(self):
        """Receive one worker execution result."""
        data = read_json(self)
        response, status = finish_attempt(data)
        self.send_json(response, status=status)

    def handle_list_tasks(self, parsed):
        """List parent tasks as JSON."""
        query = parse_qs(parsed.query)
        status = query.get("status", [None])[0]
        limit = int(query.get("limit", [50])[0])
        limit = max(1, min(limit, 200))

        conn = get_conn()
        cur = conn.cursor()

        sql = """
            SELECT
                t.task_id,
                t.task_name,
                t.template_name,
                t.revision,
                t.revision_policy,
                t.resolved_zip_path,
                t.suite,
                t.target_worker,
                t.status,
                t.priority,
                t.work_root,
                t.target_dir,
                t.total_examples,
                t.created_at,
                t.started_at,
                t.finished_at,
                t.message,
                COUNT(e.example_id) AS real_total,
                SUM(CASE WHEN e.status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN e.status = 'running' THEN 1 ELSE 0 END) AS running_count,
                SUM(CASE WHEN e.status = 'success' THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN e.status IN ('failed', 'timeout') THEN 1 ELSE 0 END) AS failed_count,
                SUM(CASE WHEN e.status IN ('success', 'failed', 'timeout', 'canceled') THEN 1 ELSE 0 END) AS done_count
            FROM tasks t
            LEFT JOIN task_examples e
                ON t.task_id = e.task_id
        """
        params = []

        if status:
            sql += " WHERE t.status = ?"
            params.append(status)

        sql += " GROUP BY t.task_id ORDER BY t.id DESC LIMIT ?"
        params.append(limit)

        cur.execute(sql, params)
        rows = [row_to_dict(row) for row in cur.fetchall()]
        conn.close()

        self.send_json({"ok": True, "tasks": rows})

    def handle_list_examples(self, parsed):
        """List examples of a task as JSON."""
        query = parse_qs(parsed.query)
        task_id = query.get("task_id", [None])[0]
        status = query.get("status", [None])[0]
        limit = int(query.get("limit", [500])[0])
        limit = max(1, min(limit, 2000))

        conn = get_conn()
        cur = conn.cursor()

        sql = """
            SELECT *
            FROM task_examples
        """
        conditions = []
        params = []

        if task_id:
            conditions.append("task_id = ?")
            params.append(task_id)
        if status:
            conditions.append("status = ?")
            params.append(status)

        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        sql += " ORDER BY task_id, seq LIMIT ?"
        params.append(limit)

        cur.execute(sql, params)
        rows = [row_to_dict(row) for row in cur.fetchall()]
        conn.close()

        self.send_json({"ok": True, "examples": rows})

    def handle_list_workers(self):
        """List workers as JSON."""
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT worker_name, hostname, status, current_task_id,
                   current_example_id, current_attempt_id, capabilities_json,
                   last_seen_at, started_at, updated_at, message
            FROM workers
            ORDER BY worker_name ASC
            """
        )
        rows = [row_to_dict(row) for row in cur.fetchall()]
        conn.close()

        self.send_json({"ok": True, "workers": rows})


def build_parser():
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description="PJTest central HTTP scheduler")
    parser.add_argument("--host", default="0.0.0.0", help="listen host")
    parser.add_argument("--port", type=int, default=9000, help="listen port")
    parser.add_argument(
        "--reconcile-interval",
        type=int,
        default=10,
        help="background reconciliation interval seconds",
    )
    parser.add_argument(
        "--worker-timeout",
        type=int,
        default=300,
        help="mark worker offline after this many heartbeat seconds",
    )
    return parser


def main():
    """Program entry."""
    args = build_parser().parse_args()

    init_database_pragmas()
    server = ThreadingHTTPServer((args.host, args.port), SchedulerHandler)
    stop_event = threading.Event()
    reconcile_thread = threading.Thread(
        target=reconcile_loop,
        args=(stop_event, args.reconcile_interval, args.worker_timeout),
    )
    reconcile_thread.daemon = True
    reconcile_thread.start()

    log_scheduler("INFO", "scheduler started host=%s port=%s" % (args.host, args.port))
    log_scheduler("INFO", "database=%s" % DB_PATH)
    log_scheduler("INFO", "latest_zip_dirs=%s" % ", ".join(str(path) for path in get_zip_dirs()))
    log_scheduler("INFO", "report_root=%s" % REPORT_ROOT)
    log_scheduler("INFO", "scheduler_log_root=%s" % SCHEDULER_LOG_ROOT)
    log_scheduler("INFO", "run_log_dir=%s" % RUN_LOG_DIR)
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nScheduler stopped.")
        log_scheduler("INFO", "scheduler stopped by KeyboardInterrupt")
    finally:
        stop_event.set()
        server.server_close()
        log_scheduler("INFO", "scheduler server closed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())