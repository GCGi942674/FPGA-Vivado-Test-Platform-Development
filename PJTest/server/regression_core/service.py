"""Shared read-only regression view; source scheduler data is never modified."""

import csv
import hashlib
import io
import json
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .analyzer import (Observation, aggregate_revision_status, analyze_observations,
                       load_observations)
from .identity import build_test_identity

TASK_FIELDS = ("id", "task_id", "template_name", "revision", "status", "work_root",
               "flow_config_json", "created_at", "updated_at", "finished_at", "total_examples")
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_cache (
 task_id TEXT PRIMARY KEY, fingerprint TEXT, run_date TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS history (
 example_id TEXT PRIMARY KEY, task_id TEXT, test_key TEXT, run_date TEXT,
 task_order INTEGER, seq INTEGER, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS history_task ON history(task_id);
CREATE INDEX IF NOT EXISTS history_case ON history(test_key, run_date, task_order, seq);
CREATE TABLE IF NOT EXISTS samples (
 task_id TEXT, test_key TEXT, revision INTEGER, run_date TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS samples_task ON samples(task_id);
CREATE INDEX IF NOT EXISTS samples_case ON samples(test_key, revision);
CREATE TABLE IF NOT EXISTS cases (
 test_key TEXT PRIMARY KEY, case_path TEXT, template TEXT, category TEXT,
 latest_revision INTEGER, updated_at TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cases_filter ON cases(category, template, updated_at);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ViewError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class RegressionService:
    """One incremental builder and concurrent readers of a separate WAL cache."""

    def __init__(self, source_path, cache_path=None, refresh_seconds=60):
        self.source_path = Path(source_path)
        self.cache_path = Path(cache_path or self.source_path.with_name("regression_view.db"))
        if self.cache_path.resolve() == self.source_path.resolve():
            raise ValueError("regression cache must differ from source database")
        self.refresh_seconds = max(10, refresh_seconds)
        self.stop_event = threading.Event()
        self.refresh_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.thread = None
        self.syncing = False
        self.error = ""
        self.processed_tasks = 0
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def connect(self):
        conn = sqlite3.connect(str(self.cache_path), timeout=2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=2000")
        return conn

    def start(self):
        with self.state_lock:
            if self.thread is not None:
                return
            self.thread = threading.Thread(target=self._loop, name="regression-view")
            self.thread.daemon = True
            self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.refresh()
            except Exception as exc:
                with self.state_lock:
                    self.error = "%s: %s" % (type(exc).__name__, exc)
            self.stop_event.wait(self.refresh_seconds)

    def _source(self):
        conn = sqlite3.connect(self.source_path.resolve().as_uri() + "?mode=ro",
                               uri=True, timeout=2, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=2000")
        return conn

    def refresh(self):
        if not self.refresh_lock.acquire(False):
            return False
        with self.state_lock:
            self.syncing, self.processed_tasks = True, 0
        source = cache = None
        try:
            source, cache = self._source(), self.connect()
            # This transaction locks only the derived cache. Source transactions
            # are short, one task at a time; readers retain the previous cache view.
            cache.execute("BEGIN IMMEDIATE")
            tasks = source.execute(
                "SELECT %s FROM tasks WHERE suite=? ORDER BY id" % ",".join(TASK_FIELDS),
                ("daily_regression",),
            ).fetchall()
            seen, changed = set(), set()
            previous_days = self._days(cache)
            for index, initial in enumerate(tasks):
                if self.stop_event.is_set():
                    raise RuntimeError("regression refresh stopped")
                task_id = initial["task_id"]
                seen.add(task_id)
                previous = cache.execute("SELECT fingerprint FROM task_cache WHERE task_id=?",
                                         (task_id,)).fetchone()
                fingerprint = hashlib.sha256(encode(dict(initial)).encode("utf-8")).hexdigest()
                if (previous and previous[0] == fingerprint
                        and initial["status"] in ("success", "failed", "canceled")):
                    continue
                source.execute("BEGIN")
                # Do not let a large source task monopolize the reader indefinitely.
                deadline = time.monotonic() + 15
                source.set_progress_handler(
                    lambda: int(self.stop_event.is_set() or time.monotonic() > deadline), 10000)
                try:
                    task_row = source.execute(
                        "SELECT %s FROM tasks WHERE task_id=? AND suite=?"
                        % ",".join(TASK_FIELDS), (task_id, "daily_regression"),
                    ).fetchone()
                    if task_row is None:
                        seen.discard(task_id)
                        continue
                    task = dict(task_row)
                    fingerprint = hashlib.sha256(encode(task).encode("utf-8")).hexdigest()
                    examples = source.execute(
                        "SELECT example_id, run_tcl_path, target_arg, status, revision, "
                        "infra_reason, assigned_worker, finished_at, updated_at, failed_reason, "
                        "log_file, report_dir, run_log_dir, exit_code, seq "
                        "FROM task_examples WHERE task_id=? ORDER BY seq", (task_id,),
                    ).fetchall()
                    observations, _ = load_observations(source, task_ids=[task_id])
                finally:
                    source.rollback()
                    source.set_progress_handler(None, 0)
                changed.update(row[0] for row in cache.execute(
                    "SELECT DISTINCT test_key FROM history WHERE task_id=?", (task_id,)))
                cache.execute("DELETE FROM history WHERE task_id=?", (task_id,))
                cache.execute("DELETE FROM samples WHERE task_id=?", (task_id,))
                run_date = str(task["created_at"] or "")[:10]
                counts = defaultdict(int)
                for row in examples:
                    item = dict(row)
                    key, config_hash, case_path, _ = build_test_identity(
                        task["work_root"], item["run_tcl_path"], task["template_name"],
                        task["flow_config_json"],
                    )
                    changed.add(key)
                    counts[item["status"]] += 1
                    item.update(test_key=key, config_hash=config_hash, case_path=case_path,
                                task_id=task_id, template_name=task["template_name"],
                                revision=item["revision"] or task["revision"],
                                run_date=run_date, task_status=task["status"])
                    item["failed_reason"] = str(item["failed_reason"] or "")[:4096]
                    cache.execute("INSERT INTO history VALUES(?,?,?,?,?,?,?)",
                                  (item["example_id"], task_id, key, run_date,
                                   task["id"], item["seq"], encode(item)))
                cache.executemany("INSERT INTO samples VALUES(?,?,?,?,?)",
                                  ((task_id, item.test_key, item.revision, item.run_date,
                                    encode(item._asdict())) for item in observations))
                task["counts"] = dict(counts)
                task["real_total"] = len(examples)
                cache.execute("INSERT OR REPLACE INTO task_cache VALUES(?,?,?,?)",
                              (task_id, fingerprint, run_date, encode(task)))
                with self.state_lock:
                    self.processed_tasks = index + 1
                # Yield between source tasks; no source database lock is held here.
                self.stop_event.wait(0.005)
            for row in cache.execute("SELECT task_id FROM task_cache").fetchall():
                if row[0] not in seen:
                    changed.update(item[0] for item in cache.execute(
                        "SELECT DISTINCT test_key FROM history WHERE task_id=?", (row[0],)))
                    for table in ("history", "samples", "task_cache"):
                        cache.execute("DELETE FROM %s WHERE task_id=?" % table, (row[0],))
            days = self._days(cache)
            if days != previous_days:
                changed.update(row[0] for row in cache.execute("SELECT DISTINCT test_key FROM history"))
            for key in changed:
                self._update_case(cache, key, days)
            old = self._meta(cache)
            metadata = self._metadata(cache, days)
            has_changes = bool(changed) or not old.get("ready") or any(
                old.get(key) != value for key, value in metadata.items())
            metadata.update(generation=old.get("generation", 0) + int(has_changes),
                            updated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ready=True)
            cache.execute("INSERT OR REPLACE INTO meta VALUES('snapshot',?)", (encode(metadata),))
            cache.commit()
            with self.state_lock:
                self.error = ""
            return True
        finally:
            if source:
                source.close()
            if cache:
                cache.close()
            with self.state_lock:
                self.syncing = False
            self.refresh_lock.release()

    @staticmethod
    def _days(conn):
        return [row[0] for row in conn.execute(
            "SELECT DISTINCT run_date FROM task_cache WHERE run_date<>'' "
            "ORDER BY run_date DESC LIMIT 2")]

    @staticmethod
    def _meta(conn):
        row = conn.execute("SELECT value FROM meta WHERE key='snapshot'").fetchone()
        return json.loads(row[0]) if row else {"ready": False, "generation": 0}

    def _update_case(self, conn, key, days):
        row = conn.execute("SELECT data FROM history WHERE test_key=? "
                           "ORDER BY run_date DESC, task_order DESC, seq DESC LIMIT 1",
                           (key,)).fetchone()
        if row is None:
            conn.execute("DELETE FROM cases WHERE test_key=?", (key,))
            return
        latest = json.loads(row[0])
        observations = [Observation(**json.loads(row[0])) for row in conn.execute(
            "SELECT data FROM samples WHERE test_key=? ORDER BY revision", (key,))]
        summaries = analyze_observations(observations)
        if summaries:
            data = summaries[0]._asdict()
            category = data["state"]
            by_day = defaultdict(list)
            for item in observations:
                by_day[item.run_date].append(item)
            if len(days) == 2:
                current, previous = by_day[days[0]], by_day[days[1]]
                comparable = (current and previous and len(set(x.revision for x in current)) == 1
                              and len(set(x.revision for x in previous)) == 1)
                if comparable:
                    before = aggregate_revision_status(x.status for x in previous)
                    after = aggregate_revision_status(x.status for x in current)
                    if category == "OPEN" and before == "PASS" and after == "FAIL":
                        category = "NEW"
                    elif category == "FIXED" and before == "FAIL" and after == "PASS":
                        category = "RECOVERED"
        else:
            data = dict(test_key=key, case_path=latest["case_path"],
                        template_name=latest["template_name"], config_hash=latest["config_hash"],
                        state="UNKNOWN", last_good=None, first_bad=None, last_bad=None,
                        first_fixed=None, latest_revision=None, latest_status="UNKNOWN",
                        severity=0, updated_at="")
            category = "INCOMPLETE"
        data.update(category=category, last_run_date=latest["run_date"],
                    last_task_id=latest["task_id"], current_status=latest["status"],
                    current_task_status=latest["task_status"], issue_id=key[:12])
        conn.execute("INSERT OR REPLACE INTO cases VALUES(?,?,?,?,?,?,?)",
                     (key, data["case_path"], data["template_name"], category,
                      data["latest_revision"], data["updated_at"], encode(data)))

    @staticmethod
    def _metadata(conn, days):
        counts = {row[0]: row[1] for row in conn.execute(
            "SELECT category, COUNT(*) FROM cases GROUP BY category")}
        latest_tasks = [json.loads(row[0]) for row in conn.execute(
            "SELECT data FROM task_cache WHERE run_date=?", (days[0] if days else "",))]
        done_statuses = ("success", "failed", "timeout", "canceled")
        return dict(categories=counts, total_cases=sum(counts.values()), dates=days,
                    templates=[row[0] for row in conn.execute(
                        "SELECT DISTINCT template FROM cases ORDER BY template")],
                    task_count=conn.execute("SELECT COUNT(*) FROM task_cache").fetchone()[0],
                    latest_tasks=len(latest_tasks),
                    latest_tasks_done=sum(t["status"] in ("success", "failed", "canceled")
                                          for t in latest_tasks),
                    latest_examples=sum(t["real_total"] for t in latest_tasks),
                    latest_done=sum(t["counts"].get(s, 0) for t in latest_tasks for s in done_statuses),
                    grouping="submission_date", completeness="submitted_tasks_only")

    @contextmanager
    def snapshot(self, generation=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            meta = self._meta(conn)
            if generation not in (None, "") and int(generation) != meta["generation"]:
                raise ViewError("数据已刷新，请重新加载列表。", 409)
            yield conn, meta
        finally:
            conn.close()

    def status(self):
        with self.snapshot() as (_, meta):
            result = dict(meta)
        with self.state_lock:
            result.update(syncing=self.syncing, error=self.error,
                          processed_tasks=self.processed_tasks)
        return result

    @staticmethod
    def _filter(query):
        clauses, args = [], []
        for field, column in (("category", "category"), ("template", "template"),
                              ("revision", "latest_revision")):
            if query.get(field):
                clauses.append(column + "=?")
                args.append(query[field])
        if query.get("q"):
            word = str(query["q"])[:200].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(case_path LIKE ? ESCAPE '\\' OR test_key LIKE ? ESCAPE '\\')")
            args.extend(["%" + word + "%"] * 2)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), args

    def cases(self, query):
        limit = max(1, min(200, int(query.get("limit", 100))))
        offset = max(0, int(query.get("offset", 0)))
        where, args = self._filter(query)
        with self.snapshot(query.get("generation")) as (conn, meta):
            count = conn.execute("SELECT COUNT(*) FROM cases" + where, args).fetchone()[0]
            rows = conn.execute("SELECT data FROM cases" + where
                                + " ORDER BY updated_at DESC, test_key LIMIT ? OFFSET ?",
                                args + [limit, offset])
            return dict(items=[json.loads(row[0]) for row in rows], total=count,
                        offset=offset, limit=limit, generation=meta["generation"], ready=meta["ready"])

    def history(self, query):
        key = query.get("test_key", "")
        if not key:
            raise ViewError("test_key is required")
        limit = max(1, min(200, int(query.get("limit", 50))))
        offset = max(0, int(query.get("offset", 0)))
        with self.snapshot(query.get("generation")) as (conn, meta):
            count = conn.execute("SELECT COUNT(*) FROM history WHERE test_key=?", (key,)).fetchone()[0]
            rows = conn.execute("SELECT data FROM history WHERE test_key=? "
                                "ORDER BY run_date DESC, task_order DESC, seq DESC LIMIT ? OFFSET ?",
                                (key, limit, offset))
            return dict(items=[json.loads(row[0]) for row in rows], total=count,
                        generation=meta["generation"], offset=offset, limit=limit)

    def export(self, query):
        where, args = self._filter(query)
        with self.snapshot(query.get("generation")) as (conn, meta):
            count = conn.execute("SELECT COUNT(*) FROM cases" + where, args).fetchone()[0]
            if count > 100000:
                raise ViewError("导出超过 100000 行，请先缩小筛选范围。", 413)
            stream = io.StringIO()
            stream.write("PJTEST REGRESSION\nSnapshot: %s\nRows: %d\n\n" %
                         (meta.get("updated_at", "not ready"), count))
            writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
            fields = ("test_key", "template_name", "case_path", "category", "last_good",
                      "first_bad", "latest_revision", "latest_status", "last_run_date")
            writer.writerow([field.upper() for field in fields])
            for row in conn.execute("SELECT data FROM cases" + where + " ORDER BY test_key", args):
                data = json.loads(row[0])
                writer.writerow(["-" if data.get(field) is None else data[field] for field in fields])
            return stream.getvalue()

    def evidence(self, query):
        example_id = query.get("example_id", "")
        if not example_id:
            raise ViewError("example_id is required")
        conn = self._source()
        try:
            deadline = time.monotonic() + 5
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT e.example_id, e.status, e.exit_code, e.log_file, e.report_dir, "
                "substr(e.failed_reason,1,4096) AS failed_reason, "
                "substr(e.log_tail,-16000) AS log_tail "
                "FROM task_examples e JOIN tasks t ON t.task_id=e.task_id "
                "WHERE e.example_id=? AND t.suite=?", (example_id, "daily_regression"),
            ).fetchone()
            if row is None:
                raise ViewError("该 nightly 用例记录不存在。", 404)
            attempts = conn.execute(
                "SELECT attempt_id, attempt_no, worker_name, revision, status, exit_code, "
                "infra_reason, log_file, substr(log_tail,-4000) AS log_tail "
                "FROM task_attempts WHERE example_id=? ORDER BY id DESC LIMIT 5", (example_id,),
            ).fetchall()
            return {"example": dict(row), "attempts": [dict(item) for item in attempts]}
        finally:
            conn.close()
