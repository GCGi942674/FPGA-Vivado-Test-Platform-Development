#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GalaxCore distributed test task control tool.

This tool runs on the central server, usually pudong.
It writes tasks into the central SQLite database only.
Workers should not run this tool and should not access SQLite directly.

Main responsibilities:
    1. Add one-shot test tasks.
    2. Add repeat test tasks.
    3. Add latest-revision repeat tasks.
    4. List, show, cancel, and delete tasks.
    5. List, stop, and start repeat groups.
    6. Preserve every execution record by never reusing a finished task_id.

Repeat task policy:
    - Use --repeat to create a repeat task.
    - A repeat task belongs to one repeat_group.
    - After a worker finishes it, scheduler.py creates the next pending task.
    - stop-repeat disables the group and cancels pending tasks only.
    - Running tasks are not killed by stop-repeat.

Revision policy:
    - fixed: use the task revision as-is.
    - latest: scheduler resolves the latest compiled GalaxCore zip revision
      when a worker pulls the task.

Time policy:
    - This tool explicitly writes datetime('now','localtime') for new records.
    - This avoids SQLite CURRENT_TIMESTAMP showing UTC time in taskctl output.

Compatibility notes:
    - Python 3.6 compatible.
    - Old SQLite compatible. Do not use ON CONFLICT DO UPDATE.
"""

import argparse
import json
import os
import shlex
import sys
import uuid
from pathlib import Path
from string import Template


BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from common.db import get_conn


LOCAL_TIME_SQL = "datetime('now','localtime')"
DEFAULT_TEST_DIR = "/home/user3/workspace/galaxcore/test2"
TEMPLATE_DIR = BASE_DIR / "templates"

DEFAULT_FLOW_CONFIG = {
    "report_timing_summary": 0,
    "opt_design": 0,
    "place_design": 0,
    "place_design_from_syn": 0,
    "phys_opt_design": 0,
    "route_design": 0,
    "route_design_from_place": 0,
    "write_checkpoint": 0,
    "write_bitstream": 1,
    "bit_cmp": 1,
    "msk_cmp": 1,
    "bgn_cmp": 1,
    "dcp_cmp": 0,
    "checksum_cmp": 0,
    "enable_copy": 1,
}


def make_task_id():
    """Create a short unique task id."""
    return "task_" + uuid.uuid4().hex[:12]


def make_default_repeat_group(suite, revision, revision_policy):
    """Build a stable default repeat group name."""
    if revision_policy == "latest":
        return "%s_latest_loop" % suite
    return "%s_r%s_loop" % (suite, revision)


def ensure_columns(cur, table_name, column_defs):
    """Add missing columns to an existing SQLite table."""
    cur.execute("PRAGMA table_info(%s)" % table_name)
    existing_columns = set(row[1] for row in cur.fetchall())

    for column_name, column_def in column_defs.items():
        if column_name not in existing_columns:
            cur.execute(
                "ALTER TABLE %s ADD COLUMN %s %s"
                % (table_name, column_name, column_def)
            )


def ensure_task_runtime_columns(conn):
    """Ensure old databases have command, target, repeat, and revision-policy columns."""
    cur = conn.cursor()

    ensure_columns(
        cur,
        "tasks",
        {
            "revision_policy": "TEXT DEFAULT 'fixed'",
            "cmd": "TEXT",
            "target_arg": "TEXT",
            "target_type": "TEXT",
            "repeat_enabled": "INTEGER DEFAULT 0",
            "repeat_group": "TEXT",
            "repeat_index": "INTEGER DEFAULT 1",
            "parent_task_id": "TEXT",
        },
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS repeat_groups (
            repeat_group TEXT PRIMARY KEY,
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            disabled_at TEXT,
            note TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tasks_repeat_group
        ON tasks(repeat_group, status)
        """
    )

    conn.commit()


def ensure_repeat_group(cur, repeat_group, note=""):
    """Create or re-enable one repeat group."""
    if not repeat_group:
        return

    cur.execute(
        """
        SELECT repeat_group
        FROM repeat_groups
        WHERE repeat_group = ?
        """,
        (repeat_group,),
    )

    row = cur.fetchone()

    if row:
        cur.execute(
            """
            UPDATE repeat_groups
            SET enabled = 1,
                disabled_at = NULL,
                note = ?
            WHERE repeat_group = ?
            """,
            (note, repeat_group),
        )
    else:
        cur.execute(
            """
            INSERT INTO repeat_groups (
                repeat_group,
                enabled,
                created_at,
                note
            )
            VALUES (?, 1, datetime('now','localtime'), ?)
            """,
            (repeat_group, note),
        )


