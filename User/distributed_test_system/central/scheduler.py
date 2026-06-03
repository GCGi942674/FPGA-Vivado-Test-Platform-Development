#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import shlex
import argparse
import json
import sys
import traceback
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
import socketserver
from urllib.parse import urlparse, parse_qs


BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from common.db import get_conn

DEFAULT_TEST_DIR = "/home/user3/workspace/galaxcore/test2"


def ensure_task_runtime_columns(conn):
    """Ensure old SQLite databases have command and target metadata columns."""
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(tasks)")
    existing_columns = {row[1] for row in cur.fetchall()}

    column_defs = {
        "cmd": "TEXT",
        "target_arg": "TEXT",
        "target_type": "TEXT",
    }

    for column, column_type in column_defs.items():
        if column not in existing_columns:
            cur.execute(f"ALTER TABLE tasks ADD COLUMN {column} {column_type}")

    conn.commit()


def build_default_cmd(target_arg=None):
    """Build a safe fallback command for old tasks that do not have cmd."""
    target = target_arg or "."
    return f"cd {DEFAULT_TEST_DIR} && ./run.sh {target}"


def safe_load_flow_config(raw):
    """Load flow_config_json safely for old or malformed records."""
    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except Exception:
        return {}

    return data if isinstance(data, dict) else {}


def extract_runsh_target(cmd):
    """
    Extract the target argument after ./run.sh from a command string.

    Examples:
        cd /path/test2 && ./run.sh .
        cd /path/test2 && ./run.sh kintexuplus/case1
        cd /path/test2 && ./run.sh a.tcl b.tcl

    Returns:
        (target_arg, target_type)
    """

    if not cmd:
        return "", "UNKNOWN"

    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return "", "UNKNOWN"

    runsh_index = -1

    for index, token in enumerate(tokens):
        if os.path.basename(token) == "run.sh":
            runsh_index = index
            break

    if runsh_index < 0:
        return "", "UNKNOWN"

    raw_args = tokens[runsh_index + 1:]

    stop_tokens = {"&&", ";", "||", "|", ">", ">>", "2>", "2>>", "&"}
    target_args = []

    for arg in raw_args:
        if arg in stop_tokens:
            break
        target_args.append(arg)

    if not target_args:
        return ".", "ALL"

    target_arg = " ".join(target_args).strip()
    target_type = classify_runsh_target(target_args)

    return target_arg, target_type


def classify_runsh_target(target_args):
    """
    Classify run.sh target arguments for database display.
    """

    if not target_args:
        return "ALL"

    if len(target_args) > 1:
        return "FILE_LIST"

    target = target_args[0].strip()

    if target in ("", "."):
        return "ALL"

    lower_target = target.lower()

    if lower_target.endswith(".tcl"):
        return "SINGLE_TCL"

    if lower_target.endswith((".list", ".lst", ".txt", ".f", ".flist", ".filelist")):
        return "FILE_LIST"

    if lower_target.endswith("/"):
        return "DIR"

    # Most GalaxCore testcase targets are directory-like paths.
    if "/" in target and "." not in os.path.basename(target):
        return "DIR"

    return "DIR"

class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


def json_dumps(data):
    # 末尾加 \n，这样 curl 输出后会自动换行
    return (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8")


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))

    if length <= 0:
        return {}

    body = handler.rfile.read(length).decode("utf-8")

    if not body.strip():
        return {}

    return json.loads(body)


