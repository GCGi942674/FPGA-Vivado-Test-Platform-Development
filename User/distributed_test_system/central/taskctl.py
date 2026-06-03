#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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


# Default values used when no template value and no CLI override are provided.
#
# These keys are saved into tasks.flow_config_json.
# The scheduler will send them to the worker as task["flow_config"].
# The worker should use them to update the real flow_config file before running run.sh.
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

    # Keep the old taskctl behavior: default copy is enabled.
    # Change this to 0 if you want the raw flow_config default.
    "enable_copy": 1,
}


# Default template directory:
#   /home/user3/distributed_test_system/templates
TEMPLATE_DIR = BASE_DIR / "templates"


def make_task_id():
    return "task_" + uuid.uuid4().hex[:12]


def parse_set_args(items):
    """
    Parse template variables from CLI.

    Example:
        --set REVISION=14820 --set TARGET_WORKER=kangqiao

    Returns:
        {
            "REVISION": "14820",
            "TARGET_WORKER": "kangqiao"
        }
    """
    result = {}

    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--set argument must be KEY=VALUE, got: {item}")

        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key:
            raise ValueError("--set key cannot be empty")

        result[key] = value

    return result


def load_template(template_name, variables):
    """
    Load a JSON task template from templates/ and replace ${VAR} placeholders.

    Example:
        --template default

    Loads:
        templates/default.json
    """
    if template_name.endswith(".json"):
        template_path = TEMPLATE_DIR / template_name
    else:
        template_path = TEMPLATE_DIR / f"{template_name}.json"

    if not template_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_path}")

    raw = template_path.read_text(encoding="utf-8")

    try:
        rendered = Template(raw).substitute(variables)
    except KeyError as e:
        missing = e.args[0]
        raise ValueError(
            f"Template variable missing: {missing}. "
            f"Use --set {missing}=xxx or a matching CLI option."
        )

    try:
        return json.loads(rendered)
    except json.JSONDecodeError as e:
        raise ValueError(f"Rendered template is not valid JSON: {e}")


def pick_value(cli_value, template_value, default_value):
    """
    Priority:
        CLI value > template value > default value
    """
    if cli_value is not None:
        return cli_value

    if template_value is not None:
        return template_value

    return default_value


def normalize_revision(value):
    """
    Keep revision as int, same as the original taskctl behavior.
    """
    if value is None:
        return None

    try:
        return int(value)
    except ValueError:
        raise ValueError(f"revision must be an integer, got: {value}")


def ensure_task_runtime_columns(conn):
    """Ensure old SQLite databases have command and target columns."""
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(tasks)")
    existing_columns = {row[1] for row in cur.fetchall()}

    column_defs = {
        "cmd": "TEXT",
        "target_arg": "TEXT",
        "target_type": "TEXT",
    }

    for column, column_type in column_defs.items():
        if column not in existing_columns:
            cur.execute(f"ALTER TABLE tasks ADD COLUMN {column} {column_type}")

    conn.commit()


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
    return f"cd {test_dir} && ./run.sh {target_arg}"


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


def print_cmd_target(cmd, target_arg, target_type):
    print(f"cmd: {cmd}")
    print(f"target_arg: {target_arg}")
    print(f"target_type: {target_type}")