def parse_set_args(items):
    """Parse template variables from CLI --set KEY=VALUE arguments."""
    result = {}

    for item in items or []:
        if "=" not in item:
            raise ValueError("--set argument must be KEY=VALUE, got: %s" % item)

        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key:
            raise ValueError("--set key cannot be empty")

        result[key] = value

    return result


def load_template(template_name, variables):
    """Load a JSON task template and substitute ${VAR} placeholders."""
    if template_name.endswith(".json"):
        template_path = TEMPLATE_DIR / template_name
    else:
        template_path = TEMPLATE_DIR / (template_name + ".json")

    if not template_path.exists():
        raise FileNotFoundError("Template file not found: %s" % template_path)

    raw = template_path.read_text(encoding="utf-8")

    try:
        rendered = Template(raw).substitute(variables)
    except KeyError as e:
        missing = e.args[0]
        raise ValueError(
            "Template variable missing: %s. Use --set %s=xxx or a matching CLI option."
            % (missing, missing)
        )

    try:
        return json.loads(rendered)
    except ValueError as e:
        raise ValueError("Rendered template is not valid JSON: %s" % e)


def pick_value(cli_value, template_value, default_value):
    """Pick value by priority: CLI value > template value > default value."""
    if cli_value is not None:
        return cli_value

    if template_value is not None:
        return template_value

    return default_value


def normalize_revision(value):
    """Normalize revision to int or None."""
    if value is None:
        return None

    if isinstance(value, str) and value.strip().lower() in ("", "none", "null", "latest"):
        return None

    try:
        return int(value)
    except ValueError:
        raise ValueError("revision must be an integer or latest/null, got: %s" % value)


def normalize_revision_policy(value):
    """Normalize revision policy."""
    if value is None:
        return "fixed"

    value = str(value).strip().lower()

    if value not in ("fixed", "latest"):
        raise ValueError("revision_policy must be fixed or latest, got: %s" % value)

    return value


def normalize_target_arg(target):
    """Normalize old or empty target values to a run.sh argument."""
    if target is None:
        return "."

    target = str(target).strip()

    if not target or target.lower() == "any":
        return "."

    return target


def build_default_cmd(test_dir, target_arg):
    """Build the default worker command when a template does not provide cmd."""
    target_arg = normalize_target_arg(target_arg)
    return "cd %s && ./run.sh %s" % (test_dir, target_arg)


def classify_runsh_target(target_args):
    """Classify run.sh target arguments for display and scheduling metadata."""
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

    if "/" in target and "." not in os.path.basename(target):
        return "DIR"

    return "DIR"


def extract_runsh_target(cmd):
    """Extract and classify the argument after ./run.sh from a command string."""
    if not cmd:
        return ".", "ALL"

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

    stop_tokens = {"&&", ";", "||", "|", ">", ">>", "2>", "2>>", "&"}
    target_args = []

    for arg in tokens[runsh_index + 1:]:
        if arg in stop_tokens:
            break
        target_args.append(arg)

    if not target_args:
        return ".", "ALL"

    target_arg = " ".join(target_args).strip()
    return target_arg, classify_runsh_target(target_args)


def print_cmd_target(cmd, target_arg, target_type):
    """Print command and target metadata."""
    print("cmd: %s" % cmd)
    print("target_arg: %s" % target_arg)
    print("target_type: %s" % target_type)


