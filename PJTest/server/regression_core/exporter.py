#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export PJTest regression summaries as atomically replaced text files."""

import csv
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .analyzer import (
    DEFAULT_REGRESSION_SUITE,
    analyze_observations,
    load_observations,
)


TSV_COLUMNS = [
    "CASE_PATH",
    "TEMPLATE",
    "LAST_GOOD",
    "FIRST_BAD",
    "LAST_BAD",
    "FIRST_FIXED",
    "LATEST_REVISION",
    "LATEST_STATUS",
    "UPDATED_AT",
]

LOCK_STALE_SECONDS = 3600


def _display(value):
    return "" if value is None else str(value)


@contextmanager
def export_lock(output_dir):
    """Prevent overlapping exporters without requiring platform file locks."""
    lock_path = output_dir / ".regression-export.lock"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(lock_path), flags)
    except FileExistsError:
        age = time.time() - lock_path.stat().st_mtime
        if age <= LOCK_STALE_SECONDS:
            raise RuntimeError("regression export already running: %s" % lock_path)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        fd = os.open(str(lock_path), flags)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("pid=%s\n" % os.getpid())
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path):
    if os.name == "nt":
        return
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_rows(path, delimiter, rows):
    """Write rows in the target directory and atomically replace the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".%s." % path.name,
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter=delimiter, lineterminator="\n")
            for row in rows:
                writer.writerow(row)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp_path), str(path))
        _fsync_directory(path.parent)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _full_rows(cases):
    yield TSV_COLUMNS
    for item in cases:
        yield [
            item.case_path,
            item.template_name,
            _display(item.last_good),
            _display(item.first_bad),
            _display(item.last_bad),
            _display(item.first_fixed),
            item.latest_revision,
            item.latest_status,
            item.updated_at,
        ]


def _summary_rows(cases):
    yield ["CASE_PATH", "TEMPLATE", "S_VERSION", "F_VERSION"]
    for item in cases:
        success_version = "s%s" % item.last_good if item.last_good is not None else "s-"
        fail_version = "f%s" % item.first_bad if item.first_bad is not None else "f-"
        yield [
            item.case_path,
            item.template_name,
            success_version,
            fail_version,
        ]


def export_regression_reports(
    db_path,
    output_dir,
    connect_timeout_sec=60,
    busy_timeout_ms=60000,
    suite=DEFAULT_REGRESSION_SUITE,
):
    """Analyze the current database and generate both phase-one reports."""
    db_path = Path(db_path)
    output_dir = Path(output_dir)
    if not db_path.is_file():
        raise FileNotFoundError("PJTest database not found: %s" % db_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    full_path = output_dir / "regression_cases.tsv"
    summary_path = output_dir / "regression_summary.txt"

    with export_lock(output_dir):
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(
            uri,
            uri=True,
            timeout=max(1, int(connect_timeout_sec)),
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=%d" % max(1, int(busy_timeout_ms)))
        try:
            observations, stats = load_observations(conn, suite=suite)
        finally:
            conn.close()

        cases = analyze_observations(observations)
        _atomic_write_rows(full_path, "\t", _full_rows(cases))
        _atomic_write_rows(summary_path, "\t", _summary_rows(cases))

    result = dict(stats)
    result.update({
        "case_count": len(cases),
        "suite": suite,
        "full_path": str(full_path),
        "summary_path": str(summary_path),
    })
    return result