def upsert_worker(cur, worker, host="", status="idle", current_task_id=None):
    """
    兼容老版本 SQLite 的 worker upsert。

    不使用：
        ON CONFLICT(worker_name) DO UPDATE

    改成：
        SELECT 是否存在
        存在则 UPDATE
        不存在则 INSERT
    """

    cur.execute(
        """
        SELECT worker_name
        FROM workers
        WHERE worker_name = ?
        """,
        (worker,),
    )

    row = cur.fetchone()

    if row:
        if host:
            cur.execute(
                """
                UPDATE workers
                SET host = ?,
                    status = ?,
                    current_task_id = ?,
                    last_heartbeat = CURRENT_TIMESTAMP
                WHERE worker_name = ?
                """,
                (
                    host,
                    status,
                    current_task_id,
                    worker,
                ),
            )
        else:
            cur.execute(
                """
                UPDATE workers
                SET status = ?,
                    current_task_id = ?,
                    last_heartbeat = CURRENT_TIMESTAMP
                WHERE worker_name = ?
                """,
                (
                    status,
                    current_task_id,
                    worker,
                ),
            )
    else:
        cur.execute(
            """
            INSERT INTO workers (
                worker_name,
                host,
                status,
                current_task_id,
                last_heartbeat
            )
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                worker,
                host,
                status,
                current_task_id,
            ),
        )


class SchedulerHandler(BaseHTTPRequestHandler):
    server_version = "GalaxCoreScheduler/0.2"

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))
        sys.stdout.flush()

    def send_json(self, data, status=200):
        body = json_dumps(data)

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/api/health":
                self.send_json(
                    {
                        "ok": True,
                        "service": "scheduler",
                    }
                )
                return

            if path == "/api/tasks":
                self.handle_list_tasks(parsed)
                return

            if path == "/api/workers":
                self.handle_list_workers()
                return

            self.send_json(
                {
                    "ok": False,
                    "error": "not found",
                },
                status=404,
            )

        except Exception as e:
            self.handle_error(e)

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

            if path in ("/api/task/update", "/api/task/report"):
                self.handle_task_update()
                return

            self.send_json(
                {
                    "ok": False,
                    "error": "not found",
                },
                status=404,
            )

        except Exception as e:
            self.handle_error(e)

    def handle_error(self, e):
        traceback.print_exc()

        self.send_json(
            {
                "ok": False,
                "error": str(e),
            },
            status=500,
        )

    def handle_worker_register(self):
        data = read_json(self)

        worker = data.get("worker") or data.get("worker_name")
        host = data.get("host", "")

        if not worker:
            self.send_json(
                {
                    "ok": False,
                    "error": "missing worker",
                },
                status=400,
            )
            return

        conn = get_conn()
        cur = conn.cursor()

        upsert_worker(
            cur,
            worker=worker,
            host=host,
            status="idle",
            current_task_id=None,
        )

        conn.commit()
        conn.close()

        self.send_json(
            {
                "ok": True,
                "worker": worker,
                "status": "registered",
            }
        )

    def handle_worker_heartbeat(self):
        data = read_json(self)

        worker = data.get("worker") or data.get("worker_name")
        current_task_id = data.get("current_task_id")
        status = data.get("status", "idle")

        if not worker:
            self.send_json(
                {
                    "ok": False,
                    "error": "missing worker",
                },
                status=400,
            )
            return

        conn = get_conn()
        cur = conn.cursor()

        upsert_worker(
            cur,
            worker=worker,
            status=status,
            current_task_id=current_task_id,
        )

        conn.commit()
        conn.close()

        self.send_json(
            {
                "ok": True,
                "worker": worker,
                "status": status,
                "current_task_id": current_task_id,
            }
        )

    def handle_task_pull(self):
        data = read_json(self)

        worker = data.get("worker") or data.get("worker_name")

        if not worker:
            self.send_json(
                {
                    "ok": False,
                    "error": "missing worker",
                },
                status=400,
            )
            return

        conn = get_conn()
        ensure_task_runtime_columns(conn)
        cur = conn.cursor()

        try:
            # BEGIN IMMEDIATE 可以防止多个 worker 同时抢到同一个 pending 任务
            cur.execute("BEGIN IMMEDIATE")

            cur.execute(
                """
                SELECT *
                FROM tasks
                WHERE status = 'pending'
                  AND (
                        target_worker = 'any'
                        OR target_worker = ?
                      )
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """,
                (worker,),
            )

            row = cur.fetchone()

            if not row:
                upsert_worker(
                    cur,
                    worker=worker,
                    status="idle",
                    current_task_id=None,
                )

                conn.commit()
                conn.close()

                self.send_json(
                    {
                        "ok": True,
                        "task": None,
                    }
                )
                return

            task_id = row["task_id"]

            cur.execute(
                """
                UPDATE tasks
                SET status = 'running',
                    assigned_worker = ?,
                    started_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
                  AND status = 'pending'
                """,
                (
                    worker,
                    task_id,
                ),
            )

            if cur.rowcount != 1:
                conn.rollback()
                conn.close()

                self.send_json(
                    {
                        "ok": False,
                        "error": "failed to lock task",
                    },
                    status=409,
                )
                return

            cur.execute(
                """
                INSERT INTO task_events (
                    task_id,
                    worker_name,
                    event,
                    message
                )
                VALUES (?, ?, 'pulled', ?)
                """,
                (
                    task_id,
                    worker,
                    "Task pulled by worker %s" % worker,
                ),
            )

            upsert_worker(
                cur,
                worker=worker,
                status="running",
                current_task_id=task_id,
            )

            conn.commit()

            cmd = row["cmd"] or build_default_cmd(row["target_arg"] or ".")
            target_arg = row["target_arg"]
            target_type = row["target_type"]

            if not target_arg or not target_type:
                target_arg, target_type = extract_runsh_target(cmd)

            task = {
                "task_id": row["task_id"],
                "id": row["task_id"],
                "revision": row["revision"],
                "suite": row["suite"],
                "cmd": cmd,
                "target_arg": target_arg,
                "target_type": target_type,
                "flow_config": safe_load_flow_config(row["flow_config_json"]),
                "target_worker": row["target_worker"],
                "priority": row["priority"],
            }

            conn.close()

            self.send_json(
                {
                    "ok": True,
                    "task": task,
                }
            )

        except Exception:
            conn.rollback()
            conn.close()
            raise

    def handle_task_update(self):
        data = read_json(self)

        task_id = data.get("task_id")
        worker = data.get("worker") or data.get("worker_name")
        status = data.get("status")
        result_path = data.get("result_path")
        error_message = data.get("error_message")

        if error_message is None:
            error_message = data.get("message")

        if error_message is None and data.get("log_tail"):
            error_message = str(data.get("log_tail"))[-4000:]

        if not task_id:
            self.send_json(
                {
                    "ok": False,
                    "error": "missing task_id",
                },
                status=400,
            )
            return

        if not worker:
            self.send_json(
                {
                    "ok": False,
                    "error": "missing worker",
                },
                status=400,
            )
            return

        if status not in ("success", "failed", "timeout"):
            self.send_json(
                {
                    "ok": False,
                    "error": "invalid status",
                },
                status=400,
            )
            return

        conn = get_conn()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT task_id, assigned_worker, status
            FROM tasks
            WHERE task_id = ?
            """,
            (task_id,),
        )

        row = cur.fetchone()

        if not row:
            conn.close()

            self.send_json(
                {
                    "ok": False,
                    "error": "task not found",
                },
                status=404,
            )
            return

        if row["assigned_worker"] != worker:
            conn.close()

            self.send_json(
                {
                    "ok": False,
                    "error": "task assigned_worker mismatch",
                    "assigned_worker": row["assigned_worker"],
                    "request_worker": worker,
                },
                status=400,
            )
            return

        cur.execute(
            """
            UPDATE tasks
            SET status = ?,
                finished_at = CURRENT_TIMESTAMP,
                result_path = ?,
                error_message = ?
            WHERE task_id = ?
            """,
            (
                status,
                result_path,
                error_message,
                task_id,
            ),
        )

        cur.execute(
            """
            INSERT INTO task_events (
                task_id,
                worker_name,
                event,
                message
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                task_id,
                worker,
                status,
                error_message or result_path or "",
            ),
        )

        upsert_worker(
            cur,
            worker=worker,
            status="idle",
            current_task_id=None,
        )

        conn.commit()
        conn.close()

        self.send_json(
            {
                "ok": True,
                "task_id": task_id,
                "status": status,
            }
        )

    def handle_list_tasks(self, parsed):
        query = parse_qs(parsed.query)

        status = None
        limit = 50

        if "status" in query and query["status"]:
            status = query["status"][0]

        if "limit" in query and query["limit"]:
            try:
                limit = int(query["limit"][0])
            except ValueError:
                limit = 50

        if limit <= 0:
            limit = 50

        if limit > 200:
            limit = 200

        conn = get_conn()
        ensure_task_runtime_columns(conn)
        cur = conn.cursor()

        if status:
            cur.execute(
                """
                SELECT task_id, revision, suite, target_worker, assigned_worker,
                       status, priority, retry_count, max_retry, created_at,
                       started_at, finished_at, result_path, error_message,
                       cmd, target_arg, target_type
                FROM tasks
                WHERE status = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (
                    status,
                    limit,
                ),
            )
        else:
            cur.execute(
                """
                SELECT task_id, revision, suite, target_worker, assigned_worker,
                       status, priority, retry_count, max_retry, created_at,
                       started_at, finished_at, result_path, error_message,
                       cmd, target_arg, target_type
                FROM tasks
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )

        rows = cur.fetchall()
        conn.close()

        tasks = []

        for row in rows:
            tasks.append(
                {
                    "task_id": row["task_id"],
                    "revision": row["revision"],
                    "suite": row["suite"],
                    "target_worker": row["target_worker"],
                    "assigned_worker": row["assigned_worker"],
                    "status": row["status"],
                    "priority": row["priority"],
                    "retry_count": row["retry_count"],
                    "max_retry": row["max_retry"],
                    "created_at": row["created_at"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "result_path": row["result_path"],
                    "error_message": row["error_message"],
                    "cmd": row["cmd"],
                    "target_arg": row["target_arg"],
                    "target_type": row["target_type"],
                }
            )

        self.send_json(
            {
                "ok": True,
                "tasks": tasks,
            }
        )

    def handle_list_workers(self):
        conn = get_conn()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT worker_name, host, status, current_task_id,
                   last_heartbeat, registered_at
            FROM workers
            ORDER BY worker_name ASC
            """
        )

        rows = cur.fetchall()
        conn.close()

        workers = []

        for row in rows:
            workers.append(
                {
                    "worker_name": row["worker_name"],
                    "host": row["host"],
                    "status": row["status"],
                    "current_task_id": row["current_task_id"],
                    "last_heartbeat": row["last_heartbeat"],
                    "registered_at": row["registered_at"],
                }
            )

        self.send_json(
            {
                "ok": True,
                "workers": workers,
            }
        )


def main():
    parser = argparse.ArgumentParser(
        description="GalaxCore distributed test scheduler"
    )

    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)

    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), SchedulerHandler)

    print("Scheduler started: http://%s:%s" % (args.host, args.port))
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nScheduler stopped.")
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())