def add_task(args):
    template_task = {}

    if args.template and not args.no_template:
        variables = parse_set_args(args.set)

        if args.revision is not None:
            variables["REVISION"] = str(args.revision)

        if args.revision_policy is not None:
            variables["REVISION_POLICY"] = args.revision_policy

        if args.suite is not None:
            variables["SUITE"] = args.suite

        if args.target_worker is not None:
            variables["TARGET_WORKER"] = args.target_worker

        if args.target is not None:
            variables["TARGET"] = args.target

        if args.cmd is not None:
            variables["CMD"] = args.cmd

        if args.test_dir is not None:
            variables["TEST_DIR"] = args.test_dir

        default_template_test_dir = variables.get("TEST_DIR", DEFAULT_TEST_DIR)
        default_template_target = normalize_target_arg(variables.get("TARGET", "."))

        variables.setdefault("TEST_DIR", default_template_test_dir)
        variables.setdefault("TARGET", default_template_target)
        variables.setdefault(
            "CMD",
            build_default_cmd(default_template_test_dir, default_template_target),
        )
        variables.setdefault("REVISION_POLICY", "fixed")

        template_task = load_template(args.template, variables)

    revision_policy = normalize_revision_policy(
        pick_value(args.revision_policy, template_task.get("revision_policy"), "fixed")
    )

    revision = normalize_revision(
        pick_value(args.revision, template_task.get("revision"), None)
    )

    if revision_policy == "fixed" and revision is None:
        raise ValueError(
            "missing revision for fixed revision policy. Use --revision 14820, "
            "or use --revision-policy latest."
        )

    suite = pick_value(args.suite, template_task.get("suite"), None)
    target_worker = pick_value(args.target_worker, template_task.get("target_worker"), "any")
    priority = int(pick_value(args.priority, template_task.get("priority"), 100))
    max_retry = int(pick_value(args.max_retry, template_task.get("max_retry"), 1))
    test_dir = pick_value(args.test_dir, template_task.get("test_dir"), DEFAULT_TEST_DIR)

    target_arg_from_template = template_task.get("target_arg")
    if target_arg_from_template is None:
        target_arg_from_template = template_task.get("target")

    target_arg_for_cmd = normalize_target_arg(
        pick_value(args.target, target_arg_from_template, ".")
    )

    cmd = pick_value(
        args.cmd,
        template_task.get("cmd") or template_task.get("command"),
        None,
    )

    if cmd:
        cmd = str(cmd).strip()
    else:
        cmd = build_default_cmd(test_dir, target_arg_for_cmd)

    target_arg, target_type = extract_runsh_target(cmd)

    flow_config = dict(DEFAULT_FLOW_CONFIG)

    if "flow_config" in template_task:
        if not isinstance(template_task["flow_config"], dict):
            raise ValueError("template field 'flow_config' must be a JSON object")
        flow_config.update(template_task["flow_config"])

    cli_flow_values = {
        "report_timing_summary": args.report_timing_summary,
        "opt_design": args.opt_design,
        "place_design": args.place_design,
        "place_design_from_syn": args.place_design_from_syn,
        "phys_opt_design": args.phys_opt_design,
        "route_design": args.route_design,
        "route_design_from_place": args.route_design_from_place,
        "write_checkpoint": args.write_checkpoint,
        "write_bitstream": args.write_bitstream,
        "bit_cmp": args.bit_cmp,
        "msk_cmp": args.msk_cmp,
        "bgn_cmp": args.bgn_cmp,
        "dcp_cmp": args.dcp_cmp,
        "checksum_cmp": args.checksum_cmp,
        "enable_copy": args.enable_copy,
    }

    for key, value in cli_flow_values.items():
        if value is not None:
            flow_config[key] = value

    if not suite:
        raise ValueError(
            "missing suite. Use --suite night_build, or put suite in the template."
        )

    if not target_worker:
        target_worker = "any"

    task_id = args.task_id or make_task_id()

    template_repeat_enabled = int(template_task.get("repeat_enabled", 0) or 0)
    repeat_enabled = 1 if args.repeat else template_repeat_enabled
    repeat_group = pick_value(args.repeat_group, template_task.get("repeat_group"), None)

    if repeat_group and not repeat_enabled:
        repeat_enabled = 1

    if repeat_enabled and not repeat_group:
        repeat_group = make_default_repeat_group(suite, revision, revision_policy)

    repeat_index = 1
    parent_task_id = None

    conn = get_conn()
    ensure_task_runtime_columns(conn)
    cur = conn.cursor()

    if repeat_enabled:
        ensure_repeat_group(cur, repeat_group, note="Created by taskctl.py")

    cur.execute(
        """
        INSERT INTO tasks (
            task_id,
            revision,
            revision_policy,
            suite,
            flow_config_json,
            target_worker,
            status,
            priority,
            max_retry,
            cmd,
            target_arg,
            target_type,
            repeat_enabled,
            repeat_group,
            repeat_index,
            parent_task_id,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
        """,
        (
            task_id,
            revision,
            revision_policy,
            suite,
            json.dumps(flow_config, ensure_ascii=False),
            target_worker,
            priority,
            max_retry,
            cmd,
            target_arg,
            target_type,
            repeat_enabled,
            repeat_group,
            repeat_index,
            parent_task_id,
        ),
    )

    message = (
        "Task created. revision=%s, revision_policy=%s, suite=%s, "
        "target_worker=%s, target_type=%s, target_arg=%s, "
        "repeat_enabled=%s, repeat_group=%s"
        % (
            revision if revision is not None else "latest",
            revision_policy,
            suite,
            target_worker,
            target_type,
            target_arg,
            repeat_enabled,
            repeat_group or "-",
        )
    )

    cur.execute(
        """
        INSERT INTO task_events (
            task_id,
            worker_name,
            event,
            message,
            created_at
        )
        VALUES (?, NULL, 'created', ?, datetime('now','localtime'))
        """,
        (task_id, message),
    )

    conn.commit()
    conn.close()

    print("Task added: %s" % task_id)
    print("revision: %s" % (revision if revision is not None else "latest"))
    print("revision_policy: %s" % revision_policy)
    print("suite: %s" % suite)
    print("target_worker: %s" % target_worker)
    print("priority: %s" % priority)
    print("max_retry: %s" % max_retry)
    print("repeat_enabled: %s" % repeat_enabled)
    print("repeat_group: %s" % (repeat_group or "-"))
    print("repeat_index: %s" % repeat_index)
    print_cmd_target(cmd, target_arg, target_type)
    print("flow_config:")
    for k, v in flow_config.items():
        print("  %s %s" % (k, v))


