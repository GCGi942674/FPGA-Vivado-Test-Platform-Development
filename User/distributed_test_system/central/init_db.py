#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Initialize or migrate the SQLite database for the distributed GalaxCore test system.

This script runs only on the central server, usually pudong.
The SQLite database is central-only. Workers should never access it directly.

Time policy:
    - SQLite CURRENT_TIMESTAMP is UTC.
    - Runtime scripts explicitly write datetime('now','localtime') when creating
      or updating records.
    - Table defaults are kept mostly for compatibility, but scheduler/taskctl
      should not rely on them.

Revision policy:
    - revision_policy = 'fixed': use the task revision as-is.
    - revision_policy = 'latest': scheduler resolves the latest compiled
      GalaxCore zip revision at pull time.
"""

import sqlite3
from datetime import datetime
from pathlib import Path


DB_PATH = Path("/home/user3/distributed_test_system/data/task_queue.db")


TASK_COLUMNS = [
    "id",
    "task_id",
    "revision",
    "revision_policy",
    "suite",
    "cmd",
    "target_arg",
    "target_type",
    "flow_config_json",
    "target_worker",
    "assigned_worker",
    "status",
    "priority",
    "retry_count",
    "max_retry",
    "repeat_enabled",
    "repeat_group",
    "repeat_index",
    "parent_task_id",
    "created_at",
    "started_at",
    "finished_at",
    "result_path",
    "error_message",
]


def table_exists(cur, table_name):
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


def get_columns(cur, table_name):
    cur.execute("PRAGMA table_info(%s)" % table_name)
    return cur.fetchall()


def get_column_names(cur, table_name):
    return [row[1] for row in get_columns(cur, table_name)]


def create_builds_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS builds (
            revision INTEGER PRIMARY KEY,
            zip_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'success',
            build_time TEXT DEFAULT CURRENT_TIMESTAMP,
            error_message TEXT
        )
        """
    )


def create_tasks_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT UNIQUE NOT NULL,

            revision INTEGER,
            revision_policy TEXT DEFAULT 'fixed',
            suite TEXT NOT NULL,

            cmd TEXT,
            target_arg TEXT,
            target_type TEXT,

            flow_config_json TEXT NOT NULL,

            target_worker TEXT DEFAULT 'any',
            assigned_worker TEXT,

            status TEXT NOT NULL DEFAULT 'pending',
            priority INTEGER DEFAULT 100,

            retry_count INTEGER DEFAULT 0,
            max_retry INTEGER DEFAULT 1,

            repeat_enabled INTEGER DEFAULT 0,
            repeat_group TEXT,
            repeat_index INTEGER DEFAULT 1,
            parent_task_id TEXT,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            finished_at TEXT,

            result_path TEXT,
            error_message TEXT
        )
        """
    )


def create_workers_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS workers (
            worker_name TEXT PRIMARY KEY,
            host TEXT,

            status TEXT DEFAULT 'offline',
            current_task_id TEXT,

            last_heartbeat TEXT,
            registered_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def create_task_events_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            worker_name TEXT,
            event TEXT NOT NULL,
            message TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def create_repeat_groups_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS repeat_groups (
            repeat_group TEXT PRIMARY KEY,
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            disabled_at TEXT,
            note TEXT
        )
        """
    )


def create_indexes(cur):
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tasks_status_priority
        ON tasks(status, priority, created_at)
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tasks_assigned_worker
        ON tasks(assigned_worker)
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tasks_target_type
        ON tasks(target_type)
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tasks_repeat_group
        ON tasks(repeat_group, status)
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_task_events_task_id
        ON task_events(task_id)
        """
    )


def add_missing_columns(conn, table_name, column_defs):
    cur = conn.cursor()
    existing_columns = set(get_column_names(cur, table_name))

    for column_name, column_def in column_defs.items():
        if column_name not in existing_columns:
            cur.execute(
                "ALTER TABLE %s ADD COLUMN %s %s"
                % (table_name, column_name, column_def)
            )
            print("Column added: %s.%s" % (table_name, column_name))

    conn.commit()


def add_missing_task_columns(conn):
    add_missing_columns(
        conn,
        "tasks",
        {
            "revision_policy": "TEXT DEFAULT 'fixed'",
            "cmd": "TEXT",
            "target_arg": "TEXT",
            "target_type": "TEXT",
            "repeat_enabled": "INTEGER DEFAULT 0",
            "repeat_group": "TEXT",
            "repeat_index": "INTEGER DEFAULT 1",
            "parent_task_id": "TEXT",
        },
    )


def revision_column_is_notnull(cur):
    """Return True if tasks.revision is defined as NOT NULL in an old schema."""
    for row in get_columns(cur, "tasks"):
        # PRAGMA table_info columns:
        # cid, name, type, notnull, dflt_value, pk
        if row[1] == "revision":
            return int(row[3]) == 1
    return False


def migrate_tasks_revision_nullable(conn):
    """
    Rebuild tasks table if revision was created as NOT NULL.

    SQLite cannot remove a NOT NULL constraint with ALTER TABLE, so the table
    is renamed, recreated, and copied back.
    """
    cur = conn.cursor()

    if not table_exists(cur, "tasks"):
        return

    if not revision_column_is_notnull(cur):
        return

    backup_table = "tasks_backup_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    print("Migrating tasks.revision to nullable. Backup table: %s" % backup_table)

    cur.execute("ALTER TABLE tasks RENAME TO %s" % backup_table)
    create_tasks_table(cur)

    old_columns = set(get_column_names(cur, backup_table))
    new_columns = set(get_column_names(cur, "tasks"))
    common_columns = [col for col in TASK_COLUMNS if col in old_columns and col in new_columns]

    column_list = ", ".join(common_columns)
    cur.execute(
        """
        INSERT INTO tasks (%s)
        SELECT %s
        FROM %s
        """ % (column_list, column_list, backup_table)
    )

    conn.commit()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    cur = conn.cursor()

    create_builds_table(cur)
    create_tasks_table(cur)
    create_workers_table(cur)
    create_task_events_table(cur)
    create_repeat_groups_table(cur)
    conn.commit()

    add_missing_task_columns(conn)
    migrate_tasks_revision_nullable(conn)

    create_indexes(cur)

    conn.commit()
    conn.close()

    print("SQLite database initialized: %s" % DB_PATH)


if __name__ == "__main__":
    init_db()
