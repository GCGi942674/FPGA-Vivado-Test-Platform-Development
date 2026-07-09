#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Initialize or reset the PJTest SQLite database.

The database lives only on the central scheduler host, usually pudong.
Workers must not access this SQLite file directly.  Workers communicate with
scheduler.py through HTTP APIs.

Usage:
    python3 init_db.py
    python3 init_db.py --reset
"""

import argparse
import os
import re
import sqlite3
from pathlib import Path


DB_PATH = Path(os.environ.get(
    "PJTEST_DB_PATH",
    "/home/user3/PJTest/data/task_queue.db",
))


TABLES = [
    "task_attempts",
    "task_events",
    "task_examples",
    "tasks",
    "workers",
    "repeat_groups",
]


def drop_all(cur):
    """Drop every PJTest table for a clean reset."""
    for table_name in TABLES:
        cur.execute("DROP TABLE IF EXISTS %s" % table_name)


def create_tasks(cur):
    """Create the parent task table.

    One row in tasks means one user-submitted batch, for example:
        ./taskctl.py add place .

    The actual distributable units are stored in task_examples.
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT UNIQUE NOT NULL,

            task_name TEXT,
            template_name TEXT,

            revision TEXT,
            revision_policy TEXT NOT NULL DEFAULT 'latest',
            resolved_zip_path TEXT,

            suite TEXT NOT NULL DEFAULT 'night_build',
            target_worker TEXT NOT NULL DEFAULT 'any',

            priority INTEGER NOT NULL DEFAULT 100,
            max_retry INTEGER NOT NULL DEFAULT 1,
            max_time INTEGER DEFAULT 3600,

            work_root TEXT NOT NULL,
            target_dir TEXT NOT NULL,
            split_mode TEXT NOT NULL DEFAULT 'scan',

            flow_config_json TEXT NOT NULL DEFAULT '{}',

            status TEXT NOT NULL DEFAULT 'pending',
            total_examples INTEGER NOT NULL DEFAULT 0,

            repeat_enabled INTEGER NOT NULL DEFAULT 0,
            repeat_group TEXT,
            repeat_index INTEGER NOT NULL DEFAULT 1,
            parent_task_id TEXT,

            created_at TEXT NOT NULL,
            updated_at TEXT,
            started_at TEXT,
            finished_at TEXT,

            result_json TEXT NOT NULL DEFAULT '{}',
            message TEXT
        )
        """
    )


def task_examples_table_sql(table_name="task_examples"):
    """Return the task_examples schema used by normal and revision-scan tasks."""
    return """
        CREATE TABLE IF NOT EXISTS %s (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            example_id TEXT UNIQUE NOT NULL,
            task_id TEXT NOT NULL,

            seq INTEGER NOT NULL,
            platform TEXT,

            target_arg TEXT NOT NULL,
            run_tcl_path TEXT NOT NULL,
            cmd TEXT NOT NULL,

            revision TEXT,
            resolved_zip_path TEXT,

            status TEXT NOT NULL DEFAULT 'pending',
            assigned_worker TEXT,
            current_attempt_id TEXT,

            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retry INTEGER NOT NULL DEFAULT 1,

            created_at TEXT NOT NULL,
            updated_at TEXT,
            started_at TEXT,
            finished_at TEXT,

            exit_code INTEGER,
            raw_exit_code INTEGER,
            failed_step TEXT,
            failed_reason TEXT,
            infra_reason TEXT,
            message TEXT,

            log_file TEXT,
            log_tail TEXT,
            run_log_dir TEXT,
            report_dir TEXT,

            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        )
        """ % table_name


def create_task_examples(cur):
    """Create the table for one runnable execution item."""
    cur.execute(task_examples_table_sql())


def create_task_attempts(cur):
    """Create immutable-ish attempt records for every worker execution."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS task_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id TEXT UNIQUE NOT NULL,
            example_id TEXT NOT NULL,
            task_id TEXT NOT NULL,

            attempt_no INTEGER NOT NULL,
            worker_name TEXT NOT NULL,

            status TEXT NOT NULL DEFAULT 'running',
            revision TEXT,
            zip_path TEXT,

            started_at TEXT NOT NULL,
            finished_at TEXT,

            exit_code INTEGER,
            raw_exit_code INTEGER,
            infra_reason TEXT,
            timed_out INTEGER NOT NULL DEFAULT 0,

            log_file TEXT,
            log_tail TEXT,
            run_log_dir TEXT,
            report_dir TEXT,

            message TEXT,

            FOREIGN KEY (example_id) REFERENCES task_examples(example_id) ON DELETE CASCADE,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        )
        """
    )


def create_workers(cur):
    """Create central worker registry and heartbeat table."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS workers (
            worker_name TEXT PRIMARY KEY,
            hostname TEXT,
            status TEXT NOT NULL DEFAULT 'idle',
            current_task_id TEXT,
            current_example_id TEXT,
            current_attempt_id TEXT,
            capabilities_json TEXT,
            last_seen_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            updated_at TEXT,
            message TEXT
        )
        """
    )


def create_task_events(cur):
    """Create append-only scheduler event table."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            example_id TEXT,
            attempt_id TEXT,
            worker_name TEXT,
            event TEXT NOT NULL,
            message TEXT,
            created_at TEXT NOT NULL
        )
        """
    )


def create_repeat_groups(cur):
    """Create repeat group table for later loop-task control."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS repeat_groups (
            repeat_group TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            disabled_at TEXT,
            note TEXT
        )
        """
    )


def create_indexes(cur):
    """Create indexes used by scheduler pull/report operations."""
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_status_priority "
        "ON tasks(status, priority, created_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_repeat_group "
        "ON tasks(repeat_group, status)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_examples_pull "
        "ON task_examples(status, task_id, seq)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_examples_task_status "
        "ON task_examples(task_id, status)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_examples_worker "
        "ON task_examples(assigned_worker, status)"
    )
    # Do not use partial indexes here. Older SQLite versions used on some
    # PJTest hosts do not support "CREATE INDEX ... WHERE ...".
    cur.execute("DROP INDEX IF EXISTS idx_examples_unique_normal")
    cur.execute("DROP INDEX IF EXISTS idx_examples_unique_revision")

    cur.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_examples_unique_insert
        BEFORE INSERT ON task_examples
        BEGIN
            SELECT CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM task_examples existing
                    WHERE existing.task_id = NEW.task_id
                      AND existing.run_tcl_path = NEW.run_tcl_path
                      AND (
                          (
                              COALESCE(NEW.revision, '') = ''
                              AND COALESCE(existing.revision, '') = ''
                          )
                          OR
                          (
                              COALESCE(NEW.revision, '') != ''
                              AND existing.revision = NEW.revision
                          )
                      )
                )
                THEN RAISE(
                    ABORT,
                    'duplicate task example for task/run_tcl/revision'
                )
            END;
        END
        """
    )
    cur.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_examples_unique_update
        BEFORE UPDATE OF task_id, run_tcl_path, revision ON task_examples
        BEGIN
            SELECT CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM task_examples existing
                    WHERE existing.id != OLD.id
                      AND existing.task_id = NEW.task_id
                      AND existing.run_tcl_path = NEW.run_tcl_path
                      AND (
                          (
                              COALESCE(NEW.revision, '') = ''
                              AND COALESCE(existing.revision, '') = ''
                          )
                          OR
                          (
                              COALESCE(NEW.revision, '') != ''
                              AND existing.revision = NEW.revision
                          )
                      )
                )
                THEN RAISE(
                    ABORT,
                    'duplicate task example for task/run_tcl/revision'
                )
            END;
        END
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_examples_revision_status "
        "ON task_examples(task_id, revision, status)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempts_example "
        "ON task_attempts(example_id, attempt_no)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempts_status "
        "ON task_attempts(status, worker_name)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_task "
        "ON task_events(task_id, created_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_example "
        "ON task_events(example_id, created_at)"
    )