def list_tasks(args):
    conn = get_conn()
    ensure_task_runtime_columns(conn)
    cur = conn.cursor()

    select_sql = """
        SELECT task_id, revision, revision_policy, suite, target_worker, assigned_worker,
               status, priority, retry_count, max_retry, created_at,
               target_arg, target_type, cmd,
               repeat_enabled, repeat_group, repeat_index
        FROM tasks
    """

    if args.status:
        cur.execute(
            select_sql + """
            WHERE status = ?
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
            """,
            (args.status, args.limit),
        )
    else:
        cur.execute(
            select_sql + """
            ORDER BY id DESC
            LIMIT ?
            """,
            (args.limit,),
        )

    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("No tasks found.")
        return

    print(
        "%-20s %-8s %-7s %-16s %-10s %-10s %-10s %-8s %-11s %-30s %-20s %s"
        % (
            "TASK_ID",
            "REV",
            "POLICY",
            "SUITE",
            "T_WORKER",
            "WORKER",
            "STATUS",
            "RETRY",
            "TYPE",
            "TARGET_ARG",
            "REPEAT",
            "CREATED_AT",
        )
    )
    print("-" * 175)

    for row in rows:
        retry = "%s/%s" % (row["retry_count"], row["max_retry"])
        revision_text = str(row["revision"] if row["revision"] is not None else "latest")
        target_arg = str(row["target_arg"] or "-")
        repeat_text = "-"

        if int(row["repeat_enabled"] or 0) == 1:
            repeat_text = "%s#%s" % (row["repeat_group"] or "-", row["repeat_index"] or 1)

        if len(target_arg) > 29:
            target_arg = target_arg[:26] + "..."

        if len(repeat_text) > 19:
            repeat_text = repeat_text[:16] + "..."

        print(
            "%-20s %-8s %-7s %-16s %-10s %-10s %-10s %-8s %-11s %-30s %-20s %s"
            % (
                row["task_id"],
                revision_text,
                row["revision_policy"] or "fixed",
                row["suite"],
                row["target_worker"],
                row["assigned_worker"] or "-",
                row["status"],
                retry,
                row["target_type"] or "-",
                target_arg,
                repeat_text,
                row["created_at"],
            )
        )


def show_task(args):
    conn = get_conn()
    ensure_task_runtime_columns(conn)
    cur = conn.cursor()

    cur.execute("SELECT * FROM tasks WHERE task_id = ?", (args.task_id,))
    row = cur.fetchone()

    if not row:
        conn.close()
        print("Task not found: %s" % args.task_id)
        return

    print("Task:")
    for key in row.keys():
        if key == "flow_config_json":
            print("flow_config:")
            try:
                flow_config = json.loads(row[key] or "{}")
            except Exception:
                flow_config = {}
            for k, v in flow_config.items():
                print("  %s %s" % (k, v))
        else:
            print("%s: %s" % (key, row[key]))

    print()
    print("Events:")

    cur.execute(
        """
        SELECT worker_name, event, message, created_at
        FROM task_events
        WHERE task_id = ?
        ORDER BY id ASC
        """,
        (args.task_id,),
    )

    events = cur.fetchall()
    conn.close()

    if not events:
        print("  no events")
        return

    for event in events:
        worker = event["worker_name"] or "-"
        print(
            "  [%s] worker=%s event=%s message=%s"
            % (event["created_at"], worker, event["event"], event["message"])
        )


