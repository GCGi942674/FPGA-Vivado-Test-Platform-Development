#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repair timeout rows whose final exit_code was overwritten by result.env.

Equivalent SQL:

    UPDATE task_attempts
    SET exit_code = 124
    WHERE status = 'timeout'
      AND exit_code = 130
      AND raw_exit_code = 124;

    UPDATE task_examples
    SET exit_code = 124
    WHERE status = 'timeout'
      AND exit_code = 130
      AND raw_exit_code = 124;
"""

import argparse
import sqlite3
from pathlib import Path

from config import get_path


DEFAULT_DB_PATH = get_path(
    "paths",
    "database",
    env_name="PJTEST_DB_PATH",
)


def count_bad_rows(cur, table_name):
    cur.execute(
        """
        SELECT COUNT(*)
        FROM %s
        WHERE status = 'timeout'
          AND exit_code = 130
          AND raw_exit_code = 124
        """ % table_name
    )
    return int(cur.fetchone()[0] or 0)


def repair_table(cur, table_name):
    cur.execute(
        """
        UPDATE %s
        SET exit_code = 124
        WHERE status = 'timeout'
          AND exit_code = 130
          AND raw_exit_code = 124
        """ % table_name
    )
    return cur.rowcount


def main():
    parser = argparse.ArgumentParser(
        description="Repair PJTest timeout rows with exit_code=130/raw_exit_code=124."
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