def ensure_columns(cur, table_name, column_defs):
    """Add missing columns for older databases."""
    cur.execute("PRAGMA table_info(%s)" % table_name)
    existing = set(row[1] for row in cur.fetchall())

    for column_name, column_def in column_defs.items():
        if column_name not in existing:
            cur.execute(
                "ALTER TABLE %s ADD COLUMN %s %s"
                % (table_name, column_name, column_def)
            )


def migrate_existing_tables(cur):
    """Make old PJTest databases compatible without dropping data."""
    ensure_columns(
        cur,
        "tasks",
        {
            "revision_policy": "TEXT NOT NULL DEFAULT 'latest'",
            "resolved_zip_path": "TEXT",
            "repeat_enabled": "INTEGER NOT NULL DEFAULT 0",
            "repeat_group": "TEXT",
            "repeat_index": "INTEGER NOT NULL DEFAULT 1",
            "parent_task_id": "TEXT",
            "updated_at": "TEXT",
            "result_json": "TEXT NOT NULL DEFAULT '{}'",
            "message": "TEXT",
        },
    )
    ensure_columns(
        cur,
        "task_examples",
        {
            "revision": "TEXT",
            "resolved_zip_path": "TEXT",
            "current_attempt_id": "TEXT",
            "updated_at": "TEXT",
            "log_tail": "TEXT",
            "run_log_dir": "TEXT",
            "report_dir": "TEXT",
            "message": "TEXT",
            "raw_exit_code": "INTEGER",
            "infra_reason": "TEXT",
        },
    )
    ensure_columns(
        cur,
        "task_attempts",
        {
            "raw_exit_code": "INTEGER",
            "infra_reason": "TEXT",
        },
    )
    ensure_columns(
        cur,
        "workers",
        {
            "current_attempt_id": "TEXT",
            "capabilities_json": "TEXT",
            "updated_at": "TEXT",
            "message": "TEXT",
        },
    )
    ensure_columns(
        cur,
        "task_events",
        {
            "attempt_id": "TEXT",
        },
    )



