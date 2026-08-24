#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Focused phase-one tests for regression identity, analysis, and export."""

import csv
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from regression_core.analyzer import (  # noqa: E402
    Observation,
    aggregate_revision_status,
    analyze_observations,
)
from regression_core.exporter import export_regression_reports  # noqa: E402
from regression_core.identity import build_test_identity  # noqa: E402


def observation(test_key, revision, status, path="case/run.tcl", template="route"):
    return Observation(
        test_key=test_key,
        case_path=path,
        template_name=template,
        config_hash="cfg",
        revision=revision,
        status=status,
        updated_at="2026-08-24 10:%02d:00" % (revision % 60),
    )


class IdentityTests(unittest.TestCase):
    def test_dynamic_config_values_do_not_change_identity(self):
        first = build_test_identity(
            "/work/test2",
            r"kintex\demo\run.tcl",
            "route",
            '{"route_design":1,"task_id":"task_a","nested":{"log_path":"a"}}',
        )
        second = build_test_identity(
            "/work/test2/",
            "kintex/demo/run.tcl",
            "route",
            '{"nested":{"log_path":"b"},"task_id":"task_b","route_design":1}',
        )
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[2], "kintex/demo/run.tcl")


class AnalyzerTests(unittest.TestCase):
    def test_revision_aggregation_detects_conflicts(self):
        self.assertEqual(aggregate_revision_status(["PASS", "PASS"]), "PASS")
        self.assertEqual(aggregate_revision_status(["PASS", "FAIL"]), "FLAKY")
        self.assertEqual(
            aggregate_revision_status(["FAIL", "TIMEOUT"]),
            "INCONCLUSIVE",
        )
        self.assertEqual(
            aggregate_revision_status(["INFRA_ERROR"]),
            "INFRA_ERROR",
        )

    def test_open_fixed_no_baseline_and_flaky_states(self):
        rows = [
            observation("open", 100, "PASS", "open/run.tcl"),
            observation("open", 110, "FAIL", "open/run.tcl"),
            observation("open", 120, "FAIL", "open/run.tcl"),
            observation("fixed", 100, "PASS", "fixed/run.tcl"),
            observation("fixed", 110, "FAIL", "fixed/run.tcl"),
            observation("fixed", 120, "PASS", "fixed/run.tcl"),
            observation("baseline", 110, "FAIL", "baseline/run.tcl"),
            observation("flaky", 100, "PASS", "flaky/run.tcl"),
            observation("flaky", 110, "PASS", "flaky/run.tcl"),
            observation("flaky", 110, "FAIL", "flaky/run.tcl"),
        ]
        result = dict((item.test_key, item) for item in analyze_observations(rows))

        self.assertEqual(result["open"].state, "OPEN")
        self.assertEqual(result["open"].last_good, 100)
        self.assertEqual(result["open"].first_bad, 110)
        self.assertEqual(result["open"].last_bad, 120)

        self.assertEqual(result["fixed"].state, "FIXED")
        self.assertEqual(result["fixed"].first_fixed, 120)

        self.assertEqual(result["baseline"].state, "NO_BASELINE")
        self.assertIsNone(result["baseline"].last_good)

        self.assertEqual(result["flaky"].state, "FLAKY")
        self.assertEqual(result["flaky"].last_good, 100)

    def test_latest_regression_episode_replaces_an_older_fixed_episode(self):
        rows = [
            observation("repeat", 100, "PASS", "repeat/run.tcl"),
            observation("repeat", 110, "FAIL", "repeat/run.tcl"),
            observation("repeat", 120, "PASS", "repeat/run.tcl"),
            observation("repeat", 130, "PASS", "repeat/run.tcl"),
            observation("repeat", 140, "FAIL", "repeat/run.tcl"),
        ]
        item = analyze_observations(rows)[0]
        self.assertEqual(item.state, "OPEN")
        self.assertEqual(item.last_good, 130)
        self.assertEqual(item.first_bad, 140)
        self.assertEqual(item.last_bad, 140)
        self.assertIsNone(item.first_fixed)