def add_task(args):
    template_task = {}

    # By default, add command loads templates/default.json.
    # Use --no-template to disable template mode.
    if args.template and not args.no_template:
        variables = parse_set_args(args.set)

        # Common template variables can be provided by normal CLI options.
        if args.revision is not None:
            variables["REVISION"] = str(args.revision)

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

        default_template_test_dir = variables.get(
            "TEST_DIR",
            "/home/user3/workspace/galaxcore/test2",
        )
        default_template_target = normalize_target_arg(
            variables.get("TARGET", ".")
        )

        variables.setdefault("TEST_DIR", default_template_test_dir)
        variables.setdefault("TARGET", default_template_target)
        variables.setdefault(
            "CMD",
            build_default_cmd(default_template_test_dir, default_template_target),
        )

        template_task = load_template(args.template, variables)

    revision = pick_value(
        args.revision,
        template_task.get("revision"),
        None,
    )
    revision = normalize_revision(revision)

    suite = pick_value(
        args.suite,
        template_task.get("suite"),
        None,
    )

    target_worker = pick_value(
        args.target_worker,
        template_task.get("target_worker"),
        "any",
    )

    priority = int(
        pick_value(
            args.priority,
            template_task.get("priority"),
            100,
        )
    )

    max_retry = int(
        pick_value(
            args.max_retry,
            template_task.get("max_retry"),
            1,
        )
    )

    test_dir = pick_value(
        args.test_dir,
        template_task.get("test_dir"),
        "/home/user3/workspace/galaxcore/test2",
    )

    target_arg_from_template = (
        template_task.get("target_arg")
        if template_task.get("target_arg") is not None
        else template_task.get("target")
    )

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

    # Start from built-in defaults.
    flow_config = dict(DEFAULT_FLOW_CONFIG)

    # Then apply template flow_config.
    # This also preserves future extra fields, as long as they are in template["flow_config"].
    if "flow_config" in template_task:
        if not isinstance(template_task["flow_config"], dict):
            raise ValueError("template field 'flow_config' must be a JSON object")

        flow_config.update(template_task["flow_config"])

    # Finally apply CLI overrides.
    #
    # Important:
    # These argparse values must default to None.
    # Otherwise they would overwrite template values even when not specified.
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

    if revision is None:
        raise ValueError(
            "missing revision. Use --revision 14820, "
            "or put revision in the template."
        )

    if not suite:
        raise ValueError(
            "missing suite. Use --suite night_build, "
            "or put suite in the template."
        )

    if not target_worker:
        target_worker = "any"

    task_id = args.task_id or make_task_id()

    conn = get_conn()
    ensure_task_runtime_columns(conn)
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO tasks (
            task_id,
            revision,
            suite,
            flow_config_json,
            target_worker,
            status,
            priority,
            max_retry,
            cmd,
            target_arg,
            target_type
        )
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            revision,
            suite,
            json.dumps(flow_config, ensure_ascii=False),
            target_worker,
            priority,
            max_retry,
            cmd,
            target_arg,
            target_type,
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
        VALUES (?, NULL, 'created', ?)
        """,
        (
            task_id,
            (
                f"Task created. revision={revision}, suite={suite}, "
                f"target_worker={target_worker}, target_type={target_type}, target_arg={target_arg}"
            ),
        ),
    )

    conn.commit()
    conn.close()

    print(f"Task added: {task_id}")
    print(f"revision: {revision}")
    print(f"suite: {suite}")
    print(f"target_worker: {target_worker}")
    print(f"priority: {priority}")
    print(f"max_retry: {max_retry}")
    print_cmd_target(cmd, target_arg, target_type)
    print("flow_config:")
    for k, v in flow_config.items():
        print(f"  {k} {v}")


def list_tasks(args):
    conn = get_conn()
    ensure_task_runtime_columns(conn)
    cur = conn.cursor()

    if args.status:
        cur.execute(
            """
            SELECT task_id, revision, suite, target_worker, assigned_worker,
                   status, priority, retry_count, max_retry, created_at,
                   target_arg, target_type, cmd
            FROM tasks
            WHERE status = ?
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
            """,
            (args.status, args.limit),
        )
    else:
        cur.execute(
            """
            SELECT task_id, revision, suite, target_worker, assigned_worker,
                   status, priority, retry_count, max_retry, created_at,
                   target_arg, target_type, cmd
            FROM tasks
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
        f"{'TASK_ID':<20} {'REV':<8} {'SUITE':<16} "
        f"{'T_WORKER':<10} {'WORKER':<10} {'STATUS':<10} "
        f"{'TYPE':<11} {'TARGET_ARG':<36} {'RETRY':<8} {'CREATED_AT'}"
    )
    print("-" * 150)

    for row in rows:
        retry = f"{row['retry_count']}/{row['max_retry']}"
        target_arg = str(row['target_arg'] or "-")
        if len(target_arg) > 35:
            target_arg = target_arg[:32] + "..."

        print(
            f"{row['task_id']:<20} "
            f"{row['revision']:<8} "
            f"{row['suite']:<16} "
            f"{row['target_worker']:<10} "
            f"{str(row['assigned_worker'] or '-'):<10} "
            f"{row['status']:<10} "
            f"{str(row['target_type'] or '-'):<11} "
            f"{target_arg:<36} "
            f"{retry:<8} "
            f"{row['created_at']}"
        )


