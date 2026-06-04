#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
from pathlib import Path


DB_PATH = Path("/home/user3/distributed_test_system/data/task_queue.db")


def column_exists(cur, table_name, column_name):
    cur.execute("PRAGMA table_info(%s)" % table_name)
    rows = cur.fetchall()

    for row in rows:
        if row[1] == column_name:
            return True

    return False


def add_column_if_missing(cur, table_name, column_def):
    column_name = column_def.split()[0]

    if column_exists(cur, table_name, column_name):
        print("Column already exists: %s.%s" % (table_name, column_name))
        return

    sql = "ALTER TABLE %s ADD COLUMN %s" % (table_name, column_def)
    cur.execute(sql)
    print("Column added: %s.%s" % (table_name, column_name))


def migrate():
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    add_column_if_missing(cur, "tasks", "repeat_enabled INTEGER DEFAULT 0")
    add_column_if_missing(cur, "tasks", "repeat_group TEXT")
    add_column_if_missing(cur, "tasks", "repeat_index INTEGER DEFAULT 1")
    add_column_if_missing(cur, "tasks", "parent_task_id TEXT")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS repeat_groups (
        repeat_group TEXT PRIMARY KEY,
        enabled INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        disabled_at TEXT,
        note TEXT
    )
    """)

    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_tasks_repeat_group
    ON tasks(repeat_group, status)
    """)

    conn.commit()
    conn.close()

    print("Repeat task migration finished.")


if __name__ == "__main__":
    migrate()