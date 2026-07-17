#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Normalize timeout rows whose final exit fields were inconsistent.

Equivalent SQL:

    UPDATE task_attempts
    SET exit_code = 124,
        raw_exit_code = 124,
        timed_out = 1
    WHERE status = 'timeout'
      AND (
          COALESCE(exit_code, -1) != 124
          OR COALESCE(raw_exit_code, -1) != 124
          OR COALESCE(timed_out, 0) != 1
      );

    UPDATE task_examples
    SET exit_code = 124,
        raw_exit_code = 124
    WHERE status = 'timeout'
      AND (
          COALESCE(exit_code, -1) != 124
          OR COALESCE(raw_exit_code, -1) != 124
      );
"""

import argparse
import sqlite3
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from config import get_path


DEFAULT_DB_PATH = get_path(
    "paths",
    "database",
    env_name="PJTEST_DB_PATH",
)


def count_bad_rows(cur, table_name):
    if table_name == "task_attempts":
        cur.execute(
            """
            SELECT COUNT(*)
            FROM task_attempts
            WHERE status = 'timeout'
              AND (
                  COALESCE(exit_code, -1) != 124
                  OR COALESCE(raw_exit_code, -1) != 124
                  OR COALESCE(timed_out, 0) != 1
              )
            """
        )
    elif table_name == "task_examples":
        cur.execute(
            """
            SELECT COUNT(*)
            FROM task_examples
            WHERE status = 'timeout'
              AND (
                  COALESCE(exit_code, -1) != 124
                  OR COALESCE(raw_exit_code, -1) != 124
              )
            """
        )
    else:
        raise ValueError("unsupported table: %s" % table_name)
    return int(cur.fetchone()[0] or 0)


def repair_table(cur, table_name):
    if table_name == "task_attempts":
        cur.execute(
            """
            UPDATE task_attempts
            SET exit_code = 124,
                raw_exit_code = 124,
                timed_out = 1
            WHERE status = 'timeout'
              AND (
                  COALESCE(exit_code, -1) != 124
                  OR COALESCE(raw_exit_code, -1) != 124
                  OR COALESCE(timed_out, 0) != 1
              )
            """
        )
    elif table_name == "task_examples":
        cur.execute(
            """
            UPDATE task_examples
            SET exit_code = 124,
                raw_exit_code = 124
            WHERE status = 'timeout'
              AND (
                  COALESCE(exit_code, -1) != 124
                  OR COALESCE(raw_exit_code, -1) != 124
              )
            """
        )
    else:
        raise ValueError("unsupported table: %s" % table_name)
    return cur.rowcount


def main():
    parser = argparse.ArgumentParser(
        description="Normalize PJTest timeout rows to exit_code=124/raw_exit_code=124."
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="SQLite database path, default from PJTest config/env",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="only print matching row counts; do not update the database",
    )
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        attempts = count_bad_rows(cur, "task_attempts")
        examples = count_bad_rows(cur, "task_examples")
        print("Database      : %s" % db_path)
        print("Bad attempts  : %d" % attempts)
        print("Bad examples  : %d" % examples)

        if args.dry_run:
            print("Dry run only; no rows updated.")
            return 0

        cur.execute("BEGIN IMMEDIATE")
        fixed_attempts = repair_table(cur, "task_attempts")
        fixed_examples = repair_table(cur, "task_examples")
        conn.commit()

        print("Fixed attempts: %d" % fixed_attempts)
        print("Fixed examples: %d" % fixed_examples)
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