class ExportTests(unittest.TestCase):
    def _create_database(self, path):
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY,
                task_id TEXT UNIQUE,
                template_name TEXT,
                revision TEXT,
                work_root TEXT,
                flow_config_json TEXT,
                status TEXT,
                created_at TEXT,
                updated_at TEXT,
                finished_at TEXT
            );
            CREATE TABLE task_examples (
                id INTEGER PRIMARY KEY,
                example_id TEXT UNIQUE,
                task_id TEXT,
                run_tcl_path TEXT,
                revision TEXT,
                status TEXT,
                infra_reason TEXT,
                updated_at TEXT,
                finished_at TEXT
            );
            CREATE TABLE task_attempts (
                id INTEGER PRIMARY KEY,
                attempt_id TEXT UNIQUE,
                example_id TEXT,
                status TEXT,
                revision TEXT,
                infra_reason TEXT,
                started_at TEXT,
                finished_at TEXT
            );
            """
        )
        tasks = [
            (1, "task_good", "route", "100", "/work/test2", "{}", "success"),
            (2, "task_bad", "route", "110", "/work/test2", "{}", "failed"),
            (3, "task_active", "route", "120", "/work/test2", "{}", "running"),
            (4, "task_text", "route", "latest", "/work/test2", "{}", "failed"),
            (5, "task_infra", "route", "120", "/work/test2", "{}", "failed"),
        ]
        for row in tasks:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, task_id, template_name, revision, work_root,
                    flow_config_json, status, created_at, updated_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '2026-08-24', '2026-08-24', '2026-08-24')
                """,
                row,
            )
        examples = [
            (1, "ex_good", "task_good", "case/run.tcl", None, "success", None),
            (2, "ex_bad", "task_bad", "case/run.tcl", None, "failed", None),
            (3, "ex_active", "task_active", "case/run.tcl", None, "failed", None),
            (4, "ex_text", "task_text", "other/run.tcl", None, "failed", None),
            (5, "ex_infra", "task_infra", "case/run.tcl", None, "failed", "slot_busy"),
        ]
        for row in examples:
            conn.execute(
                """
                INSERT INTO task_examples (
                    id, example_id, task_id, run_tcl_path, revision, status,
                    infra_reason, updated_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '2026-08-24', '2026-08-24')
                """,
                row,
            )
        conn.commit()
        conn.close()

    def test_export_uses_terminal_numeric_results_and_writes_both_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "task_queue.db"
            out_dir = root / "out"
            self._create_database(db_path)

            result = export_regression_reports(db_path, out_dir)
            self.assertEqual(result["case_count"], 1)
            self.assertEqual(result["skipped_non_numeric_revision"], 1)

            with (out_dir / "regression_cases.tsv").open(
                "r", encoding="utf-8", newline=""
            ) as stream:
                rows = list(csv.DictReader(stream, delimiter="\t"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["STATE"], "INCONCLUSIVE")
            self.assertEqual(rows[0]["LAST_GOOD"], "100")
            self.assertEqual(rows[0]["FIRST_BAD"], "110")
            self.assertEqual(rows[0]["LATEST_STATUS"], "INFRA_ERROR")

            summary = (out_dir / "regression_summary.txt").read_text(
                encoding="utf-8"
            )
            self.assertIn("s100\tf110\tINCONCLUSIVE", summary)

    def test_attempt_conflict_is_exported_as_flaky(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "task_queue.db"
            out_dir = root / "out"
            self._create_database(db_path)
            conn = sqlite3.connect(str(db_path))
            conn.executemany(
                """
                INSERT INTO task_attempts (
                    id, attempt_id, example_id, status, revision, infra_reason,
                    started_at, finished_at
                ) VALUES (?, ?, 'ex_bad', ?, '110', NULL, '2026-08-24', '2026-08-24')
                """,
                [
                    (1, "att_fail", "failed"),
                    (2, "att_pass", "success"),
                ],
            )
            conn.commit()
            conn.close()

            export_regression_reports(db_path, out_dir)
            with (out_dir / "regression_cases.tsv").open(
                "r", encoding="utf-8", newline=""
            ) as stream:
                rows = list(csv.DictReader(stream, delimiter="\t"))
            self.assertEqual(rows[0]["STATE"], "INCONCLUSIVE")
            self.assertEqual(rows[0]["LATEST_STATUS"], "INFRA_ERROR")

            # Remove the later infrastructure-only revision so r110 becomes latest.
            conn = sqlite3.connect(str(db_path))
            conn.execute("DELETE FROM task_examples WHERE example_id = 'ex_infra'")
            conn.execute("DELETE FROM tasks WHERE task_id = 'task_infra'")
            conn.commit()
            conn.close()
            export_regression_reports(db_path, out_dir)
            with (out_dir / "regression_cases.tsv").open(
                "r", encoding="utf-8", newline=""
            ) as stream:
                rows = list(csv.DictReader(stream, delimiter="\t"))
            self.assertEqual(rows[0]["STATE"], "FLAKY")
            self.assertEqual(rows[0]["LATEST_STATUS"], "FLAKY")


if __name__ == "__main__":
    unittest.main()