def show_task(args):
    conn = get_conn()
    ensure_task_runtime_columns(conn)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM tasks
        WHERE task_id = ?
        """,
        (args.task_id,),
    )

    row = cur.fetchone()

    if not row:
        conn.close()
        print(f"Task not found: {args.task_id}")
        return

    print("Task:")
    for key in row.keys():
        if key == "flow_config_json":
            print("flow_config:")
            flow_config = json.loads(row[key])
            for k, v in flow_config.items():
                print(f"  {k} {v}")
        else:
            print(f"{key}: {row[key]}")

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
            f"  [{event['created_at']}] "
            f"worker={worker} "
            f"event={event['event']} "
            f"message={event['message']}"
        )


def cancel_task(args):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE tasks
        SET status = 'canceled',
            finished_at = CURRENT_TIMESTAMP,
            error_message = ?
        WHERE task_id = ?
          AND status IN ('pending', 'running')
        """,
        (
            args.reason,
            args.task_id,
        ),
    )

    changed = cur.rowcount

    if changed:
        cur.execute(
            """
            INSERT INTO task_events (
                task_id,
                worker_name,
                event,
                message
            )
            VALUES (?, NULL, 'canceled', ?)
            """,
            (
                args.task_id,
                args.reason,
            ),
        )

    conn.commit()
    conn.close()

    if changed:
        print(f"Task canceled: {args.task_id}")
    else:
        print(f"Task not canceled. Maybe it does not exist or is already finished: {args.task_id}")

def delete_task(args):
    """
    Delete one task from database.

    Difference between cancel and delete:
        cancel: keep the task record, only change status to canceled
        delete: remove task_events and tasks records from database

    By default, running tasks are not allowed to be deleted.
    Use --force if you really want to delete a running task.
    """
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
        print(f"Task not found: {args.task_id}")
        return

    status = row["status"]
    assigned_worker = row["assigned_worker"]

    if status == "running" and not args.force:
        conn.close()
        print(f"Task is running, not deleted: {args.task_id}")
        print(f"assigned_worker: {assigned_worker}")
        print("Use --force if you really want to delete it.")
        return

    # Delete child records first.
    cur.execute(
        """
        DELETE FROM task_events
        WHERE task_id = ?
        """,
        (args.task_id,),
    )

    deleted_events = cur.rowcount

    cur.execute(
        """
        DELETE FROM tasks
        WHERE task_id = ?
        """,
        (args.task_id,),
    )

    deleted_tasks = cur.rowcount

    conn.commit()
    conn.close()

    if deleted_tasks:
        print(f"Task deleted: {args.task_id}")
        print(f"deleted task_events: {deleted_events}")
    else:
        print(f"Task not deleted: {args.task_id}")

def build_parser():
    parser = argparse.ArgumentParser(
        description="Task control tool for distributed GalaxCore test system"
    )

    sub = parser.add_subparsers(dest="cmd")

    add = sub.add_parser("add", help="add a new test task")
    add.add_argument("--task-id", default=None)

    # Template options.
    # Default behavior:
    #   add reads templates/default.json
    add.add_argument(
        "--template",
        default="default",
        help="template name under templates/, default: default",
    )
    add.add_argument(
        "--no-template",
        action="store_true",
        help="do not load template, use CLI arguments only",
    )
    add.add_argument(
        "--set",
        action="append",
        default=[],
        help="template variable, for example: --set REVISION=14820",
    )

    # Task metadata.
    # These can come from template, but CLI options have higher priority.
    add.add_argument("--revision", type=int, default=None)
    add.add_argument("--suite", default=None)
    add.add_argument("--target-worker", default=None)
    add.add_argument("--priority", type=int, default=None)
    add.add_argument("--max-retry", type=int, default=None)
    add.add_argument(
        "--cmd",
        default=None,
        help="full command sent to worker and executed as-is",
    )
    add.add_argument(
        "--target",
        default=None,
        help="run.sh target used only when --cmd/template cmd is not provided; 'any' becomes '.'",
    )
    add.add_argument(
        "--test-dir",
        default=None,
        help="test2 directory used to build default cmd",
    )

    # flow_config options.
    # All defaults must be None to avoid overriding template values.
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
    delete.add_argument(
        "--force",
        action="store_true",
        help="force delete even if the task is running",
    )
    delete.set_defaults(func=delete_task)

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
