#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Find the first failing GalaxCore revision for one run.tcl by bisecting PJTest tasks.

This script does not modify scheduler.py, worker.py, taskctl.py, or the database schema.
It creates one temporary PJTest task per checked revision, waits for the task to finish,
reads the result from the scheduler JSON API, and optionally deletes intermediate tasks.

Typical usage:
    ./find_first_fail.py place /home/user3/workspace/galaxcore/test2/foo/bar/run.tcl \
        --good 14880 \
        --bad 15010 \
        --scheduler http://127.0.0.1:8888 \
        --taskctl ./taskctl.py
"""

from __future__ import print_function

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

try:
    from urllib.parse import urlencode
    from urllib.request import urlopen
    from urllib.error import HTTPError, URLError
except ImportError:  # pragma: no cover, Python 2 fallback is not expected.
    from urllib import urlencode
    from urllib2 import urlopen, HTTPError, URLError


TERMINAL_TASK_STATUSES = set(["success", "failed", "canceled"])
TERMINAL_EXAMPLE_STATUSES = set(["success", "failed", "timeout", "canceled"])
BAD_EXAMPLE_STATUSES = set(["failed", "timeout"])
GOOD_EXAMPLE_STATUSES = set(["success"])


class BisectError(Exception):
    """Raised when bisect cannot continue safely."""


class RevisionResult(object):
    """Result for one checked revision."""

    def __init__(self, revision, task_id, task_status, example_status, example):
        self.revision = int(revision)
        self.task_id = task_id
        self.task_status = task_status
        self.example_status = example_status
        self.example = example or {}

    def is_good(self):
        """Return True if this revision passed."""
        return self.example_status in GOOD_EXAMPLE_STATUSES

    def is_bad(self):
        """Return True if this revision failed or timed out."""
        return self.example_status in BAD_EXAMPLE_STATUSES

    def message(self):
        """Return the most useful failure message."""
        return self.example.get("message") or self.example.get("failed_reason") or ""

    def log_file(self):
        """Return worker log file path, if available."""
        return self.example.get("log_file") or ""


class BisectRunner(object):
    """Run PJTest revision bisect for one run.tcl."""

    def __init__(self, args):
        self.args = args
        self.created_tasks = []
        self.kept_task_ids = set()
        self.case_list_path = None
        self.case_hash = self.make_case_hash(args.run_tcl)

    @staticmethod
    def local_now():
        """Return a local timestamp string."""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def make_case_hash(text):
        """Build a short stable-ish hash without extra dependencies."""
        value = 0
        for char in str(text):
            value = ((value * 131) + ord(char)) & 0xFFFFFFFF
        return "%08x" % value

    def log(self, message):
        """Print one timestamped progress line."""
        print("[%s] %s" % (self.local_now(), message))
        sys.stdout.flush()

    def make_case_list(self):
        """Create a temporary one-case list file for taskctl add."""
        run_tcl = Path(self.args.run_tcl).expanduser().resolve()

        if not run_tcl.is_absolute():
            raise BisectError("run_tcl must be an absolute path: %s" % run_tcl)
        if run_tcl.name != "run.tcl":
            raise BisectError("run_tcl must point to a file named run.tcl: %s" % run_tcl)
        if not run_tcl.is_file():
            raise BisectError("run_tcl does not exist: %s" % run_tcl)

        fd, path = tempfile.mkstemp(
            prefix="pjtest_bisect_%s_" % self.case_hash,
            suffix=".txt",
            dir=self.args.tmp_dir,
        )
        with os.fdopen(fd, "w") as f:
            f.write(str(run_tcl) + "\n")

        self.case_list_path = path
        return path

    def task_name_for_revision(self, revision, label):
        """Build a compact task name."""
        prefix = self.args.name_prefix
        return "%s_%s_r%s_%s" % (prefix, self.case_hash, revision, label)

    def run_process(self, argv, timeout=None):
        """Run a command and return stdout text."""
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )

        output_lines = []
        start_time = time.time()

        while True:
            line = proc.stdout.readline()
            if line:
                output_lines.append(line)

            rc = proc.poll()
            if rc is not None:
                rest = proc.stdout.read()
                if rest:
                    output_lines.append(rest)
                break

            if timeout and timeout > 0 and time.time() - start_time > timeout:
                try:
                    proc.kill()
                except Exception:
                    pass
                raise BisectError("command timeout: %s" % " ".join(argv))

            time.sleep(0.1)

        output = "".join(output_lines)
        if rc != 0:
            raise BisectError("command failed rc=%s: %s\n%s" % (rc, " ".join(argv), output))

        return output

    def create_task(self, revision, label):
        """Create one PJTest task for a specific revision."""
        if not self.case_list_path:
            self.make_case_list()

        task_name = self.task_name_for_revision(revision, label)
        argv = [
            sys.executable,
            self.args.taskctl,
            "add",
            self.args.template,
            self.case_list_path,
            "--revision",
            str(revision),
            "--name",
            task_name,
            "--priority",
            str(self.args.priority),
            "--max-retry",
            str(self.args.max_retry),
        ]

        if self.args.max_time is not None:
            argv.extend(["--max-time", str(self.args.max_time)])

        self.log("CREATE revision=%s name=%s" % (revision, task_name))
        output = self.run_process(argv, timeout=self.args.taskctl_timeout)
        match = re.search(r"(task_[A-Za-z0-9_]+)", output)
        if not match:
            raise BisectError("cannot parse task_id from taskctl output:\n%s" % output)

        task_id = match.group(1)
        self.created_tasks.append(task_id)
        self.log("CREATED revision=%s task=%s" % (revision, task_id))
        return task_id

    def http_get_json(self, path, params=None):
        """GET JSON from scheduler."""
        query = ""
        if params:
            query = "?" + urlencode(params)
        url = self.args.scheduler.rstrip("/") + path + query

        try:
            with urlopen(url, timeout=self.args.http_timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise BisectError("HTTP %s %s: %s" % (exc.code, url, raw))
        except URLError as exc:
            raise BisectError("HTTP request failed %s: %s" % (url, exc))

        try:
            return json.loads(raw) if raw.strip() else {}
        except Exception:
            raise BisectError("invalid JSON from %s:\n%s" % (url, raw))

    def get_task_status(self, task_id):
        """Return current task status from scheduler API."""
        data = self.http_get_json("/api/tasks", {"limit": 200})
        for row in data.get("tasks", []):
            if row.get("task_id") == task_id:
                return row.get("status")
        return None

    def get_single_example(self, task_id):
        """Return the single example row for a task."""
        data = self.http_get_json("/api/examples", {"task_id": task_id, "limit": 10})
        rows = data.get("examples", [])
        if not rows:
            return None
        if len(rows) != 1:
            raise BisectError("task %s expected 1 example, got %d" % (task_id, len(rows)))
        return rows[0]

    def wait_task_done(self, task_id, revision):
        """Poll scheduler until the single example reaches terminal status."""
        start_time = time.time()
        last_status = None

        while True:
            example = self.get_single_example(task_id)
            if example:
                example_status = example.get("status")
                task_status = self.get_task_status(task_id) or "unknown"

                if example_status != last_status:
                    self.log(
                        "WAIT revision=%s task=%s task_status=%s example_status=%s"
                        % (revision, task_id, task_status, example_status)
                    )
                    last_status = example_status

                if example_status in TERMINAL_EXAMPLE_STATUSES:
                    return RevisionResult(
                        revision=revision,
                        task_id=task_id,
                        task_status=task_status,
                        example_status=example_status,
                        example=example,
                    )

            if self.args.wait_timeout > 0 and time.time() - start_time > self.args.wait_timeout:
                raise BisectError("timeout waiting for task=%s revision=%s" % (task_id, revision))

            time.sleep(self.args.poll_interval)

    def delete_task(self, task_id):
        """Delete one temporary task through taskctl."""
        if not task_id:
            return

        argv = [sys.executable, self.args.taskctl, "delete", task_id, "--force"]
        try:
            self.run_process(argv, timeout=self.args.taskctl_timeout)
            self.log("DELETED task=%s" % task_id)
        except Exception as exc:
            self.log("WARN delete failed task=%s err=%s" % (task_id, exc))

    def should_delete_task(self, task_id):
        """Return True when this task should be removed after use."""
        if self.args.keep_tasks:
            return False
        if task_id in self.kept_task_ids:
            return False
        return True

    def cleanup_intermediate_tasks(self):
        """Delete tasks that are not marked to keep."""
        if self.args.keep_tasks:
            return
        for task_id in list(self.created_tasks):
            if task_id not in self.kept_task_ids:
                self.delete_task(task_id)

    def check_revision_once(self, revision, label):
        """Create, wait, and classify one revision."""
        task_id = self.create_task(revision, label)
        result = self.wait_task_done(task_id, revision)

        self.log(
            "RESULT revision=%s task=%s status=%s message=%s"
            % (
                revision,
                result.task_id,
                result.example_status,
                result.message() or "-",
            )
        )
        return result

    def check_revision(self, revision, label, confirm_bad=False):
        """Check one revision and optionally confirm bad results."""
        first = self.check_revision_once(revision, label)

        if not first.is_bad() or not confirm_bad or self.args.confirm_fail <= 1:
            return first

        bad_results = [first]
        for index in range(2, self.args.confirm_fail + 1):
            retry_label = "%s_confirm%d" % (label, index)
            retry = self.check_revision_once(revision, retry_label)
            bad_results.append(retry)

            if retry.is_good():
                raise BisectError(
                    "revision %s is unstable: first result was %s, confirm result was %s"
                    % (revision, first.example_status, retry.example_status)
                )

        return bad_results[-1]

    def assert_boundary(self, good_revision, bad_revision):
        """Verify initial good and bad revisions."""
        good_result = self.check_revision(good_revision, "good_check", confirm_bad=False)
        bad_result = self.check_revision(bad_revision, "bad_check", confirm_bad=True)

        if not good_result.is_good():
            raise BisectError(
                "good boundary is not success: revision=%s status=%s task=%s"
                % (good_revision, good_result.example_status, good_result.task_id)
            )

        if not bad_result.is_bad():
            raise BisectError(
                "bad boundary is not failed/timeout: revision=%s status=%s task=%s"
                % (bad_revision, bad_result.example_status, bad_result.task_id)
            )

        return good_result, bad_result

    def print_result(self, good_revision, bad_revision, good_result, bad_result, history):
        """Print final bisect result."""
        print("")
        print("BISECT RESULT")
        print("=" * 80)
        print("Example              : %s" % Path(self.args.run_tcl).resolve())
        print("Last good revision   : %s" % good_revision)
        print("First bad revision   : %s" % bad_revision)
        print("Last good task id    : %s" % (good_result.task_id if good_result else "-"))
        print("First bad task id    : %s" % (bad_result.task_id if bad_result else "-"))
        print("Bad status           : %s" % (bad_result.example_status if bad_result else "-"))
        print("Bad message          : %s" % (bad_result.message() if bad_result else "-"))
        print("Bad log file         : %s" % (bad_result.log_file() if bad_result else "-"))
        print("")
        print("History")
        print("-" * 80)
        for item in history:
            print(
                "r%-10s %-8s task=%s %s"
                % (item.revision, item.example_status, item.task_id, item.message() or "")
            )
        print("")

    def run(self):
        """Run the full bisect workflow."""
        good_revision = int(self.args.good)
        bad_revision = int(self.args.bad)

        if good_revision >= bad_revision:
            raise BisectError("--good must be smaller than --bad")

        self.make_case_list()
        self.log("case list: %s" % self.case_list_path)

        history = []
        current_good_result = None
        current_bad_result = None

        try:
            if not self.args.skip_boundary_check:
                self.log("boundary check start")
                current_good_result, current_bad_result = self.assert_boundary(good_revision, bad_revision)
                history.extend([current_good_result, current_bad_result])
            else:
                self.log("boundary check skipped")

            while bad_revision - good_revision > 1:
                mid_revision = (good_revision + bad_revision) // 2
                result = self.check_revision(mid_revision, "mid", confirm_bad=True)
                history.append(result)

                if result.is_good():
                    good_revision = mid_revision
                    current_good_result = result
                elif result.is_bad():
                    bad_revision = mid_revision
                    current_bad_result = result
                else:
                    raise BisectError(
                        "unexpected result revision=%s status=%s task=%s"
                        % (mid_revision, result.example_status, result.task_id)
                    )

                self.log("RANGE good=%s bad=%s" % (good_revision, bad_revision))

            if current_good_result:
                self.kept_task_ids.add(current_good_result.task_id)
            if current_bad_result:
                self.kept_task_ids.add(current_bad_result.task_id)

            if self.args.delete_all:
                self.kept_task_ids.clear()

            self.cleanup_intermediate_tasks()
            self.print_result(
                good_revision,
                bad_revision,
                current_good_result,
                current_bad_result,
                history,
            )
            return 0

        finally:
            if self.case_list_path and not self.args.keep_case_list:
                try:
                    os.unlink(self.case_list_path)
                except Exception:
                    pass


def default_taskctl_path():
    """Return the default taskctl path."""
    env_value = os.environ.get("PJTEST_TASKCTL")
    if env_value:
        return env_value

    local = Path.cwd() / "taskctl.py"
    if local.is_file():
        return str(local)

    script_dir_local = Path(__file__).resolve().parent / "taskctl.py"
    if script_dir_local.is_file():
        return str(script_dir_local)

    return "./taskctl.py"


def build_parser():
    """Build CLI parser."""
    parser = argparse.ArgumentParser(
        description="Find first failing GalaxCore revision for one PJTest run.tcl."
    )
    parser.add_argument("template", help="task template name, for example: place")
    parser.add_argument("run_tcl", help="absolute path to one run.tcl")
    parser.add_argument("--good", required=True, type=int, help="known good revision")
    parser.add_argument("--bad", required=True, type=int, help="known bad revision")
    parser.add_argument(
        "--scheduler",
        default=os.environ.get("PJTEST_SCHEDULER_URL", "http://127.0.0.1:8888"),
        help="scheduler URL, default: env PJTEST_SCHEDULER_URL or http://127.0.0.1:8888",
    )
    parser.add_argument(
        "--taskctl",
        default=default_taskctl_path(),
        help="path to taskctl.py, default: ./taskctl.py or env PJTEST_TASKCTL",
    )
    parser.add_argument("--priority", type=int, default=1000, help="task priority, default: 1000")
    parser.add_argument("--max-retry", type=int, default=0, help="task max retry, default: 0")
    parser.add_argument("--max-time", type=int, default=None, help="task max time seconds")
    parser.add_argument("--poll-interval", type=int, default=10, help="poll interval seconds")
    parser.add_argument("--wait-timeout", type=int, default=0, help="wait timeout per task, 0 means no limit")
    parser.add_argument("--http-timeout", type=int, default=10, help="HTTP timeout seconds")
    parser.add_argument("--taskctl-timeout", type=int, default=60, help="taskctl command timeout seconds")
    parser.add_argument(
        "--confirm-fail",
        type=int,
        default=1,
        help="repeat a failed revision this many total times, default: 1",
    )
    parser.add_argument(
        "--skip-boundary-check",
        action="store_true",
        help="do not run the initial good/bad boundary checks",
    )
    parser.add_argument(
        "--keep-tasks",
        action="store_true",
        help="keep all generated PJTest tasks",
    )
    parser.add_argument(
        "--delete-all",
        action="store_true",
        help="delete even the final good/bad tasks after printing result",
    )
    parser.add_argument(
        "--keep-case-list",
        action="store_true",
        help="keep the temporary one-case list file",
    )
    parser.add_argument(
        "--tmp-dir",
        default="/tmp",
        help="temporary directory for the one-case list file",
    )
    parser.add_argument(
        "--name-prefix",
        default="bisect",
        help="task name prefix, default: bisect",
    )
    return parser


def main():
    """Program entry."""
    args = build_parser().parse_args()

    if args.delete_all and args.keep_tasks:
        print("ERROR: --delete-all conflicts with --keep-tasks", file=sys.stderr)
        return 1

    taskctl_path = Path(args.taskctl).expanduser()
    if not taskctl_path.is_file():
        found = shutil.which(args.taskctl)
        if found:
            args.taskctl = found
        else:
            print("ERROR: taskctl.py not found: %s" % args.taskctl, file=sys.stderr)
            return 1
    else:
        args.taskctl = str(taskctl_path.resolve())

    try:
        return BisectRunner(args).run()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except BisectError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    except Exception as exc:
        print("ERROR: unexpected: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
