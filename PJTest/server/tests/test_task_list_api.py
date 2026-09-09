#!/usr/bin/env python3
"""Task-list query correctness and bounded-history regression checks."""

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from database.init_db import create_task_examples, create_tasks
from scheduler_core import main as scheduler


class TaskListTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = str(Path(self.temp.name) / "tasks.db")
        with sqlite3.connect(self.db_path) as conn:
            create_tasks(conn.cursor())
            create_task_examples(conn.cursor())
            conn.execute("CREATE INDEX idx_examples_task_status "
                         "ON task_examples(task_id, status)")

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def seed(self, records):
        with self.connect() as conn:
            for task_id, status, example_statuses in records:
                conn.execute(
                    "INSERT INTO tasks(task_id, template_name, status, "
                    "work_root, target_dir, created_at) "
                    "VALUES (?, 'route_design', ?, '/work', 'runlist', '2026-09-09')",
                    (task_id, status),
                )
                conn.executemany(
                    "INSERT INTO task_examples(example_id, task_id, seq, "
                    "target_arg, run_tcl_path, cmd, status, created_at) "
                    "VALUES (?, ?, ?, 'case', 'case/run.tcl', 'run.sh', ?, '2026-09-09')",
                    ((task_id + "_" + str(i), task_id, i, value)
                     for i, value in enumerate(example_statuses)),
                )

    def request(self, query, budget=None):
        conn = self.connect()
        self.addCleanup(conn.close)
        ticks = [0]

        def progress():
            ticks[0] += 100
            return int(budget is not None and ticks[0] > budget)

        conn.set_progress_handler(progress, 100)
        handler = scheduler.SchedulerHandler.__new__(scheduler.SchedulerHandler)
        handler.send_json = Mock()
        with patch.object(scheduler, "get_conn", return_value=conn), \
                patch.object(scheduler, "log_scheduler"):
            handler.handle_list_tasks(urlparse("/api/tasks?" + query))
        return handler.send_json.call_args[0][0]["tasks"], ticks[0]

    def test_latest_task_counts_and_order(self):
        self.seed([
            ("old", "success", ["success"] * 8),
            ("latest", "failed", ["pending", "running", "success", "success",
                                  "failed", "timeout", "canceled"]),
        ])
        rows, _ = self.request("limit=1")
        self.assertEqual([row["task_id"] for row in rows], ["latest"])
        row = rows[0]
        self.assertEqual(row["real_total"], 7)
        self.assertEqual(row["pending_count"], 1)
        self.assertEqual(row["running_count"], 1)
        self.assertEqual(row["success_count"], 2)
        self.assertEqual(row["failed_count"], 2)
        self.assertEqual(row["done_count"], 5)

    def test_status_filter_and_empty_task(self):
        self.seed([("first", "failed", ["failed"]),
                   ("second", "success", ["success"]),
                   ("third", "failed", [])])
        rows, _ = self.request("status=failed&limit=200")
        self.assertEqual([row["task_id"] for row in rows], ["third", "first"])
        self.assertEqual(rows[0]["real_total"], 0)
        self.assertEqual(rows[0]["done_count"], 0)

    def test_filter_is_applied_before_limit(self):
        self.seed([("older_failed", "failed", ["failed"]),
                   ("newer_success", "success", ["success"])])
        rows, _ = self.request("status=failed&limit=1")
        self.assertEqual([row["task_id"] for row in rows], ["older_failed"])

    def test_keyset_paging_and_suite_filter(self):
        self.seed([("first", "success", ["success"]),
                   ("second", "failed", ["failed"]),
                   ("third", "running", [])])
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET suite='daily_regression' WHERE task_id='first'")
        rows, _ = self.request("limit=1&before_id=3")
        self.assertEqual(rows[0]["task_id"], "second")
        rows, _ = self.request("limit=1&suite=daily_regression")
        self.assertEqual(rows[0]["task_id"], "first")

    def test_empty_database(self):
        rows, _ = self.request("limit=1")
        self.assertEqual(rows, [])

    def test_historical_examples_do_not_consume_query_budget(self):
        self.seed(("history_%d" % i, "success", ["success"] * 100)
                  for i in range(1000))
        self.seed([("latest", "running", ["running", "pending"])])
        rows, steps = self.request("limit=1", budget=25000)
        self.assertEqual(rows[0]["task_id"], "latest")
        self.assertEqual(rows[0]["real_total"], 2)
        self.assertLessEqual(steps, 25000)

    def test_http_query_does_not_require_worker_registration(self):
        self.seed([("latest", "running", ["running"])])
        server = scheduler.ThreadingHTTPServer(
            ("127.0.0.1", 0), scheduler.SchedulerHandler)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        with patch.object(scheduler, "get_conn", side_effect=self.connect), \
                patch.object(scheduler, "log_scheduler"):
            thread.start()
            try:
                url = "http://127.0.0.1:%d/api/tasks?limit=1" % server.server_port
                with build_opener(ProxyHandler({})).open(url, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))
                self.assertTrue(data["ok"])
                self.assertEqual(data["tasks"][0]["task_id"], "latest")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
