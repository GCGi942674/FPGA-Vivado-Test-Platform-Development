#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

            if path == "/api/task/update":
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

            task = {
                "task_id": row["task_id"],
                "revision": row["revision"],
                "suite": row["suite"],
                "flow_config": json.loads(row["flow_config_json"]),
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
        cur = conn.cursor()

        if status:
            cur.execute(
                """
                SELECT task_id, revision, suite, target_worker, assigned_worker,
                       status, priority, retry_count, max_retry, created_at,
                       started_at, finished_at, result_path, error_message
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
                       started_at, finished_at, result_path, error_message
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