def cancel_task(args):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE tasks
        SET status = 'canceled',
            finished_at = datetime('now','localtime'),
            error_message = ?
        WHERE task_id = ?
          AND status IN ('pending', 'running')
        """,
        (args.reason, args.task_id),
    )

    changed = cur.rowcount

    if changed:
        cur.execute(
            """
            INSERT INTO task_events (
                task_id,
                worker_name,
                event,
                message,
                created_at
            )
            VALUES (?, NULL, 'canceled', ?, datetime('now','localtime'))
            """,
            (args.task_id, args.reason),
        )

    conn.commit()
    conn.close()

    if changed:
        print("Task canceled: %s" % args.task_id)
    else:
        print(
            "Task not canceled. Maybe it does not exist or is already finished: %s"
            % args.task_id
        )


def delete_task(args):
    """Delete one task and its events from database."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT task_id, status, assigned_worker
        FROM tasks
        WHERE task_id = ?
        """,
        (args.task_id,),
    )

    row = cur.fetchone()

    if not row:
        conn.close()
        print("Task not found: %s" % args.task_id)
        return

    if row["status"] == "running" and not args.force:
        conn.close()
        print("Task is running, not deleted: %s" % args.task_id)
        print("assigned_worker: %s" % row["assigned_worker"])
        print("Use --force if you really want to delete it.")
        return

    cur.execute("DELETE FROM task_events WHERE task_id = ?", (args.task_id,))
    deleted_events = cur.rowcount

    cur.execute("DELETE FROM tasks WHERE task_id = ?", (args.task_id,))
    deleted_tasks = cur.rowcount

    conn.commit()
    conn.close()

    if deleted_tasks:
        print("Task deleted: %s" % args.task_id)
        print("deleted task_events: %s" % deleted_events)
    else:
        print("Task not deleted: %s" % args.task_id)


def list_repeat_groups(args):
    conn = get_conn()
    ensure_task_runtime_columns(conn)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT repeat_group, enabled, created_at, disabled_at, note
        FROM repeat_groups
        ORDER BY repeat_group ASC
        """
    )

    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("No repeat groups found.")
        return

    print(
        "%-32s %-8s %-20s %-20s %s"
        % ("REPEAT_GROUP", "ENABLED", "CREATED_AT", "DISABLED_AT", "NOTE")
    )
    print("-" * 110)

    for row in rows:
        print(
            "%-32s %-8s %-20s %-20s %s"
            % (
                row["repeat_group"],
                row["enabled"],
                row["created_at"],
                row["disabled_at"] or "-",
                row["note"] or "-",
            )
        )


