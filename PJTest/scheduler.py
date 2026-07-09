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
    ./scheduler.py
    ./scheduler.py --check   # check configuration and exit
"""

import argparse
import json
import os
import queue
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

from config import (
    CONFIG_FILE,
    get_bool,
    get_float,
    get_int,
    get_path,
    get_path_list,
    get_value,
)


DB_PATH = get_path(
    "paths",
    "database",
    env_name="PJTEST_DB_PATH",
)

DEFAULT_ZIP_DIRS = get_path_list("paths", "zip_dirs")

ZIP_RE = re.compile(r"^Galax[Cc]ore_(\d+)\.zip$")
TERMINAL_TASK_STATUSES = set(["success", "failed", "canceled"])
TERMINAL_EXAMPLE_STATUSES = set(["success", "failed", "timeout", "canceled"])

REPORT_ROOT = get_path(
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
REPORT_MAINTENANCE_INTERVAL_SEC = get_int(
    "reports",
    "maintenance_interval_sec",
    env_name="PJTEST_REPORT_MAINTENANCE_INTERVAL_SEC",
    minimum=60,
)
REPORT_OLD_DIR_NAME = get_value("reports", "old_dir_name")
REPORT_SUMMARY_DIR_NAME = get_value("reports", "summary_dir_name")

# Shared log directory for completed task report copies.
RUN_LOG_DIR = get_path(
    "paths",
    "run_log_dir",
    env_name="PJTEST_RUN_LOG_DIR",
)

# Scheduler-generated task reports are organized as:
#     REPORT_ROOT / <revision> / <template>_<task_id> /
# Reports older than the retention window are moved below REPORT_ROOT / Old.
# REPORT_ROOT / Summary keeps one latest stat_summary per template.

SCHEDULER_LOG_ROOT = get_path(
    "paths",
    "scheduler_log_root",
    env_name="PJTEST_SCHEDULER_LOG_ROOT",
)

SCHEDULER_DEBUG = get_bool(
    "scheduler",
    "debug",
    env_name="PJTEST_SCHEDULER_DEBUG",
)

LOG_LOCK = threading.Lock()
DB_WRITE_LOCK = threading.RLock()
DB_LOCK_RETRIES = get_int(
    "database",
    "lock_retries",
    env_name="PJTEST_DB_LOCK_RETRIES",
    minimum=1,
)
DB_LOCK_RETRY_BASE_SEC = get_float(
    "database",
    "lock_retry_base_sec",
    env_name="PJTEST_DB_LOCK_RETRY_BASE_SEC",
    minimum=0.0,
)
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
REPORT_RECENT_ATTEMPT_LIMIT = get_int(
    "reports",
    "recent_attempt_limit",
    minimum=1,
)
TASK_API_DEFAULT_LIMIT = get_int(
    "http",
    "task_api_default_limit",
    minimum=1,
)
TASK_API_MAX_LIMIT = get_int(
    "http",
    "task_api_max_limit",
    minimum=1,
)
EXAMPLE_API_DEFAULT_LIMIT = get_int(
    "http",
    "example_api_default_limit",
    minimum=1,
)
EXAMPLE_API_MAX_LIMIT = get_int(
    "http",
    "example_api_max_limit",
    minimum=1,
)
REQUEUE_STALE_RUNNING = get_bool(
    "scheduler",
    "requeue_stale_running",
    env_name="PJTEST_REQUEUE_STALE_RUNNING",
)

DEFAULT_SCHEDULER_HOST = get_value("scheduler", "host")
DEFAULT_SCHEDULER_PORT = get_int("scheduler", "port", minimum=1)
DEFAULT_RECONCILE_INTERVAL = get_int(
    "scheduler",
    "reconcile_interval_sec",
    minimum=1,
)
DEFAULT_WORKER_TIMEOUT = get_int(
    "scheduler",
    "worker_timeout_sec",
    minimum=1,
)

# ======================== Configuration check ========================
def check_configuration():
    """Check and display scheduler configuration and environment."""
    print("PJTest Scheduler Configuration Check")
    print("=" * 60)
    print("Config File  : %s" % CONFIG_FILE)

    # 1. Database
    db_path = DB_PATH
    print(f"Database Path: {db_path}")
    if db_path.exists():
        print("  [OK] Database file exists.")
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            conn.close()
            print("  [OK] Database is accessible.")
        except Exception as e:
            print(f"  [ERROR] Cannot connect to database: {e}")
    else:
        print("  [WARN] Database file does not exist. It will be created on first use.")
        parent = db_path.parent
        if parent.exists():
            if os.access(parent, os.W_OK):
                print(f"  [OK] Parent directory {parent} is writable.")
            else:
                print(f"  [ERROR] Parent directory {parent} is NOT writable.")
        else:
            print(f"  [WARN] Parent directory {parent} does not exist.")

    # 2. REPORT_ROOT
    print(f"\nREPORT_ROOT: {REPORT_ROOT}")
    if REPORT_ROOT.exists():
        print("  [OK] Report root exists.")
    else:
        print("  [WARN] Report root does not exist. It will be created as needed.")
        parent = REPORT_ROOT.parent
        if parent.exists() and os.access(parent, os.W_OK):
            print(f"  [OK] Parent directory {parent} is writable.")
        else:
            print(f"  [ERROR] Parent directory {parent} is NOT writable or does not exist.")

    print(f"  Retention days: {REPORT_RETENTION_DAYS}")
    print(f"  Old directory : {REPORT_ROOT / REPORT_OLD_DIR_NAME}")
    print(f"  Summary dir   : {REPORT_ROOT / REPORT_SUMMARY_DIR_NAME}")

    # 3. RUN_LOG_DIR
    print(f"\nRUN_LOG_DIR: {RUN_LOG_DIR}")
    if RUN_LOG_DIR.exists():
        print("  [OK] Run log directory exists.")
        if os.access(RUN_LOG_DIR, os.W_OK):
            print("  [OK] Run log directory is writable.")
        else:
            print("  [ERROR] Run log directory is NOT writable.")
    else:
        print("  [WARN] Run log directory does not exist. It will be created as needed.")
        parent = RUN_LOG_DIR.parent
        if parent.exists() and os.access(parent, os.W_OK):
            print(f"  [OK] Parent directory {parent} is writable.")
        else:
            print(f"  [ERROR] Parent directory {parent} is NOT writable or does not exist.")

    # 4. SCHEDULER_LOG_ROOT
    print(f"\nSCHEDULER_LOG_ROOT: {SCHEDULER_LOG_ROOT}")
    if SCHEDULER_LOG_ROOT.exists():
        print("  [OK] Scheduler log root exists.")
        if os.access(SCHEDULER_LOG_ROOT, os.W_OK):
            print("  [OK] Scheduler log root is writable.")
        else:
            print("  [ERROR] Scheduler log root is NOT writable.")
    else:
        print("  [WARN] Scheduler log root does not exist. It will be created as needed.")
        parent = SCHEDULER_LOG_ROOT.parent
        if parent.exists() and os.access(parent, os.W_OK):
            print(f"  [OK] Parent directory {parent} is writable.")
        else:
            print(f"  [ERROR] Parent directory {parent} is NOT writable or does not exist.")

    # 5. Zip directories
    print("\nZip Directories (GALAXCORE_ZIP_DIR):")
    zip_dirs = get_zip_dirs()
    for zd in zip_dirs:
        if zd.exists():
            print(f"  [OK] {zd}")
        else:
            print(f"  [WARN] {zd} does not exist.")

    # 6. Find latest zip
    print("\nLooking for latest GalaxCore zip...")
    latest = find_latest_compiled_zip()
    if latest:
        print(f"  [OK] Latest revision: {latest[0]} at {latest[1]}")
    else:
        print("  [ERROR] No GalaxCore zip found in configured directories.")

    # 7. Environment variables (optional)
    print("\nEnvironment variables (overrides):")
    env_vars = [
        "PJTEST_CONFIG_FILE",
        "PJTEST_DB_PATH",
        "PJTEST_SHARE_REPORT_DIR",
        "PJTEST_REPORT_RETENTION_DAYS",
        "PJTEST_REPORT_MAINTENANCE_INTERVAL_SEC",
        "PJTEST_RUN_LOG_DIR",
        "PJTEST_SCHEDULER_LOG_ROOT",
        "PJTEST_SCHEDULER_DEBUG",
        "PJTEST_DB_LOCK_RETRIES",
        "PJTEST_DB_LOCK_RETRY_BASE_SEC",
        "PJTEST_SQLITE_BUSY_TIMEOUT_MS",
        "PJTEST_REQUEUE_STALE_RUNNING",
        "GALAXCORE_ZIP_DIR",
    ]
    for var in env_vars:
        val = os.environ.get(var)
        if val is not None:
            print(f"  {var}={val}")

    print("\nCheck complete.")
# ====================== End configuration check ======================


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """Threaded HTTP server for concurrent worker requests."""

    daemon_threads = True


def get_conn():
    """Create a SQLite connection with runtime pragmas."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH), timeout=DB_CONNECT_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=%d" % SQLITE_BUSY_TIMEOUT_MS)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_database_pragmas():
    """Initialize SQLite database mode once at scheduler startup."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH), timeout=DB_CONNECT_TIMEOUT_SEC)
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


def compute_revision_scan_result(cur, task_id, existing_result=None):
    """Compute structured first-bad metadata from terminal scan examples."""
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

def refresh_one_task_status(cur, task_id):
    """Refresh task status, including revision-scan result semantics."""
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
        return False

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
        new_status, message = "canceled", "Task canceled. canceled=%d done=%d total=%d" % (canceled, done, total)
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
        return True
    return False

def refresh_all_task_statuses(cur):
    """Refresh all non-terminal parent tasks and return changed task ids."""
    cur.execute(
        """
        SELECT task_id
        FROM tasks
        WHERE status NOT IN ('success', 'failed', 'canceled')
        ORDER BY id
        """
    )
    rows = cur.fetchall()

    changed_task_ids = []
    for row in rows:
        if refresh_one_task_status(cur, row["task_id"]):
            changed_task_ids.append(row["task_id"])

    return changed_task_ids


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


def resolve_example_build(cur, row):
    """Resolve the build snapshot for one execution item.

    A per-example revision must never fall back to the task-level ZIP because
    that ZIP may belong to a different revision.
    """
    example_revision = row["example_revision"]
    example_zip = row["example_resolved_zip_path"]
    if example_revision is not None and str(example_revision).strip() != "":
        revision = str(example_revision)
        if example_zip and Path(example_zip).is_file():
            return revision, example_zip, None
        fixed_zip = find_zip_for_revision(revision)
        if not fixed_zip:
            return None, None, "compiled GalaxCore zip not found for example revision %s" % revision
        cur.execute(
            """
            UPDATE task_examples SET resolved_zip_path = ?, updated_at = ?
            WHERE example_id = ?
            """,
            (fixed_zip, local_now(), row["example_id"]),
        )
        return revision, fixed_zip, None
    return resolve_task_build(cur, row)


def resolve_existing_assignment_build(cur, row):
    """Recover exactly the build used by an existing running attempt."""
    attempt_revision = row["attempt_revision"]
    attempt_zip = row["attempt_zip_path"]
    if attempt_revision:
        revision = str(attempt_revision)
        if attempt_zip and Path(attempt_zip).is_file():
            return revision, attempt_zip, None
        fixed_zip = find_zip_for_revision(revision)
        if not fixed_zip:
            return None, None, "compiled GalaxCore zip not found for running attempt revision %s" % revision
        cur.execute(
            "UPDATE task_attempts SET zip_path = ? WHERE attempt_id = ?",
            (fixed_zip, row["existing_attempt_id"]),
        )
        return revision, fixed_zip, None
    return resolve_example_build(cur, row)

def select_existing_assignment(cur, worker_name):
    """Return an unfinished assignment already owned by this worker.

    A pull response can be lost after SQLite commits the assignment. Returning
    the same attempt on the next pull makes task claiming idempotent and
    prevents one worker from accumulating multiple running examples.
    """
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
            e.revision AS example_revision,
            e.resolved_zip_path AS example_resolved_zip_path,

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
            t.flow_config_json AS flow_config_json,

            a.attempt_id AS existing_attempt_id,
            a.attempt_no AS existing_attempt_no,
            a.revision AS attempt_revision,
            a.zip_path AS attempt_zip_path,

            CASE
                WHEN w.current_task_id = e.task_id
                 AND w.current_example_id = e.example_id
                 AND w.current_attempt_id = e.current_attempt_id
                THEN 0
                ELSE 1
            END AS assignment_rank
        FROM task_examples e
        JOIN tasks t
            ON t.task_id = e.task_id
        JOIN task_attempts a
            ON a.attempt_id = e.current_attempt_id
           AND a.example_id = e.example_id
        LEFT JOIN workers w
            ON w.worker_name = ?
        WHERE e.status = 'running'
          AND e.assigned_worker = ?
          AND a.status = 'running'
          AND t.status IN ('pending', 'running', 'canceling')
        ORDER BY assignment_rank ASC, e.started_at ASC, e.id ASC
        LIMIT 1
        """,
        (worker_name, worker_name),
    )
    return cur.fetchone()


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
            e.revision AS example_revision,
            e.resolved_zip_path AS example_resolved_zip_path,

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
        "revision_policy": "fixed" if row["example_revision"] else (row["revision_policy"] or "fixed"),
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
    """Claim one pending example or recover the existing assignment."""
    conn = get_conn()
    cur = conn.cursor()
    now = local_now()

    try:
        cur.execute("BEGIN IMMEDIATE")

        existing = select_existing_assignment(cur, worker_name)
        if existing:
            attempt_id = existing["existing_attempt_id"]
            attempt_no = int(existing["existing_attempt_no"] or 1)
            revision, zip_path, build_error = resolve_existing_assignment_build(cur, existing)
            if build_error:
                conn.rollback()
                return None, build_error

            upsert_worker(
                cur,
                worker_name=worker_name,
                hostname=hostname,
                status="running",
                task_id=existing["task_id"],
                example_id=existing["example_id"],
                attempt_id=attempt_id,
                capabilities=capabilities,
                message="recovered existing example %s" % existing["example_id"],
            )

            insert_event(
                cur,
                existing["task_id"],
                existing["example_id"],
                attempt_id,
                worker_name,
                "example_claim_recovered",
                "Returned the existing running attempt after a repeated pull",
            )

            payload = build_example_payload(
                existing,
                revision,
                zip_path,
                attempt_id,
                attempt_no,
            )
            conn.commit()
            enqueue_share_report(existing["task_id"])

            log_scheduler(
                "WARN",
                "pull recovered worker=%s task=%s example=%s attempt=%s target=%s"
                % (
                    worker_name,
                    existing["task_id"],
                    existing["example_id"],
                    attempt_id,
                    payload["target_arg"],
                ),
                task_id=existing["task_id"],
            )
            return payload, "recovered existing assignment"

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

        revision, zip_path, error = resolve_example_build(cur, row)
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
        enqueue_share_report(row["task_id"])
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

    if SCHEDULER_DEBUG:
        log_scheduler(
            "DEBUG",
            "report received worker=%s task=%s example=%s attempt=%s status=%s exit=%s raw_exit=%s infra=%s"
            % (
                worker_name or "-",
                task_id or "-",
                example_id or "-",
                attempt_id or "-",
                status or "-",
                data.get("exit_code"),
                data.get("raw_exit_code"),
                data.get("infra_reason") or "-",
            ),
            task_id=task_id,
            also_stdout=False,
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
    raw_exit_code = data.get("raw_exit_code")
    timed_out = 1 if data.get("timed_out") else 0
    message = data.get("message") or data.get("error_message") or ""
    log_file = data.get("log_file")
    log_tail = data.get("log_tail") or ""
    run_log_dir = data.get("run_log_dir")
    report_dir = data.get("report_dir")
    infra_reason = data.get("infra_reason") or ""
    infra_error = bool(data.get("infra_error"))

    if not infra_error and status == "failed":
        tail_text = str(log_tail or "")
        if raw_exit_code == 75 or exit_code == 75:
            if "Current slot already has a run.sh task running" in tail_text or "infra_slot_busy" in str(message):
                infra_error = True
                infra_reason = infra_reason or "slot_busy"

    if infra_error and not infra_reason:
        infra_reason = "infra_error"

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

        if attempt["task_id"] != task_id or attempt["example_id"] != example_id:
            conn.rollback()
            return {
                "ok": False,
                "error": "attempt assignment mismatch",
                "attempt_task_id": attempt["task_id"],
                "attempt_example_id": attempt["example_id"],
                "request_task_id": task_id,
                "request_example_id": example_id,
            }, 400

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

        if example["task_id"] != task_id:
            conn.rollback()
            return {
                "ok": False,
                "error": "example task mismatch",
                "example_task_id": example["task_id"],
                "request_task_id": task_id,
                "example_id": example_id,
            }, 400

        retry_count = int(example["retry_count"] or 0)
        max_retry = int(example["max_retry"] or 0)

        cur.execute(
            """
            UPDATE task_attempts
            SET status = ?,
                finished_at = ?,
                exit_code = ?,
                raw_exit_code = ?,
                timed_out = ?,
                log_file = ?,
                log_tail = ?,
                run_log_dir = ?,
                report_dir = ?,
                infra_reason = ?,
                message = ?
            WHERE attempt_id = ?
            """,
            (
                "infra_failed" if infra_error else status,
                now,
                exit_code,
                raw_exit_code,
                timed_out,
                log_file,
                log_tail,
                run_log_dir,
                report_dir,
                infra_reason,
                message,
                attempt_id,
            ),
        )

        final_example_status = status
        requeued = False

        if infra_error:
            final_example_status = "pending"
            requeued = True

            cur.execute(
                """
                UPDATE task_examples
                SET status = 'pending',
                    assigned_worker = NULL,
                    current_attempt_id = NULL,
                    updated_at = ?,
                    started_at = NULL,
                    finished_at = NULL,
                    exit_code = ?,
                    raw_exit_code = ?,
                    failed_step = 'worker_infra',
                    failed_reason = ?,
                    infra_reason = ?,
                    message = ?,
                    log_file = ?,
                    log_tail = ?,
                    run_log_dir = ?,
                    report_dir = ?
                WHERE example_id = ?
                """,
                (
                    now,
                    exit_code,
                    raw_exit_code,
                    infra_reason,
                    infra_reason,
                    "infra failure requeued without consuming retry: %s" % (message or infra_reason),
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
                "example_infra_requeued",
                "Example requeued after infra failure: %s" % (infra_reason or message or "infra_error"),
            )

        elif status in ("failed", "timeout") and retry_count < max_retry:
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
                    raw_exit_code = ?,
                    failed_reason = ?,
                    infra_reason = NULL,
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
                    raw_exit_code,
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
                    raw_exit_code = ?,
                    failed_reason = ?,
                    infra_reason = NULL,
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
                    raw_exit_code,
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
        # Queue a non-blocking report refresh after the transaction commits.
        enqueue_share_report(task_id)

        slot_busy_noise = infra_reason == "slot_busy"

        if not slot_busy_noise or SCHEDULER_DEBUG:
            log_scheduler(
                "INFO",
                "report handled worker=%s task=%s example=%s attempt=%s status=%s final=%s requeued=%s exit=%s raw_exit=%s infra=%s"
                % (
                    worker_name,
                    task_id,
                    example_id,
                    attempt_id,
                    status,
                    final_example_status,
                    requeued,
                    exit_code,
                    raw_exit_code,
                    infra_reason or "-",
                ),
                task_id=task_id,
                also_stdout=not slot_busy_noise,
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


def requeue_or_fail_stale_example(
    cur,
    worker_name,
    example,
    reason,
    detail,
):
    """Requeue or fail one running example whose assignment was lost."""
    now = local_now()
    retry_count = int(example["retry_count"] or 0)
    max_retry = int(example["max_retry"] or 0)
    attempt_id = example["current_attempt_id"]
    attempt_status = "assignment_lost" if reason == "assignment_superseded" else "worker_lost"

    if attempt_id:
        cur.execute(
            """
            UPDATE task_attempts
            SET status = ?,
                finished_at = ?,
                message = ?
            WHERE attempt_id = ?
              AND status = 'running'
            """,
            (
                attempt_status,
                now,
                detail,
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
                exit_code = NULL,
                failed_step = 'scheduler_reconcile',
                failed_reason = ?,
                message = ?
            WHERE example_id = ?
              AND status = 'running'
            """,
            (
                new_retry_count,
                now,
                reason,
                "%s; requeued retry=%d/%d" % (
                    detail,
                    new_retry_count,
                    max_retry,
                ),
                example["example_id"],
            ),
        )
        event_name = "example_requeued"
        message = "%s. retry=%d/%d" % (detail, new_retry_count, max_retry)
    else:
        cur.execute(
            """
            UPDATE task_examples
            SET status = 'failed',
                assigned_worker = ?,
                updated_at = ?,
                finished_at = ?,
                exit_code = NULL,
                failed_step = 'scheduler_reconcile',
                failed_reason = ?,
                message = ?
            WHERE example_id = ?
              AND status = 'running'
            """,
            (
                worker_name,
                now,
                now,
                reason,
                "%s; retry limit reached" % detail,
                example["example_id"],
            ),
        )
        event_name = "example_failed"
        message = "%s. retry limit reached." % detail

    if cur.rowcount != 1:
        return False

    insert_event(
        cur,
        example["task_id"],
        example["example_id"],
        attempt_id,
        worker_name,
        event_name,
        message,
    )
    return True

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
            LIMIT ?
            """,
            (task_id, REPORT_RECENT_ATTEMPT_LIMIT),
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

    if task["split_mode"] == "revision_scan" or revision_policy == "per_example":
        revision_display = str(revision or "per-example")
    elif revision_policy == "latest":
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
            "revision": row["revision"],
            "resolved_zip_path": row["resolved_zip_path"],
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
            "result": json_loads_dict(task["result_json"]),
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
    lines.append("%-6s %-14s %-10s %-10s %-10s %-7s %-6s %s" % (
        "SEQ",
        "EXAMPLE_ID",
        "REV",
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
        lines.append("%-6s %-14s %-10s %-10s %-10s %-7s %-6s %s" % (
            example.get("seq") or "",
            example.get("example_id") or "",
            example.get("revision") or "-",
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


def get_task_field(task, key, default=None):
    """Return one task field from a dict or sqlite row."""
    try:
        value = task[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def get_task_revision_dir(task):
    """Return the revision directory name for one task mapping."""
    if get_task_field(task, "split_mode") == "revision_scan":
        return "scan_%s" % sanitize_report_component(
            get_task_field(task, "revision"), "revision_scan"
        )
    revision = get_task_field(task, "revision")
    if revision and re.match(r"^\d+$", str(revision)):
        return "r%s" % revision
    revision_display = get_task_field(task, "revision_display")
    if not revision_display:
        policy = get_task_field(task, "revision_policy", "fixed")
        revision_display = "latest" if policy == "latest" else revision
    return sanitize_report_component(revision_display, "unknown_revision")

def get_report_revision_dir(report):
    """Return the revision directory name used below REPORT_ROOT."""
    return get_task_revision_dir(report["task"])


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


def get_report_cutoff_date():
    """Return the oldest calendar date kept in the active report tree."""
    return datetime.now().date() - timedelta(days=REPORT_RETENTION_DAYS - 1)


def should_archive_task_report(task):
    """Return True when a terminal task is older than the retention window."""
    status = get_task_field(task, "status")
    if status not in TERMINAL_TASK_STATUSES:
        return False

    created_at = get_task_field(task, "created_at")
    try:
        created_time = parse_local_time(created_at)
    except Exception:
        return False

    return bool(created_time and created_time.date() < get_report_cutoff_date())


def get_task_report_directory(report_root, task):
    """Return the active or archived report directory for one task."""
    revision_dir = get_task_revision_dir(task)
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
    revision_dir = get_task_revision_dir(task)
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


def maintain_report_directories(report_root=REPORT_ROOT):
    """Normalize task directories and archive terminal tasks older than ten days."""
    report_root = Path(report_root)
    report_root.mkdir(parents=True, exist_ok=True)
    (report_root / REPORT_OLD_DIR_NAME).mkdir(parents=True, exist_ok=True)
    (report_root / REPORT_SUMMARY_DIR_NAME).mkdir(parents=True, exist_ok=True)

    conn = get_conn()
    cur = conn.cursor()
    moved = 0
    try:
        cur.execute(
            """
            SELECT task_id, task_name, template_name, revision,
                   revision_policy, split_mode, status, created_at
            FROM tasks
            ORDER BY id ASC
            """
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    for row in rows:
        task = row_to_dict(row)
        before = []
        revision_dir = get_task_revision_dir(task)
        task_id = str(task["task_id"])
        task_dir_name = get_task_report_dir_name(task)
        before.extend([
            report_root / revision_dir / task_id,
            report_root / REPORT_OLD_DIR_NAME / revision_dir / task_id,
            report_root / revision_dir / task_dir_name,
            report_root / REPORT_OLD_DIR_NAME / revision_dir / task_dir_name,
        ])
        existing_before = set(str(path) for path in before if path.exists())
        target = prepare_task_report_directory(report_root, task)
        if existing_before and str(target) not in existing_before:
            moved += 1

    cleanup_empty_report_directories(report_root)
    return moved


def get_task_elapsed(task, generated_at=None):
    """Return task elapsed seconds and a readable duration string."""
    started_at = get_task_field(task, "started_at")
    if not started_at:
        return None, "-"

    try:
        start_time = parse_local_time(started_at)
        end_text = get_task_field(task, "finished_at") or generated_at
        end_time = parse_local_time(end_text) if end_text else datetime.now()
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


def write_latest_template_summary(report, report_root=REPORT_ROOT):
    """Overwrite the latest stat_summary file for one template."""
    task = report["task"]
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
    atomic_write_text(summary_path, build_stat_summary_text(report))
    return True


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
    elapsed_seconds, elapsed_time = get_task_elapsed(
        task,
        generated_at=report.get("generated_at"),
    )

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
        "elapsed_seconds  %s" % (elapsed_seconds if elapsed_seconds is not None else "-"),
        "elapsed_time     %s" % elapsed_time,
        "message          %s" % clean_report_text(task["message"]),
    ]
    if task.get("split_mode") == "revision_scan":
        result = task.get("result") or {}
        lines.extend([
            "scan_status      %s" % clean_report_text(result.get("scan_status")),
            "last_good        %s" % clean_report_text(result.get("last_good_revision")),
            "first_bad        %s" % clean_report_text(result.get("first_bad_revision")),
            "non_monotonic    %s" % clean_report_text(result.get("non_monotonic_revisions")),
        ])
    lines.append("")
    return "\n".join(lines)


def build_status_list_text(report, status):
    """Build status list, preserving revision identity for vertical scans."""
    lines = []
    is_scan = report["task"].get("split_mode") == "revision_scan"
    for example in report["examples"]:
        if example.get("status") != status:
            continue
        target = example.get("target_arg") or example.get("run_tcl_path") or ""
        if target:
            lines.append("r%s\t%s" % (example.get("revision"), target) if is_scan else target)
    return "\n".join(lines) + ("\n" if lines else "")

def get_task_report_paths(report):
    """Return final paths under active Reports or Reports/Old."""
    task_dir = get_task_report_directory(REPORT_ROOT, report["task"])
    return {
        "task_dir": task_dir,
        "stat_summary": task_dir / "stat_summary",
        "list_pass_to_run": task_dir / "list_pass_to_run",
        "list_fail_to_run": task_dir / "list_fail_to_run",
        "timeout_list": task_dir / "timeout_list",
    }


def write_task_share_reports(task_id):
    """Write task files and overwrite the latest template summary."""
    report = get_task_report_data(task_id)
    if not report:
        return False

    prepare_task_report_directory(REPORT_ROOT, report["task"])
    paths = get_task_report_paths(report)
    files = {
        "stat_summary": build_stat_summary_text(report),
        "list_pass_to_run": build_status_list_text(report, "success"),
        "list_fail_to_run": build_status_list_text(report, "failed"),
        "timeout_list": build_status_list_text(report, "timeout"),
    }

    for name, content in files.items():
        atomic_write_text(paths[name], content)

    write_latest_template_summary(report, REPORT_ROOT)
    return True


def copy_task_reports_to_run_log(task_id, report=None):
    """
    只有当 task 同时满足“终态”和“统计强一致”时，才执行拷贝。
    作用层级：动作层（defensive gate），不影响状态机层。
    """
    # Reload the latest database snapshot when no report was supplied.
    if report is None:
        report = get_task_report_data(task_id)
        if not report:
            log_scheduler("WARN", "report data not found for task %s" % task_id, task_id=task_id)
            return False

    task_status = report["task"]["status"]
    summary = report["summary"]

    # Require a terminal task state before copying.
    if task_status not in TERMINAL_TASK_STATUSES:
        if SCHEDULER_DEBUG:
            log_scheduler(
                "DEBUG",
                "task not terminal, skip run_log copy: %s" % task_status,
                task_id=task_id,
            )
        return False

    # Require aggregate counters to be fully consistent.
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

    # Resolve source and destination directories.
    paths = get_task_report_paths(report)
    src_dir = paths["task_dir"]
    if not src_dir.exists():
        log_scheduler("WARN", "source report dir not found for run_log copy: %s" % src_dir, task_id=task_id)
        return False

    revision_dir = get_report_revision_dir(report)
    dst_dir = RUN_LOG_DIR / revision_dir / str(task_id)

    # Keep the copy operation idempotent.
    if dst_dir.exists():
        log_scheduler("INFO", "run_log already exists, skip: %s" % dst_dir, task_id=task_id)
        return True

    # Copy the completed report tree.
    try:
        RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_dir, dst_dir, symlinks=True)
        log_scheduler("INFO", "copied task reports to run_log: %s" % dst_dir, task_id=task_id)
        return True
    except Exception as exc:
        log_scheduler("ERROR", "failed to copy run_log: %s" % exc, task_id=task_id)
        return False


REPORT_WRITE_LOCK = threading.Lock()
REPORT_QUEUE = queue.Queue()
REPORT_QUEUE_STATE_LOCK = threading.Lock()
REPORT_QUEUE_PENDING = set()
REPORT_QUEUE_ACTIVE = set()
REPORT_QUEUE_DIRTY = set()
REPORT_MAINTENANCE_LOCK = threading.Lock()
REPORT_MAINTENANCE_LAST_RUN = 0.0


def run_report_maintenance(force=False):
    """Run report directory maintenance at a bounded frequency."""
    global REPORT_MAINTENANCE_LAST_RUN

    now = time.time()
    with REPORT_MAINTENANCE_LOCK:
        if (
            not force
            and now - REPORT_MAINTENANCE_LAST_RUN
            < REPORT_MAINTENANCE_INTERVAL_SEC
        ):
            return 0

        moved = maintain_report_directories(REPORT_ROOT)
        REPORT_MAINTENANCE_LAST_RUN = now
        return moved


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
        # Copy only when the terminal-state consistency gate passes.
        copy_task_reports_to_run_log(task_id)
        run_report_maintenance(force=False)
        return ok
    except Exception as exc:
        log_exception("failed to write share reports for %s" % task_id, exc, task_id=task_id)
        return False


def enqueue_share_report(task_id):
    """Queue one coalesced report refresh without blocking an HTTP request."""
    if not task_id:
        return False

    task_id = str(task_id)
    should_queue = False

    with REPORT_QUEUE_STATE_LOCK:
        if task_id in REPORT_QUEUE_PENDING or task_id in REPORT_QUEUE_ACTIVE:
            REPORT_QUEUE_DIRTY.add(task_id)
        else:
            REPORT_QUEUE_PENDING.add(task_id)
            should_queue = True

    if should_queue:
        REPORT_QUEUE.put(task_id)

    return should_queue


def enqueue_latest_template_reports():
    """Queue one report refresh for the newest task of every template."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT t.task_id
            FROM tasks t
            JOIN (
                SELECT template_name, MAX(id) AS max_id
                FROM tasks
                WHERE template_name IS NOT NULL
                  AND template_name != ''
                GROUP BY template_name
            ) latest
                ON latest.max_id = t.id
            ORDER BY t.id
            """
        )
        task_ids = [row["task_id"] for row in cur.fetchall()]
    finally:
        conn.close()

    for task_id in task_ids:
        enqueue_share_report(task_id)

    return len(task_ids)


def report_writer_loop(stop_event):
    """Write queued reports outside scheduler HTTP and SQLite transactions."""
    while True:
        with REPORT_QUEUE_STATE_LOCK:
            queue_idle = not REPORT_QUEUE_PENDING and not REPORT_QUEUE_ACTIVE

        if stop_event.is_set() and queue_idle and REPORT_QUEUE.empty():
            return

        try:
            task_id = REPORT_QUEUE.get(timeout=1.0)
        except queue.Empty:
            continue

        with REPORT_QUEUE_STATE_LOCK:
            REPORT_QUEUE_PENDING.discard(task_id)
            REPORT_QUEUE_ACTIVE.add(task_id)

        try:
            write_share_reports_for_task(task_id)
        finally:
            requeue = False
            with REPORT_QUEUE_STATE_LOCK:
                REPORT_QUEUE_ACTIVE.discard(task_id)
                if task_id in REPORT_QUEUE_DIRTY:
                    REPORT_QUEUE_DIRTY.discard(task_id)
                    REPORT_QUEUE_PENDING.add(task_id)
                    requeue = True

            if requeue:
                REPORT_QUEUE.put(task_id)

            REPORT_QUEUE.task_done()

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
                t.split_mode,
                t.result_json,
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
        if row["split_mode"] == "revision_scan" or revision_policy == "per_example":
            revision_display = str(revision or "per-example")
        elif revision_policy == "latest":
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
            "split_mode": row["split_mode"],
            "result": json_loads_dict(row["result_json"]),
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
    """Recover lost assignments and refresh task aggregates without retry."""
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
            SELECT worker_name, current_task_id, current_example_id,
                   current_attempt_id, last_seen_at
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

            insert_event(
                cur,
                worker["current_task_id"],
                worker["current_example_id"],
                worker["current_attempt_id"],
                worker_name,
                "worker_stale",
                "heartbeat timeout; worker marked offline",
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

        if REQUEUE_STALE_RUNNING:
            cur.execute(
                """
                SELECT
                    e.example_id,
                    e.task_id,
                    e.retry_count,
                    e.max_retry,
                    e.current_attempt_id,
                    e.assigned_worker,
                    w.status AS worker_status,
                    w.current_task_id AS worker_task_id,
                    w.current_example_id AS worker_example_id,
                    w.current_attempt_id AS worker_attempt_id
                FROM task_examples e
                LEFT JOIN workers w
                    ON w.worker_name = e.assigned_worker
                WHERE e.status = 'running'
                  AND (
                      w.worker_name IS NULL
                      OR w.status = 'offline'
                      OR (
                          w.status IN ('running', 'reporting')
                          AND NOT (
                              w.current_task_id = e.task_id
                              AND w.current_example_id = e.example_id
                              AND w.current_attempt_id = e.current_attempt_id
                          )
                      )
                  )
                ORDER BY e.id
                """
            )
            lost_examples = cur.fetchall()

            for example in lost_examples:
                worker_name = example["assigned_worker"] or "unknown"
                if example["worker_status"] in ("running", "reporting"):
                    reason = "assignment_superseded"
                    detail = (
                        "worker %s moved to task=%s example=%s attempt=%s before reporting this attempt"
                        % (
                            worker_name,
                            example["worker_task_id"] or "-",
                            example["worker_example_id"] or "-",
                            example["worker_attempt_id"] or "-",
                        )
                    )
                elif example["worker_status"] == "offline":
                    reason = "worker_offline"
                    detail = "worker %s is offline" % worker_name
                else:
                    reason = "worker_missing"
                    detail = "worker record is missing for %s" % worker_name

                if requeue_or_fail_stale_example(
                    cur,
                    worker_name,
                    example,
                    reason,
                    detail,
                ):
                    affected_examples += 1
                    affected_task_ids.add(example["task_id"])

        for task_id in sorted(affected_task_ids):
            refresh_one_task_status(cur, task_id)

        changed_task_ids = set(refresh_all_task_statuses(cur))
        conn.commit()

        report_task_ids = affected_task_ids | changed_task_ids
        for task_id in sorted(report_task_ids):
            enqueue_share_report(task_id)

        return stale_count, affected_examples, len(changed_task_ids)

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

            if path == "/api/task/refresh-report":
                self.handle_task_refresh_report()
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

    def handle_task_refresh_report(self):
        """Queue a report refresh requested by an administrative tool."""
        data = read_json(self)
        task_id = data.get("task_id")
        if not task_id:
            self.send_json({"ok": False, "error": "missing task_id"}, status=400)
            return

        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,))
            exists = cur.fetchone() is not None
        finally:
            conn.close()

        if not exists:
            self.send_json({"ok": False, "error": "task not found"}, status=404)
            return

        queued = enqueue_share_report(task_id)
        self.send_json({"ok": True, "task_id": task_id, "queued": queued})

    def handle_list_tasks(self, parsed):
        """List parent tasks as JSON."""
        query = parse_qs(parsed.query)
        status = query.get("status", [None])[0]
        limit = int(query.get("limit", [TASK_API_DEFAULT_LIMIT])[0])
        limit = max(1, min(limit, TASK_API_MAX_LIMIT))

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
                t.split_mode,
                t.result_json,
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
        limit = int(query.get("limit", [EXAMPLE_API_DEFAULT_LIMIT])[0])
        limit = max(1, min(limit, EXAMPLE_API_MAX_LIMIT))

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
    parser.add_argument("--host", default=DEFAULT_SCHEDULER_HOST, help="listen host")
    parser.add_argument("--port", type=int, default=DEFAULT_SCHEDULER_PORT, help="listen port")
    parser.add_argument(
        "--reconcile-interval",
        type=int,
        default=DEFAULT_RECONCILE_INTERVAL,
        help="background reconciliation interval seconds",
    )
    parser.add_argument(
        "--worker-timeout",
        type=int,
        default=DEFAULT_WORKER_TIMEOUT,
        help="mark worker offline after this many heartbeat seconds",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check configuration and environment, then exit",
    )
    return parser


def main():
    """Program entry."""
    args = build_parser().parse_args()

    # Run the configuration check without starting the HTTP service.
    if args.check:
        check_configuration()
        return 0

    init_database_pragmas()
    try:
        run_report_maintenance(force=True)
    except Exception as exc:
        log_exception("initial report maintenance failed", exc)

    server = ThreadingHTTPServer((args.host, args.port), SchedulerHandler)
    stop_event = threading.Event()
    report_thread = threading.Thread(
        target=report_writer_loop,
        args=(stop_event,),
        name="pjtest-report-writer",
    )
    report_thread.daemon = True
    report_thread.start()
    enqueue_latest_template_reports()

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
    log_scheduler(
        "INFO",
        "report_retention_days=%s old_dir=%s summary_dir=%s"
        % (
            REPORT_RETENTION_DAYS,
            REPORT_ROOT / REPORT_OLD_DIR_NAME,
            REPORT_ROOT / REPORT_SUMMARY_DIR_NAME,
        ),
    )
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
        reconcile_thread.join(timeout=5)
        report_thread.join(timeout=10)
        log_scheduler("INFO", "scheduler server closed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
