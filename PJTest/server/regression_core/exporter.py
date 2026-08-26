#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export PJTest regression summaries as atomically replaced text files."""

import csv
import os
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .analyzer import (
    DEFAULT_REGRESSION_SUITE,
    analyze_latest_nightly_regressions,
    analyze_observations,
    load_observations,
)


LOCK_STALE_SECONDS = 3600
SUMMARY_PREFIX = "Regression_Summary_"
NIGHTLY_REGRESSION_FILENAME = "Regression.txt"
LEGACY_FILENAMES = ("regression_cases.tsv", "regression_summary.txt")


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


def _summary_rows(cases):
    yield ["CASE_PATH", "S_VERSION", "F_VERSION"]
    for item in cases:
        success_version = "s%s" % item.last_good if item.last_good is not None else "s-"
        fail_version = "f%s" % item.first_bad if item.first_bad is not None else "f-"
        yield [
            item.case_path,
            success_version,
            fail_version,
        ]


def _nightly_regression_rows(regressions):
    yield ["CASE_PATH", "TEMPLATE", "S_VERSION", "F_VERSION"]
    for item in regressions:
        yield [
            item.case_path,
            item.template_name,
            "s%s" % item.success_revision,
            "f%s" % item.fail_revision,
        ]


def _summary_filename(template_name):
    """Return a safe and predictable per-template summary filename."""
    component = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        str(template_name or "").strip(),
    ).strip("._-")
    if not component:
        component = "unknown"
    return "%s%s.txt" % (SUMMARY_PREFIX, component)


def _group_cases_by_template(cases):
    grouped = {}
    filename_owners = {}
    for item in cases:
        template_name = str(item.template_name or "")
        filename = _summary_filename(template_name)
        owner = filename_owners.get(filename.lower())
        if owner is not None and owner != template_name:
            raise ValueError(
                "template names map to the same summary file: %r and %r"
                % (owner, template_name)
            )
        filename_owners[filename.lower()] = template_name
        grouped.setdefault(template_name, []).append(item)
    return grouped


def _remove_obsolete_outputs(output_dir, expected_names):
    """Remove legacy and stale module summaries after new files are published."""
    removed = []
    candidates = [output_dir / name for name in LEGACY_FILENAMES]
    candidates.extend(output_dir.glob("%s*.txt" % SUMMARY_PREFIX))
    for path in candidates:
        if path.name in expected_names or not path.is_file():
            continue
        path.unlink()
        removed.append(str(path))
    if removed:
        _fsync_directory(output_dir)
    return removed


def export_regression_reports(
    db_path,
    output_dir,
    connect_timeout_sec=60,
    busy_timeout_ms=60000,
    suite=DEFAULT_REGRESSION_SUITE,
):
    """Analyze daily results and generate one compact summary per template."""
    db_path = Path(db_path)
    output_dir = Path(output_dir)
    if not db_path.is_file():
        raise FileNotFoundError("PJTest database not found: %s" % db_path)

    output_dir.mkdir(parents=True, exist_ok=True)
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
        nightly_regressions, previous_run_date, current_run_date = (
            analyze_latest_nightly_regressions(observations)
        )
        grouped = _group_cases_by_template(cases)
        summary_paths = []
        expected_names = set()
        for template_name in sorted(grouped):
            filename = _summary_filename(template_name)
            path = output_dir / filename
            _atomic_write_rows(path, "\t", _summary_rows(grouped[template_name]))
            summary_paths.append(str(path))
            expected_names.add(filename)
        nightly_regression_path = output_dir / NIGHTLY_REGRESSION_FILENAME
        _atomic_write_rows(
            nightly_regression_path,
            "\t",
            _nightly_regression_rows(nightly_regressions),
        )
        removed_paths = _remove_obsolete_outputs(output_dir, expected_names)

    result = dict(stats)
    result.update({
        "case_count": len(cases),
        "module_count": len(grouped),
        "nightly_regression_count": len(nightly_regressions),
        "previous_run_date": previous_run_date,
        "current_run_date": current_run_date,
        "nightly_regression_path": str(nightly_regression_path),
        "suite": suite,
        "summary_paths": summary_paths,
        "removed_paths": removed_paths,
    })
    return result
