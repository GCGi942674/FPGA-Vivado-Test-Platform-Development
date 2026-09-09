"""Regression cache, read-only HTTP, snapshot and concurrent-reader tests."""

import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from database.init_db import create_task_attempts, create_task_examples, create_tasks
from regression_core.service import RegressionService, ViewError
from scheduler_core import main as scheduler


def create_source(path):
    conn = sqlite3.connect(str(path))
    try:
        for create in (create_tasks, create_task_examples, create_task_attempts):
            create(conn.cursor())
        conn.execute("CREATE INDEX idx_examples_task_status ON task_examples(task_id,status)")
        conn.execute("CREATE INDEX idx_attempts_example ON task_attempts(example_id)")
        conn.commit()
    finally:
        conn.close()


def add_task(path, name, day, revision, items, template="place_design",
             status="failed", suite="daily_regression"):
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("INSERT INTO tasks(task_id,template_name,revision,status,suite,work_root,"
                     "target_dir,created_at,updated_at,total_examples) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (name, template, str(revision), status, suite, "/work/test2", "runlist",
                      day + " 19:00:00", day + " 22:00:00", len(items)))
        for i, (case, result) in enumerate(items):
            conn.execute("INSERT INTO task_examples(example_id,task_id,seq,target_arg,run_tcl_path,"
                         "cmd,status,revision,created_at,finished_at,assigned_worker,failed_reason,"
                         "log_file,log_tail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (name + "_" + str(i), name, i, case, case + "/run.tcl", "run.sh",
                          result, str(revision), day, day + " 22:00:00", "worker-demo-02",
                          "SAMPLE: stage assertion" if result == "failed" else "",
                          "/report/demo/%s/%s.log" % (name, i), "SAMPLE LOG: " + result))
        conn.commit()
    finally:
        conn.close()


def seed_demo(path):
    create_source(path)
    add_task(path, "old", "2026-09-06", 18237, [("memory/ddr_bridge", "success")])
    add_task(path, "previous", "2026-09-07", 18267,
             [("dsp/fir_pipeline", "success"), ("control/uart_controller", "failed"),
              ("memory/ddr_bridge", "failed"), ("system/soc_top", "success"),
              ("control/counter", "success"), ("video/framebuffer", "success")])
    add_task(path, "latest", "2026-09-08", 18300,
             [("dsp/fir_pipeline", "failed"), ("control/uart_controller", "success"),
              ("memory/ddr_bridge", "failed"), ("system/soc_top", "success"),
              ("control/counter", "success"), ("video/framebuffer", "timeout"),
              ("memory/new_controller", "failed")])
    conn = sqlite3.connect(str(path))
    try:
        for i, status in enumerate(("failed", "success")):
            conn.execute("INSERT INTO task_attempts(attempt_id,example_id,task_id,attempt_no,"
                         "worker_name,status,revision,started_at,log_tail) VALUES(?,?,?,?,?,?,?,?,?)",
                         ("retry_" + str(i), "latest_3", "latest", i + 1, "worker-demo-02",
                          status, "18300", "2026-09-08", "SAMPLE RETRY: " + status))
        conn.commit()
    finally:
        conn.close()
    add_task(path, "manual", "2026-09-09", 18350, [("manual_only", "failed")], suite="manual")


class RegressionViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "tasks.db"
        seed_demo(self.source)
        self.service = RegressionService(self.source)
        self.addCleanup(self.service.close)
        self.service.refresh()

    def test_categories_and_retry_history(self):
        cases = self.service.cases({})
        by_path = {row["case_path"]: row for row in cases["items"]}
        expected = {"dsp/fir_pipeline": "NEW", "control/uart_controller": "RECOVERED",
                    "memory/ddr_bridge": "OPEN", "system/soc_top": "FLAKY",
                    "control/counter": "STABLE", "video/framebuffer": "INCONCLUSIVE",
                    "memory/new_controller": "NO_BASELINE"}
        self.assertEqual(cases["total"], len(expected))
        for path, category in expected.items():
            self.assertEqual(by_path[path + "/run.tcl"]["category"], category)
        self.assertEqual(self.service.status()["dates"], ["2026-09-08", "2026-09-07"])
        self.assertEqual(self.service.status()["latest_examples"], 7)
        key = by_path["dsp/fir_pipeline/run.tcl"]["test_key"]
        history = self.service.history({"test_key": key})
        self.assertEqual([row["revision"] for row in history["items"]], ["18300", "18267"])
        self.assertEqual(len(self.service.evidence({"example_id": "latest_3"})["attempts"]), 2)

    def test_same_revision_conflict_remains_flaky(self):
        add_task(self.source, "same_revision", "2026-09-09", 18267,
                 [("control/counter", "failed")])
        self.service.refresh()
        row = self.service.cases({"q": "control/counter"})["items"][0]
        # The latest numeric revision still passes; the same-revision conflict
        # must remain visible in historical observations, not become environment noise.
        conn = self.service.connect()
        try:
            statuses = [json.loads(r[0])["status"] for r in conn.execute(
                "SELECT data FROM samples WHERE test_key=? AND revision=18267", (row["test_key"],))]
        finally:
            conn.close()
        self.assertEqual(set(statuses), {"PASS", "FAIL"})
        add_task(self.source, "same_latest_revision", "2026-09-10", 18300,
                 [("control/counter", "failed")])
        self.service.refresh()
        self.assertEqual(self.service.cases({"q": "control/counter"})["items"][0]["category"], "FLAKY")

    def test_filters_paging_export_and_literal_search(self):
        first = self.service.cases({"limit": 2})
        second = self.service.cases({"limit": 2, "offset": 2})
        self.assertFalse({r["test_key"] for r in first["items"]}
                         & {r["test_key"] for r in second["items"]})
        self.assertEqual(self.service.cases({"category": "NEW"})["total"], 1)
        self.assertEqual(self.service.cases({"template": "no-such-template"})["total"], 0)
        self.assertEqual(self.service.cases({"q": "%"})["total"], 0)
        self.assertEqual(self.service.cases({"revision": 18300})["total"], 7)
        export = self.service.export({"limit": 1})
        self.assertIn("Rows: 7", export)
        self.assertIn("dsp/fir_pipeline/run.tcl", export)
        self.assertNotIn("manual_only", export)

    def test_refresh_is_incremental_and_source_is_unchanged(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with patch("regression_core.service.load_observations", side_effect=AssertionError("unneeded rescan")):
            self.service.refresh()
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(self.service.cases({})["total"], 7)

    def test_atomic_rollback_and_generation_guard(self):
        previous = self.service.status()["generation"]
        add_task(self.source, "more", "2026-09-09", 18301, [("another_case", "failed")])
        with patch("regression_core.service.load_observations", side_effect=RuntimeError("failed import")):
            with self.assertRaises(RuntimeError):
                self.service.refresh()
        self.assertEqual(self.service.status()["generation"], previous)
        self.assertEqual(self.service.cases({})["total"], 7)
        self.service.refresh()
        with self.assertRaises(ViewError) as error:
            self.service.cases({"generation": previous})
        self.assertEqual(error.exception.status, 409)

    def test_unfinished_task_is_visible_without_claiming_complete_batch(self):
        add_task(self.source, "running", "2026-09-09", 18301,
                 [("pending_case", "pending"), ("running_case", "running")], status="running")
        self.service.refresh()
        meta = self.service.status()
        self.assertEqual(meta["latest_tasks_done"], 0)
        self.assertEqual(meta["latest_examples"], 2)
        self.assertEqual(meta["completeness"], "submitted_tasks_only")
        self.assertEqual(self.service.cases({"category": "INCOMPLETE"})["total"], 2)

    def test_readers_keep_old_snapshot_during_cache_write(self):
        writer = self.service.connect()
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("DELETE FROM cases")
            self.assertEqual(self.service.cases({})["total"], 7)
        finally:
            writer.close()

    def test_only_one_builder_per_service(self):
        self.service.refresh_lock.acquire()
        try:
            self.assertFalse(self.service.refresh())
        finally:
            self.service.refresh_lock.release()

    def test_http_concurrent_reads_and_no_write_routes(self):
        server = scheduler.ThreadingHTTPServer(("127.0.0.1", 0), scheduler.SchedulerHandler)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        with patch.object(scheduler, "get_regression_service", return_value=self.service), \
                patch.object(scheduler, "log_scheduler"):
            thread.start()
            try:
                base = "http://127.0.0.1:%d/api/regression/" % server.server_port
                def get(_):
                    with build_opener(ProxyHandler({})).open(base + "cases?limit=2", timeout=5) as response:
                        return json.loads(response.read().decode("utf-8"))
                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(get, range(12)))
                self.assertTrue(all(row["total"] == 7 and len(row["items"]) == 2 for row in results))
                with self.assertRaises(HTTPError) as error:
                    build_opener(ProxyHandler({})).open(Request(base + "cases", data=b"{}"), timeout=5)
                self.assertEqual(error.exception.code, 404)
                with build_opener(ProxyHandler({})).open(base + "export?category=NEW", timeout=5) as response:
                    text = response.read().decode("utf-8-sig")
                self.assertIn("Rows: 1", text)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
