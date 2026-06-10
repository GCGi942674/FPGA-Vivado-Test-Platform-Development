#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Initialize or reset the PJTest SQLite database.

All time fields must be written explicitly as local-time strings
by taskctl / scheduler / worker.  This script does not create DEFAULT
CURRENT_TIMESTAMP columns so that UTC is never used accidentally.

Usage:
    python3 init_db.py            # safe init (creates tables if missing)
    python3 init_db.py --reset    # drop & recreate everything
"""

import argparse
import sqlite3
from pathlib import Path


DB_PATH = Path("/home/user3/distributed_test_system/data/task_queue.db")


def drop_all(cur):
    """Drop every table known to PJTest so --reset works cleanly."""
    cur.execute("DROP TABLE IF EXISTS task_events")
    cur.execute("DROP TABLE IF EXISTS task_examples")
    cur.execute("DROP TABLE IF EXISTS tasks")
    cur.execute("DROP TABLE IF EXISTS workers")
    cur.execute("DROP TABLE IF EXISTS builds")
    cur.execute("DROP TABLE IF EXISTS repeat_groups")


def create_tasks(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT UNIQUE NOT NULL,

            task_name TEXT,
            template_name TEXT,

            revision TEXT NOT NULL,
            revision_policy TEXT NOT NULL DEFAULT 'fixed',

            suite TEXT NOT NULL DEFAULT 'night_build',
            target_worker TEXT NOT NULL DEFAULT 'any',

            priority INTEGER NOT NULL DEFAULT 100,
            max_retry INTEGER NOT NULL DEFAULT 1,
            max_time INTEGER DEFAULT 3600,

            work_root TEXT NOT NULL,
            target_dir TEXT NOT NULL,

            split_mode TEXT NOT NULL DEFAULT 'scan',

            flow_config_json TEXT NOT NULL,

            status TEXT NOT NULL DEFAULT 'pending',

            total_examples INTEGER NOT NULL DEFAULT 0,

            repeat_enabled INTEGER NOT NULL DEFAULT 0,
            repeat_group TEXT,
            repeat_index INTEGER DEFAULT 1,
            parent_task_id TEXT,

            created_at TEXT NOT NULL,
            updated_at TEXT,
            started_at TEXT,
            finished_at TEXT,

            message TEXT
        )
    """)


def create_task_examples(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS task_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            example_id TEXT UNIQUE NOT NULL,
            task_id TEXT NOT NULL,

            seq INTEGER NOT NULL,

            platform TEXT,
            target_arg TEXT NOT NULL,
            run_tcl_path TEXT NOT NULL,

            cmd TEXT NOT NULL,

            status TEXT NOT NULL DEFAULT 'pending',

            assigned_worker TEXT,

            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retry INTEGER NOT NULL DEFAULT 1,

            created_at TEXT NOT NULL,
            updated_at TEXT,
            started_at TEXT,
            finished_at TEXT,

            exit_code INTEGER,

            failed_step TEXT,
            failed_reason TEXT,
            message TEXT,

            log_file TEXT,
            log_tail TEXT,

            run_log_dir TEXT,
            report_dir TEXT,

            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
            UNIQUE(task_id, run_tcl_path)
        )
    """)


def create_workers(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            worker_name TEXT PRIMARY KEY,
            hostname TEXT,
            status TEXT NOT NULL DEFAULT 'idle',
            current_task_id TEXT,
            current_example_id TEXT,
            capabilities_json TEXT,
            last_seen_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            updated_at TEXT,
            message TEXT
        )
    """)


def create_task_events(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            example_id TEXT,
            worker_name TEXT,
            event TEXT NOT NULL,
            message TEXT,
            created_at TEXT NOT NULL
        )
    """)


def create_indexes(cur):
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_status_priority "
        "ON tasks(status, priority, created_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_examples_status_seq "
        "ON task_examples(status, seq)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_examples_task_status "
        "ON task_examples(task_id, status)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_examples_worker "
        "ON task_examples(assigned_worker)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_task "
        "ON task_events(task_id, created_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_example "
        "ON task_events(example_id, created_at)"
    )


def init_db(reset=False):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    if reset:
        drop_all(cur)
        print("All existing tables dropped.")

    create_tasks(cur)
    create_task_examples(cur)
    create_workers(cur)
    create_task_events(cur)
    create_indexes(cur)

    conn.commit()
    conn.close()

    print("PJTest database initialized: %s" % DB_PATH)
    if reset:
        print("Database reset complete.")


def main():
    parser = argparse.ArgumentParser(description="Initialize PJTest SQLite database")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="drop all tables before creating new ones (DESTRUCTIVE)",
    )
    args = parser.parse_args()
    init_db(reset=args.reset)


if __name__ == "__main__":
    main()