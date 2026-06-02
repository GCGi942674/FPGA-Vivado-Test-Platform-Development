#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
from pathlib import Path


DB_PATH = Path("/home/user3/distributed_test_system/data/task_queue.db")


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")

    cur = conn.cursor()

    # GalaxCore 构建版本表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS builds (
        revision INTEGER PRIMARY KEY,
        zip_path TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'success',
        build_time TEXT DEFAULT CURRENT_TIMESTAMP,
        error_message TEXT
    );
    """)

    # 测试任务表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT UNIQUE NOT NULL,

        revision INTEGER NOT NULL,
        suite TEXT NOT NULL,

        flow_config_json TEXT NOT NULL,

        target_worker TEXT DEFAULT 'any',
        assigned_worker TEXT,

        status TEXT NOT NULL DEFAULT 'pending',
        priority INTEGER DEFAULT 100,

        retry_count INTEGER DEFAULT 0,
        max_retry INTEGER DEFAULT 1,

        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        started_at TEXT,
        finished_at TEXT,

        result_path TEXT,
        error_message TEXT
    );
    """)

    # worker 状态表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS workers (
        worker_name TEXT PRIMARY KEY,
        host TEXT,

        status TEXT DEFAULT 'offline',
        current_task_id TEXT,

        last_heartbeat TEXT,
        registered_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 任务事件日志表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        worker_name TEXT,
        event TEXT NOT NULL,
        message TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 常用索引
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_tasks_status_priority
    ON tasks(status, priority, created_at);
    """)

    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_tasks_assigned_worker
    ON tasks(assigned_worker);
    """)

    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_task_events_task_id
    ON task_events(task_id);
    """)

    conn.commit()
    conn.close()

    print(f"SQLite database initialized: {DB_PATH}")


if __name__ == "__main__":
    init_db()