def stop_repeat_group(args):
    conn = get_conn()
    ensure_task_runtime_columns(conn)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT repeat_group
        FROM repeat_groups
        WHERE repeat_group = ?
        """,
        (args.repeat_group,),
    )

    row = cur.fetchone()

    if not row:
        conn.close()
        print("Repeat group not found: %s" % args.repeat_group)
        return

    cur.execute(
        """
        UPDATE repeat_groups
        SET enabled = 0,
            disabled_at = datetime('now','localtime'),
            note = ?
        WHERE repeat_group = ?
        """,
        (args.reason, args.repeat_group),
    )

    cur.execute(
        """
        SELECT task_id
        FROM tasks
        WHERE repeat_group = ?
          AND repeat_enabled = 1
          AND status = 'pending'
        """,
        (args.repeat_group,),
    )

    pending_rows = cur.fetchall()

    cur.execute(
        """
        UPDATE tasks
        SET status = 'canceled',
            finished_at = datetime('now','localtime'),
            error_message = ?
        WHERE repeat_group = ?
          AND repeat_enabled = 1
          AND status = 'pending'
        """,
        ("repeat group stopped: %s" % args.reason, args.repeat_group),
    )

    canceled_count = cur.rowcount

    for task in pending_rows:
        cur.execute(
            """
            INSERT INTO task_events (
                task_id,
                worker_name,
                event,
                message,
                created_at
            )
            VALUES (?, NULL, 'canceled', ?, datetime('now','localtime'))
            """,
            (
                task["task_id"],
                "Canceled because repeat group was stopped: %s" % args.reason,
            ),
        )

    conn.commit()
    conn.close()

    print("Repeat group stopped: %s" % args.repeat_group)
    print("Pending tasks canceled: %s" % canceled_count)
    print("Running tasks will not be killed, but no next task will be generated.")


def start_repeat_group(args):
    conn = get_conn()
    ensure_task_runtime_columns(conn)
    cur = conn.cursor()

    ensure_repeat_group(cur, args.repeat_group, note=args.note)

    conn.commit()
    conn.close()

    print("Repeat group enabled: %s" % args.repeat_group)
    print("No new task is created automatically by this command.")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Task control tool for distributed GalaxCore test system"
    )

    sub = parser.add_subparsers(dest="cmd")

    add = sub.add_parser("add", help="add a new test task")
    add.add_argument("--task-id", default=None)
    add.add_argument("--template", default="default", help="template name under templates/, default: default")
    add.add_argument("--no-template", action="store_true", help="do not load template, use CLI arguments only")
    add.add_argument("--set", action="append", default=[], help="template variable, for example: --set REVISION=14820")
    add.add_argument("--revision", default=None, help="fixed revision number, or latest/null with --revision-policy latest")
    add.add_argument("--revision-policy", choices=["fixed", "latest"], default=None, help="fixed or latest")
    add.add_argument("--suite", default=None)
    add.add_argument("--target-worker", default=None)
    add.add_argument("--priority", type=int, default=None)
    add.add_argument("--max-retry", type=int, default=None)
    add.add_argument("--cmd", default=None, help="full command sent to worker and executed as-is")
    add.add_argument("--target", default=None, help="run.sh target used only when --cmd/template cmd is not provided; 'any' becomes '.'")
    add.add_argument("--test-dir", default=None, help="test2 directory used to build default cmd")
    add.add_argument("--repeat", action="store_true", help="make this task a repeat task")
    add.add_argument("--repeat-group", "--repeat_group", dest="repeat_group", default=None, help="repeat group name")

    add.add_argument("--report-timing-summary", type=int, default=None)
    add.add_argument("--opt-design", type=int, default=None)
    add.add_argument("--place-design", type=int, default=None)
    add.add_argument("--place-design-from-syn", type=int, default=None)
    add.add_argument("--phys-opt-design", type=int, default=None)
    add.add_argument("--route-design", type=int, default=None)
    add.add_argument("--route-design-from-place", type=int, default=None)
    add.add_argument("--write-checkpoint", type=int, default=None)
    add.add_argument("--write-bitstream", type=int, default=None)
    add.add_argument("--bit-cmp", type=int, default=None)
    add.add_argument("--msk-cmp", type=int, default=None)
    add.add_argument("--bgn-cmp", type=int, default=None)
    add.add_argument("--dcp-cmp", type=int, default=None)
    add.add_argument("--checksum-cmp", type=int, default=None)
    add.add_argument("--enable-copy", type=int, default=None)
    add.set_defaults(func=add_task)

    ls = sub.add_parser("list", help="list tasks")
    ls.add_argument("--status", default=None)
    ls.add_argument("--limit", type=int, default=20)
    ls.set_defaults(func=list_tasks)

    show = sub.add_parser("show", help="show one task")
    show.add_argument("task_id")
    show.set_defaults(func=show_task)

    cancel = sub.add_parser("cancel", help="cancel one task")
    cancel.add_argument("task_id")
    cancel.add_argument("--reason", default="manual canceled")
    cancel.set_defaults(func=cancel_task)

    delete = sub.add_parser("delete", help="delete one task from database")
    delete.add_argument("task_id")
    delete.add_argument("--force", action="store_true", help="force delete even if the task is running")
    delete.set_defaults(func=delete_task)

    list_repeat = sub.add_parser("list-repeat", help="list repeat groups")
    list_repeat.set_defaults(func=list_repeat_groups)

    stop_repeat = sub.add_parser("stop-repeat", help="stop one repeat group")
    stop_repeat.add_argument("repeat_group")
    stop_repeat.add_argument("--reason", default="manual stop repeat group")
    stop_repeat.set_defaults(func=stop_repeat_group)

    start_repeat = sub.add_parser("start-repeat", help="enable one repeat group")
    start_repeat.add_argument("repeat_group")
    start_repeat.add_argument("--note", default="manual start repeat group")
    start_repeat.set_defaults(func=start_repeat_group)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