def get_table_sql(cur, table_name):
    """Return the CREATE TABLE SQL for one table."""
    cur.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] else ""


def task_examples_needs_rebuild(cur):
    """Return True when the legacy two-column UNIQUE constraint still exists."""
    sql = get_table_sql(cur, "task_examples")
    normalized = re.sub(r"\s+", "", sql.lower())
    return "unique(task_id,run_tcl_path)" in normalized


def rebuild_task_examples_for_revision_scan(conn):
    """Remove the legacy UNIQUE(task_id, run_tcl_path) without losing rows.

    Revision scans intentionally store the same run_tcl_path multiple times,
    once for each revision. SQLite cannot drop a table-level UNIQUE constraint,
    so an existing table must be copied into the new schema.
    """
    cur = conn.cursor()
    if not task_examples_needs_rebuild(cur):
        return False

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.cursor()

    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("DROP TABLE IF EXISTS task_examples_revision_scan_new")
        cur.execute(task_examples_table_sql("task_examples_revision_scan_new"))

        cur.execute("PRAGMA table_info(task_examples)")
        existing = set(row[1] for row in cur.fetchall())
        columns = [
            "id", "example_id", "task_id", "seq", "platform",
            "target_arg", "run_tcl_path", "cmd", "revision",
            "resolved_zip_path", "status", "assigned_worker",
            "current_attempt_id", "retry_count", "max_retry", "created_at",
            "updated_at", "started_at", "finished_at", "exit_code",
            "raw_exit_code", "failed_step", "failed_reason", "infra_reason", "message", "log_file",
            "log_tail", "run_log_dir", "report_dir",
        ]
        select_parts = [name if name in existing else "NULL AS %s" % name for name in columns]
        cur.execute(
            "INSERT INTO task_examples_revision_scan_new (%s) SELECT %s FROM task_examples"
            % (", ".join(columns), ", ".join(select_parts))
        )
        cur.execute("DROP TABLE task_examples")
        cur.execute(
            "ALTER TABLE task_examples_revision_scan_new RENAME TO task_examples"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError("foreign key check failed after task_examples migration: %s" % violations)
    return True

def init_db(reset=False):
    """Initialize PJTest database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    if reset:
        drop_all(cur)
        print("All existing PJTest tables dropped.")

    create_tasks(cur)
    create_task_examples(cur)
    create_task_attempts(cur)
    create_workers(cur)
    create_task_events(cur)
    create_repeat_groups(cur)
    migrate_existing_tables(cur)
    conn.commit()

    rebuilt_examples = rebuild_task_examples_for_revision_scan(conn)
    cur = conn.cursor()
    create_indexes(cur)

    conn.commit()
    conn.close()

    print("PJTest database initialized: %s" % DB_PATH)
    if rebuilt_examples:
        print("Migrated task_examples UNIQUE constraint for revision scans.")
    if reset:
        print("Database reset complete.")


def main():
    """Program entry."""
    parser = argparse.ArgumentParser(description="Initialize PJTest SQLite database")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="drop all tables before creating them again",
    )
    args = parser.parse_args()
    init_db(reset=args.reset)


if __name__ == "__main__":
    main()     