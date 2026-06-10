#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PJTest HTTP worker.

Run this on worker hosts such as tiger, kangqiao, and yangpu.  The worker does
not access SQLite.  It only talks to the central scheduler through HTTP:

    1. Register itself.
    2. Send heartbeat.
    3. Pull one task example.
    4. Install the scheduler-selected GalaxCore zip.
    5. Update flow_config.
    6. Run ./run.sh <target_arg>.
    7. Report result to scheduler.

Usage:
    ./worker.py --scheduler http://192.168.10.11:8888 --worker-name tiger --shell csh
    ./worker.py --scheduler http://192.168.10.11:8888 --worker-name tiger --jobs 8
    ./worker.py --scheduler http://192.168.10.11:8888 --worker-name tiger --jobs 8 --once
    ./worker.py --update-test2
    ./worker.py --recheckout-slots
"""

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

try:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
except ImportError:  # pragma: no cover, Python 2 fallback is not expected.
    from urllib2 import Request, urlopen, HTTPError, URLError


LOG_ROOT = Path(os.environ.get(
    "PJTEST_WORKER_LOG_ROOT",
    "/home/user3/PJTest/logs",
))

DEFAULT_WORKER_JOBS = int(os.environ.get("PJTEST_WORKER_JOBS", "8"))
WORKER_SLOT_ROOT = Path(os.environ.get(
    "PJTEST_WORKER_SLOT_ROOT",
    "/home/user3/PJTest/worker_slots",
))
SLOT_SVN_URL = os.environ.get(
    "PJTEST_SLOT_SVN_URL",
    "http://192.168.10.10/svn/galaxcore/galaxcore",
)
SVN_COMMAND_TIMEOUT = int(os.environ.get("PJTEST_SVN_COMMAND_TIMEOUT", "0"))
CLEAN_AFTER_RUN = os.environ.get("PJTEST_CLEAN_AFTER_RUN", "1") != "0"
CLEAN_TIMEOUT = int(os.environ.get("PJTEST_CLEAN_TIMEOUT", "600"))
FLOW_CONFIG_IGNORE_KEYS = set(
    item.strip()
    for item in os.environ.get("PJTEST_FLOW_CONFIG_IGNORE_KEYS", "enable_copy").split(",")
    if item.strip()
)

_slot_prepare_lock = threading.Lock()

DEFAULT_SCHEDULER_URL = (
    os.environ.get("SCHEDULER_URL")
    or os.environ.get("PJTEST_SCHEDULER_URL")
    or "http://192.168.10.11:8888"
).rstrip("/")

DEFAULT_WORKER_NAME = (
    os.environ.get("WORKER_NAME")
    or os.environ.get("PJTEST_WORKER_NAME")
    or None
)

SHARE_ZIP_DIR = Path(os.environ.get(
    "SHARE_ZIP_DIR",
    "/home/xshare/zw_cache/distributed_test_system/GalaxCore_bin/zip",
))

DEFAULT_DUMP_JSON = os.environ.get("PJTEST_DUMP_JSON", "0") == "1"

REPORT_RETRY_INTERVAL = int(os.environ.get("PJTEST_REPORT_RETRY_INTERVAL", "10"))
REPORT_MAX_RETRIES = int(os.environ.get("PJTEST_REPORT_MAX_RETRIES", "0"))

PROTOBUF_LIB_DIR = os.environ.get(
    "PROTOBUF_LIB_DIR",
    "/home/fpga/lib/protobuf-3.9.0/lib",
)

BIN_RELATIVE_DIR = os.environ.get("BIN_RELATIVE_DIR", "bin/Linux_64")
TARGET_BINARY_ENV = (
    os.environ.get("TARGET_BINARY")
    or os.environ.get("GALAXCORE_TARGET_BINARY")
)
FLOW_TARGET_DIR_ENV = (
    os.environ.get("FLOW_TARGET_DIR")
    or os.environ.get("GALAXCORE_FLOW_DIR")
)

IGNORE_RUN_SH_RC = os.environ.get("GALAXCORE_IGNORE_RUN_RC", "0") != "0"


class WorkerState(object):
    """Thread-safe worker state shared by the heartbeat thread."""

    def __init__(self, worker_name):
        self.worker_name = worker_name
        self.status = "idle"
        self.task_id = None
        self.example_id = None
        self.attempt_id = None
        self.message = "worker initialized"
        self.lock = threading.Lock()

    def set(self, status, task_id=None, example_id=None, attempt_id=None, message=None):
        """Update current worker state."""
        with self.lock:
            self.status = status
            self.task_id = task_id
            self.example_id = example_id
            self.attempt_id = attempt_id
            if message is not None:
                self.message = message

    def snapshot(self):
        """Return current worker state."""
        with self.lock:
            return {
                "status": self.status,
                "task_id": self.task_id,
                "example_id": self.example_id,
                "attempt_id": self.attempt_id,
                "message": self.message,
            }


def local_now():
    """Return local time string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_console_lock = threading.Lock()


def console_line(worker_name, message, stream=None):
    """Print one compact, thread-safe console line."""
    if stream is None:
        stream = sys.stdout
    prefix = "[%s]" % local_now()
    if worker_name:
        prefix += " [%s]" % worker_name
    with _console_lock:
        stream.write("%s %s\n" % (prefix, message))
        stream.flush()


def console_error(worker_name, message):
    """Print one compact, thread-safe error line."""
    console_line(worker_name, message, stream=sys.stderr)


_active_processes = {}
_active_processes_lock = threading.Lock()


def register_active_process(worker_name, proc, label):
    """Track a subprocess so shutdown timeout can kill it."""
    with _active_processes_lock:
        _active_processes[(worker_name, label)] = proc


def unregister_active_process(worker_name, label):
    """Stop tracking a subprocess."""
    with _active_processes_lock:
        _active_processes.pop((worker_name, label), None)


def kill_active_processes(reason):
    """Kill all tracked subprocess groups."""
    with _active_processes_lock:
        items = list(_active_processes.items())

    for (worker_name, label), proc in items:
        if proc.poll() is None:
            console_error(worker_name, "KILL active_process=%s reason=%s" % (label, reason))
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass

    time.sleep(3)

    for (worker_name, label), proc in items:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass


_shutdown_signal_count = 0


def install_shutdown_signal_handlers(shutdown_event):
    """Install graceful shutdown handlers for SIGINT/SIGTERM.

    First signal: stop pulling new tasks and wait for running slots to finish.
    Second signal: raise KeyboardInterrupt so the operator can force exit.
    """
    def _handler(signum, frame):
        global _shutdown_signal_count
        _shutdown_signal_count += 1

        if _shutdown_signal_count == 1:
            console_line(
                None,
                "shutdown requested signal=%s; stop pulling new tasks, wait running examples to report" % signum,
            )
            shutdown_event.set()
            return

        console_error(None, "second shutdown signal received; kill active processes and force interrupt")
        kill_active_processes("second shutdown signal")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def is_shutdown_requested(args):
    """Return True if this process is draining/shutting down."""
    event = getattr(args, "shutdown_event", None)
    return bool(event and event.is_set())


def wait_or_shutdown(args, seconds):
    """Sleep until timeout or shutdown request. Return True if shutdown was requested."""
    event = getattr(args, "shutdown_event", None)
    if event:
        return event.wait(seconds)
    time.sleep(seconds)
    return False


def short_task_id(value):
    """Shorten task/example/attempt ids for console display."""
    text = str(value or "-")
    return text if len(text) <= 18 else text[:18]


def short_target(value, max_len=80):
    """Shorten a target path for console display."""
    text = str(value or "-")
    if len(text) <= max_len:
        return text
    return "..." + text[-(max_len - 3):]


def get_default_worker_name():
    """Return default worker name from hostname."""
    return socket.gethostname().split(".")[0]


def make_url(base_url, path):
    """Join scheduler base URL and API path."""
    return base_url.rstrip("/") + path


def print_json_block(title, data):
    """Print a readable JSON block for debugging HTTP payloads."""
    with _console_lock:
        print("[%s] %s" % (local_now(), title))
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        sys.stdout.flush()


def http_json(base_url, path, data=None, timeout=30, dump_json=False):
    """Send JSON request and return decoded JSON response."""
    url = make_url(base_url, path)

    if data is None:
        req = Request(url)
    else:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=body)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Content-Length", str(len(body)))
        if dump_json:
            print_json_block("HTTP POST %s request" % path, data)

    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError("HTTP %s %s: %s" % (exc.code, url, raw))
    except URLError as exc:
        raise RuntimeError("HTTP request failed %s: %s" % (url, exc))

    if not raw.strip():
        result = {}
    else:
        result = json.loads(raw)

    if dump_json:
        print_json_block("HTTP %s response" % path, result)

    return result


def register_worker(scheduler_url, worker_name, capabilities, dump_json=False):
    """Register worker with scheduler."""
    return http_json(
        scheduler_url,
        "/api/worker/register",
        {
            "worker": worker_name,
            "hostname": socket.gethostname(),
            "capabilities": capabilities,
        },
        dump_json=dump_json,
    )


def send_heartbeat(scheduler_url, worker_name, state):
    """Send one heartbeat to scheduler."""
    snapshot = state.snapshot()
    payload = {
        "worker": worker_name,
        "hostname": socket.gethostname(),
        "status": snapshot["status"],
        "task_id": snapshot["task_id"],
        "example_id": snapshot["example_id"],
        "attempt_id": snapshot["attempt_id"],
        "message": snapshot["message"],
    }
    return http_json(scheduler_url, "/api/worker/heartbeat", payload, timeout=10)


def heartbeat_loop(scheduler_url, worker_name, state, stop_event, interval):
    """Heartbeat thread."""
    while not stop_event.is_set():
        try:
            send_heartbeat(scheduler_url, worker_name, state)
        except Exception as exc:
            console_error(worker_name, "heartbeat failed: %s" % exc)

        stop_event.wait(interval)


def pull_task(scheduler_url, worker_name, capabilities, dump_json=False):
    """Pull one task example from scheduler."""
    return http_json(
        scheduler_url,
        "/api/task/pull",
        {
            "worker": worker_name,
            "hostname": socket.gethostname(),
            "capabilities": capabilities,
        },
        dump_json=dump_json,
    )


def report_result(scheduler_url, payload, dump_json=False):
    """Report one execution result to scheduler."""
    return http_json(scheduler_url, "/api/task/report", payload, timeout=60, dump_json=dump_json)


def report_result_with_retry(scheduler_url, payload, state, args, worker_name):
    """Report one result and do not pull another task until it succeeds."""
    attempt = 0
    interval = int(getattr(args, "report_retry_interval", REPORT_RETRY_INTERVAL))
    max_retries = int(getattr(args, "report_max_retries", REPORT_MAX_RETRIES))
    example_id = payload.get("example_id")
    task_id = payload.get("task_id")
    attempt_id = payload.get("attempt_id")

    while True:
        try:
            response = report_result(
                scheduler_url,
                payload,
                dump_json=getattr(state, "dump_json", False),
            )
            if attempt > 0:
                console_line(
                    worker_name,
                    "REPORT_OK_AFTER_RETRY ex=%s retry_count=%d" % (
                        short_task_id(example_id),
                        attempt,
                    ),
                )
            return response
        except Exception as exc:
            attempt += 1
            state.set(
                "reporting",
                task_id=task_id,
                example_id=example_id,
                attempt_id=attempt_id,
                message="report retry %d: %s" % (attempt, exc),
            )
            console_error(
                worker_name,
                "REPORT_RETRY ex=%s attempt=%d err=%s" % (
                    short_task_id(example_id),
                    attempt,
                    exc,
                ),
            )

            if max_retries > 0 and attempt >= max_retries:
                raise RuntimeError(
                    "report failed after %d retries for example %s: %s" % (
                        attempt,
                        example_id,
                        exc,
                    )
                )

            wait_seconds = max(1, interval)
            wait_or_shutdown(args, wait_seconds)


def shlex_quote(value):
    """Quote a shell argument."""
    import shlex
    return shlex.quote(str(value))


def format_config_value(value):
    """Format flow_config value for file output."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value)


def update_flow_config_file(work_root, flow_config):
    """Update work_root/flow_config using key value format.

    Existing lines like:
        enable_copy 0
        enable_copy = 0
    are rewritten as:
        enable_copy 1

    Tcl-style lines like:
        set enable_copy 0
    keep the leading 'set'.
    """
    if not flow_config:
        return False, "empty flow_config"

    config_path = Path(work_root) / "flow_config"
    lines = []

    if config_path.exists():
        with config_path.open("r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

    remaining = {
        key: value
        for key, value in dict(flow_config).items()
        if key not in FLOW_CONFIG_IGNORE_KEYS
    }

    if not remaining:
        ignored = ",".join(sorted(FLOW_CONFIG_IGNORE_KEYS)) or "-"
        return False, "all flow_config keys ignored: %s" % ignored
    new_lines = []

    set_re = re.compile(r"^(\s*set\s+)([A-Za-z_][A-Za-z0-9_]*)(\s+)(.*?)(\s*)$")
    kv_re = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(?:\s*=\s*|\s+)(.*?)(\s*)$")

    for line in lines:
        raw = line.rstrip("\n")

        if not raw.strip() or raw.lstrip().startswith("#"):
            new_lines.append(line)
            continue

        match = set_re.match(raw)
        if match and match.group(2) in remaining:
            key = match.group(2)
            value = format_config_value(remaining.pop(key))
            new_lines.append("%s%s%s%s%s\n" % (
                match.group(1),
                key,
                match.group(3),
                value,
                match.group(5),
            ))
            continue

        match = kv_re.match(raw)
        if match and match.group(2) in remaining:
            key = match.group(2)
            value = format_config_value(remaining.pop(key))
            new_lines.append("%s%s %s%s\n" % (
                match.group(1),
                key,
                value,
                match.group(4),
            ))
            continue

        new_lines.append(line)

    if remaining:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"

        if new_lines:
            new_lines.append("\n")
        new_lines.append("# Added by PJTest worker\n")

        for key in sorted(remaining.keys()):
            new_lines.append("%s %s\n" % (key, format_config_value(remaining[key])))

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        f.writelines(new_lines)

    return True, str(config_path)



def log_line(log, message, worker_name=None):
    """Write one timestamped line to a log file or compact console."""
    text = "[%s] %s\n" % (local_now(), message)
    if log is not None:
        log.write(text)
        log.flush()
    else:
        console_line(worker_name, message)


def run_process(argv, cwd=None, log=None, timeout=None, console_output=False):
    """Run an external command and collect stdout/stderr.

    Output is written to the log file when provided.  By default, command
    output is not streamed to the terminal because svn checkout/update can
    produce thousands of noisy lines.  Use console_output=True only when a
    command really needs to be visible on the terminal.
    """
    if log is not None:
        log_line(log, "run command: %s" % " ".join(shlex_quote(x) for x in argv))
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )

    output_lines = []
    start_time = time.time()
    try:
        for line in iter(proc.stdout.readline, ""):
            if line:
                output_lines.append(line.rstrip("\n"))
                if log is not None:
                    log.write(line)
                    log.flush()
                elif console_output:
                    with _console_lock:
                        sys.stdout.write(line)
                        sys.stdout.flush()

            if timeout and timeout > 0 and time.time() - start_time > timeout:
                try:
                    proc.kill()
                except Exception:
                    pass
                return 124, "\n".join(output_lines)

        rc = proc.wait()
        return rc, "\n".join(output_lines)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass


def slot_galaxcore_root(slot_root, slot_worker_name):
    """Return the GalaxCore working-copy root for one slot."""
    return Path(slot_root).expanduser().resolve() / slot_worker_name / "galaxcore"


def slot_checkout_status(path):
    """Return detailed status for a slot GalaxCore working copy."""
    path = Path(path).expanduser().resolve()
    return {
        "root": str(path),
        "root_exists": path.exists(),
        "svn_dir": (path / ".svn").is_dir(),
        "test2_dir": (path / "test2").is_dir(),
        "run_sh": (path / "test2" / "run.sh").is_file(),
        "flow_config": (path / "test2" / "flow_config").is_file(),
    }


def slot_has_valid_checkout(path):
    """Return True when a slot already looks like a usable SVN checkout.

    Keep this check intentionally minimal.  flow_config is not required for
    reuse because PJTest may rewrite or recreate slot-local flow_config before
    running an example.  Requiring flow_config here can make an otherwise valid
    checkout get deleted and checked out again.
    """
    status = slot_checkout_status(path)
    return status["svn_dir"] and status["test2_dir"] and status["run_sh"]


def svn_cleanup(path, log=None):
    """Run svn cleanup for a working-copy path."""
    if not shutil.which("svn"):
        raise RuntimeError("svn not found in PATH")
    rc, _ = run_process(["svn", "cleanup", str(path)], log=log, timeout=SVN_COMMAND_TIMEOUT)
    if rc != 0:
        raise RuntimeError("svn cleanup failed in %s with exit code %s" % (path, rc))


def svn_revert_flow_config(slot_root_path, log=None):
    """Revert slot-local flow_config before updating test2 to avoid SVN conflicts."""
    flow_config = Path(slot_root_path) / "test2" / "flow_config"
    if not flow_config.exists():
        return
    rc, _ = run_process(
        ["svn", "revert", str(flow_config)],
        log=log,
        timeout=SVN_COMMAND_TIMEOUT,
    )
    if rc != 0:
        raise RuntimeError("svn revert flow_config failed: %s" % flow_config)


def svn_update_test2(slot_root_path, log=None):
    """Update only slot/galaxcore/test2."""
    test2 = Path(slot_root_path) / "test2"
    if not test2.is_dir():
        raise RuntimeError("slot test2 not found for svn update: %s" % test2)
    svn_cleanup(test2, log=log)
    svn_revert_flow_config(slot_root_path, log=log)
    rc, _ = run_process(["svn", "up", str(test2)], log=log, timeout=SVN_COMMAND_TIMEOUT)
    if rc != 0:
        raise RuntimeError("svn up test2 failed in %s with exit code %s" % (test2, rc))


def svn_checkout_slot(svn_url, slot_root_path, log=None):
    """Checkout a full GalaxCore working copy for one slot."""
    if not shutil.which("svn"):
        raise RuntimeError("svn not found in PATH")

    slot_root_path = Path(slot_root_path).expanduser().resolve()
    slot_root_path.parent.mkdir(parents=True, exist_ok=True)

    if slot_root_path.exists() and any(slot_root_path.iterdir()):
        raise RuntimeError("slot path exists but is not a valid checkout: %s" % slot_root_path)

    log_line(log, "svn checkout slot: %s -> %s" % (svn_url, slot_root_path))
    rc, _ = run_process(
        ["svn", "co", svn_url, str(slot_root_path)],
        log=log,
        timeout=SVN_COMMAND_TIMEOUT,
    )
    if rc != 0:
        raise RuntimeError("svn checkout failed for %s with exit code %s" % (slot_root_path, rc))


def prepare_slot_checkout(slot_worker_name, args, log=None):
    """Ensure one slot has a full GalaxCore SVN working copy.

    Normal startup reuses existing slot checkouts.  Terminal output is kept
    compact; detailed SVN output is intentionally suppressed unless a log file
    is passed in.
    """
    root = slot_galaxcore_root(args.slot_root, slot_worker_name)

    with _slot_prepare_lock:
        if args.recheckout_slots and root.exists():
            log_line(log, "slot recheckout: remove %s" % root, worker_name=slot_worker_name)
            shutil.rmtree(str(root), ignore_errors=True)

        if not slot_has_valid_checkout(root):
            if root.exists():
                log_line(log, "slot checkout incomplete, remove %s" % root, worker_name=slot_worker_name)
                shutil.rmtree(str(root), ignore_errors=True)
            log_line(log, "slot checkout start", worker_name=slot_worker_name)
            svn_checkout_slot(args.svn_url, root, log=log)
            log_line(log, "slot checkout ready", worker_name=slot_worker_name)
        else:
            log_line(log, "slot checkout reuse", worker_name=slot_worker_name)

        if args.update_test2:
            log_line(log, "slot test2 update start", worker_name=slot_worker_name)
            svn_update_test2(root, log=log)
            log_line(log, "slot test2 update done", worker_name=slot_worker_name)

    return root


def prepare_all_slots(args, base_worker_name, jobs):
    """Prepare all local slot checkouts before pulling any scheduler tasks."""
    args.slot_root = Path(args.slot_root).expanduser().resolve()
    args.slot_root.mkdir(parents=True, exist_ok=True)

    for slot_index in range(1, jobs + 1):
        slot_worker_name = make_slot_worker_name(base_worker_name, slot_index)
        prepare_slot_checkout(slot_worker_name, args, log=None)


def prepare_task_for_slot(task, worker_name, slot_root, log=None):
    """Return a slot-local task using the pre-created SVN checkout."""
    slot_root_path = slot_galaxcore_root(slot_root, worker_name)
    if not slot_has_valid_checkout(slot_root_path):
        raise RuntimeError("slot checkout is not ready: %s" % slot_root_path)

    slot_test2 = slot_root_path / "test2"
    slot_task = dict(task)
    slot_task["work_root"] = str(slot_test2)
    slot_task["install_root"] = str(slot_root_path)
    slot_task["cmd"] = "cd %s && ./run.sh %s" % (
        shlex_quote(str(slot_test2)),
        shlex_quote(str(slot_task["target_arg"])),
    )
    log_line(log, "slot work_root: %s" % slot_task["work_root"])
    log_line(log, "slot install_root: %s" % slot_task["install_root"])
    return slot_task


def run_clean_script(work_root, shell_name, log, reason="after run.sh", worker_name=None):
    """Run work_root/clean.sh without changing test result."""
    if not CLEAN_AFTER_RUN:
        log_line(log, "clean.sh skipped by PJTEST_CLEAN_AFTER_RUN=0 (%s)" % reason)
        return 0

    clean_path = Path(work_root) / "clean.sh"
    if not clean_path.is_file():
        log_line(log, "clean.sh not found, skip: %s" % clean_path)
        return 0

    if shell_name == "csh":
        shell_path = shutil.which("tcsh") or shutil.which("csh")
        if shell_path:
            cmd = "cd %s && ./clean.sh" % shlex_quote(str(work_root))
            argv = [shell_path, "-f", "-c", cmd]
        else:
            argv = ["/bin/bash", "-lc", "cd %s && ./clean.sh" % shlex_quote(str(work_root))]
    else:
        argv = ["/bin/bash", "-lc", "cd %s && ./clean.sh" % shlex_quote(str(work_root))]

    log_line(log, "clean.sh started (%s): %s" % (reason, clean_path))
    proc = subprocess.Popen(
        argv,
        stdout=log,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    process_label = "clean:%s" % reason
    register_active_process(worker_name or "clean", proc, process_label)
    start_time = time.time()
    rc = None

    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if CLEAN_TIMEOUT > 0 and time.time() - start_time > CLEAN_TIMEOUT:
                log_line(log, "clean.sh timeout after %d seconds" % CLEAN_TIMEOUT)
                kill_process_group(proc)
                rc = 124
                break
            time.sleep(1)
    finally:
        unregister_active_process(worker_name or "clean", process_label)

    log_line(log, "clean.sh finished (%s) with exit code %s" % (reason, rc))
    return rc

def prepare_log_file(worker_name, task):
    """Create a log file path for one example run."""
    day = datetime.now().strftime("%Y%m%d")
    log_dir = (
        LOG_ROOT
        / day
        / worker_name
        / task["task_id"]
        / task["example_id"]
        / task["attempt_id"]
    )
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "run.log"
    return str(log_dir), str(log_file)


def tail_text(path, max_lines=300, max_chars=16000):
    """Read tail text from a file."""
    if not path or not os.path.isfile(path):
        return ""

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return ""

    text = "".join(lines[-max_lines:])
    if len(text) > max_chars:
        text = text[-max_chars:]

    return text


def parse_env_file(path):
    """Parse vivado_runner result.env key/value output."""
    result = {}
    path = Path(path)
    if not path.is_file():
        return result

    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                result[key.strip()] = value.strip()
    except Exception:
        return {}

    return result


def sanitize_status_relpath(value):
    """Match vivado_runner sanitize_relpath from common.sh."""
    text = str(value or "").lstrip("/")
    text = re.sub(r"[^A-Za-z0-9._/-]", "_", text)
    return text


def expected_result_env_path(work_root, target_arg):
    """Return expected result.env path for a single run.tcl target."""
    work_root = Path(work_root).resolve()
    target = Path(str(target_arg))

    if target.is_absolute():
        try:
            run_tcl = target.resolve()
            case_rel = os.path.relpath(str(run_tcl.parent), str(work_root))
        except Exception:
            case_rel = str(target.parent)
    else:
        case_rel = str(target.parent)

    if case_rel in ("", "."):
        return None

    case_rel = sanitize_status_relpath(case_rel)
    return work_root / "vivado_runner" / "runtime" / "status" / case_rel / "result.env"


def find_result_env(work_root, target_arg):
    """Find vivado_runner result.env for the current single example."""
    expected = expected_result_env_path(work_root, target_arg)
    if expected and expected.is_file():
        return str(expected), parse_env_file(expected)

    status_root = Path(work_root) / "vivado_runner" / "runtime" / "status"
    if not status_root.is_dir():
        return None, {}

    target_text = str(target_arg or "")
    target_abs = None
    try:
        target_abs = str((Path(work_root) / target_text).resolve()) if not Path(target_text).is_absolute() else str(Path(target_text).resolve())
    except Exception:
        target_abs = None

    newest_path = None
    newest_mtime = -1

    for root, _dirs, files in os.walk(str(status_root)):
        if "result.env" not in files:
            continue
        path = Path(root) / "result.env"
        data = parse_env_file(path)
        run_tcl = data.get("RUN_TCL")

        if run_tcl and target_abs:
            try:
                if str(Path(run_tcl).resolve()) == target_abs:
                    return str(path), data
            except Exception:
                pass

        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0
        if mtime > newest_mtime:
            newest_mtime = mtime
            newest_path = path

    if newest_path:
        return str(newest_path), parse_env_file(newest_path)

    return None, {}


def classify_result(exit_code, timed_out, result_env):
    """Convert vivado_runner result.env into scheduler status/message."""
    case_status = str(result_env.get("STATUS") or "").upper()
    reason = result_env.get("REASON") or ""
    ret_code = result_env.get("RET_CODE")
    runtime_sec = result_env.get("RUNTIME_SEC")
    stage = result_env.get("STAGE") or ""

    if case_status == "PASS":
        status = "success"
    elif case_status == "TIMEOUT":
        status = "timeout"
    elif case_status in ("FAIL", "FAILED", "ERROR"):
        status = "failed"
    elif timed_out:
        status = "timeout"
    elif exit_code == 0:
        # If run.sh exited normally but no case result was produced, do not
        # silently mark the example as passed.  A single dispatched run.tcl
        # should always create result.env.
        status = "failed"
        if not reason:
            reason = "RESULT_ENV_MISSING"
    else:
        status = "failed"

    report_exit_code = exit_code
    if ret_code is not None:
        try:
            report_exit_code = int(ret_code)
        except Exception:
            report_exit_code = exit_code

    parts = []
    if case_status:
        parts.append("case_status=%s" % case_status)
    if reason:
        parts.append("reason=%s" % reason)
    if stage:
        parts.append("stage=%s" % stage)
    if runtime_sec is not None:
        parts.append("runtime=%s" % runtime_sec)
    if ret_code is not None:
        parts.append("ret=%s" % ret_code)

    message = " ".join(parts) if parts else status
    return status, report_exit_code, message, case_status, reason, runtime_sec


def find_first_named_path(root, names):
    """Find the first path under root whose name is in names."""
    root_path = Path(root)
    name_set = set(names)

    for name in names:
        candidate = root_path / name
        if candidate.exists():
            return candidate

    for current_root, dirnames, filenames in os.walk(str(root_path)):
        current_path = Path(current_root)

        for dirname in sorted(dirnames):
            if dirname in name_set:
                return current_path / dirname

        for filename in sorted(filenames):
            if filename in name_set:
                return current_path / filename

    return None


def replace_path(src, dst):
    """Replace destination with source path."""
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if dst.exists() or dst.is_symlink():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(str(dst))
        else:
            dst.unlink()

    if src.is_dir():
        shutil.copytree(str(src), str(dst), symlinks=True)
    else:
        shutil.copy2(str(src), str(dst))
        try:
            mode = dst.stat().st_mode
            dst.chmod(mode | 0o111)
        except Exception:
            pass


def resolve_zip_path_from_share(task):
    """Resolve GalaxCore zip from SHARE_ZIP_DIR when scheduler did not send zip_path."""
    revision = str(task.get("revision") or "").strip()
    if not revision:
        return None

    names = [
        "GalaxCore_%s.zip" % revision,
        "Galaxcore_%s.zip" % revision,
        "GalaxCore_r%s.zip" % revision,
        "GalaxCore-%s.zip" % revision,
    ]

    for name in names:
        candidate = SHARE_ZIP_DIR / name
        if candidate.is_file():
            return candidate

    return None


def resolve_install_targets(install_root):
    """Return GalaxCore binary and flow destination paths."""
    if TARGET_BINARY_ENV:
        target_binary = Path(TARGET_BINARY_ENV).expanduser().resolve()
    else:
        target_binary = (install_root / BIN_RELATIVE_DIR / "GalaxCore").resolve()

    if FLOW_TARGET_DIR_ENV:
        flow_dst = Path(FLOW_TARGET_DIR_ENV).expanduser().resolve()
    else:
        flow_dst = (install_root / "flow").resolve()

    return target_binary, flow_dst


def install_galaxcore_zip(task, install_root_override, log):
    """Extract selected GalaxCore zip and install GalaxCore binary/flow."""
    zip_path = task.get("zip_path") or task.get("galaxcore_zip_path")
    if not zip_path:
        zip_path = resolve_zip_path_from_share(task)

    if not zip_path:
        raise RuntimeError("missing zip_path in task payload and no fallback zip found in %s" % SHARE_ZIP_DIR)

    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise RuntimeError("zip file not found on worker: %s" % zip_path)

    work_root = Path(task["work_root"]).resolve()
    install_root = Path(install_root_override).resolve() if install_root_override else None
    if install_root is None:
        install_root = Path(task.get("install_root") or work_root.parent).resolve()

    target_binary, flow_dst = resolve_install_targets(install_root)
    tmp_root = Path(tempfile.mkdtemp(prefix="pjtest_zip_"))

    try:
        extract_dir = tmp_root / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        log.write("[%s] Extract zip: %s\n" % (local_now(), zip_path))
        log.write("[%s] Extract dir: %s\n" % (local_now(), extract_dir))
        log.write("[%s] Install root: %s\n" % (local_now(), install_root))
        log.write("[%s] Target binary: %s\n" % (local_now(), target_binary))
        log.write("[%s] Flow target: %s\n" % (local_now(), flow_dst))
        log.flush()

        with zipfile.ZipFile(str(zip_path), "r") as zf:
            zf.extractall(str(extract_dir))

        galaxcore_src = find_first_named_path(extract_dir, ["GalaxCore", "Galaxcore"])
        flow_src = find_first_named_path(extract_dir, ["flow"])

        if not galaxcore_src:
            raise RuntimeError("GalaxCore not found in zip: %s" % zip_path)

        if galaxcore_src.is_dir():
            raise RuntimeError("GalaxCore in zip is a directory, expected executable file: %s" % galaxcore_src)

        replace_path(galaxcore_src, target_binary)
        log.write("[%s] Updated GalaxCore binary: %s -> %s\n" % (
            local_now(),
            galaxcore_src,
            target_binary,
        ))

        if flow_src:
            replace_path(flow_src, flow_dst)
            log.write("[%s] Updated flow: %s -> %s\n" % (
                local_now(),
                flow_src,
                flow_dst,
            ))
        else:
            if flow_dst.exists():
                log.write("[%s] flow not found in zip, keep existing flow: %s\n" % (
                    local_now(),
                    flow_dst,
                ))
            else:
                log.write("[%s] flow not found in zip and no source flow directory, skip flow install: %s\n" % (
                    local_now(),
                    flow_dst,
                ))

        log.flush()
        return str(zip_path), str(install_root)

    finally:
        shutil.rmtree(str(tmp_root), ignore_errors=True)


def csh_dquote(value):
    """Quote one value for csh double quotes."""
    text = str(value)
    text = text.replace("\\", "\\\\")
    text = text.replace('"', '\\"')
    text = text.replace("$", "\\$")
    text = text.replace("`", "\\`")
    return '"%s"' % text


def csh_runtime_prefix():
    """Build csh environment bootstrap used by manual test terminals."""
    lines = [
        "if ( -f ~/.cshrc ) source ~/.cshrc",
    ]

    if PROTOBUF_LIB_DIR:
        lib_dir = str(PROTOBUF_LIB_DIR)
        lines.extend([
            "if ( $?LD_LIBRARY_PATH ) then",
            "    setenv LD_LIBRARY_PATH %s:$LD_LIBRARY_PATH" % csh_dquote(lib_dir)[1:-1],
            "else",
            "    setenv LD_LIBRARY_PATH %s" % csh_dquote(lib_dir),
            "endif",
        ])

    return "\n".join(lines)


def build_task_command(task):
    """Return the command that should run this example."""
    if task.get("cmd"):
        return str(task["cmd"])

    work_root = task["work_root"]
    target_arg = task["target_arg"]
    return "cd %s && ./run.sh %s" % (shlex_quote(work_root), shlex_quote(target_arg))


def build_runtime_env(task, worker_name):
    """Build environment variables passed to run.sh and vivado_runner."""
    task_id = str(task["task_id"])
    example_id = str(task["example_id"])
    attempt_id = str(task["attempt_id"])
    revision = str(task.get("revision") or "")
    zip_path = str(task.get("zip_path") or task.get("galaxcore_zip_path") or "")
    flow_config = str(Path(task["work_root"]) / "flow_config")

    return {
        "PJTEST_TASK_ID": task_id,
        "PJTEST_EXAMPLE_ID": example_id,
        "PJTEST_ATTEMPT_ID": attempt_id,
        "PJTEST_REVISION": revision,
        "PJTEST_GALAXCORE_ZIP": zip_path,
        "PJTEST_WORKER_NAME": worker_name,
        "PJTEST_SLOT_WORKER": worker_name,
        "GALAXCORE_RUN_MODE": "distributed",
        "DTS_RUN_MODE": "distributed",
        "GALAXCORE_BUILD_REVISION": revision,
        "GALAXCORE_REVISION": revision,
        "GALAXCORE_BUILD_ZIP": zip_path,
        "GALAXCORE_WORKER_NAME": worker_name,
        "GALAXCORE_TASK_ID": task_id,
        "GALAXCORE_EXAMPLE_ID": example_id,
        "GALAXCORE_ATTEMPT_ID": attempt_id,
        "GALAXCORE_FLOW_CONFIG": flow_config,
        "DTS_WORKER": worker_name,
        "DTS_SLOT_WORKER": worker_name,
        "DTS_TASK_ID": task_id,
        "DTS_EXAMPLE_ID": example_id,
        "DTS_ATTEMPT_ID": attempt_id,
        "DTS_FLOW_CONFIG": flow_config,
    }


def build_shell_argv(task, shell_name, worker_name):
    """Build shell argv for subprocess."""
    runtime_env = build_runtime_env(task, worker_name)
    cmd = build_task_command(task)

    if shell_name == "csh":
        csh_path = shutil.which("tcsh") or shutil.which("csh")
        if csh_path:
            setenv_lines = []
            for key in sorted(runtime_env.keys()):
                setenv_lines.append("setenv %s %s" % (key, csh_dquote(runtime_env[key])))

            ignore_flag = "1" if IGNORE_RUN_SH_RC else "0"
            command = "\n".join([
                csh_runtime_prefix(),
                "\n".join(setenv_lines),
                "echo \"[INFO] GALAXCORE_RUN_MODE: $GALAXCORE_RUN_MODE\"",
                "echo \"[INFO] GALAXCORE_BUILD_REVISION: $GALAXCORE_BUILD_REVISION\"",
                "echo \"[INFO] GALAXCORE_BUILD_ZIP: $GALAXCORE_BUILD_ZIP\"",
                cmd,
                "set run_rc = $status",
                "echo \"[INFO] task command finished with exit code $run_rc\"",
                "if ( %s == 1 ) then" % ignore_flag,
                "    echo \"[INFO] Ignore run.sh rc because GALAXCORE_IGNORE_RUN_RC is enabled\"",
                "    exit 0",
                "else",
                "    exit $run_rc",
                "endif",
            ])
            return [csh_path, "-f", "-c", command]

    bash_path = shutil.which("bash") or "/bin/bash"
    return [bash_path, "-lc", cmd]


def kill_process_group(proc):
    """Terminate and then kill a subprocess process group."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        time.sleep(3)
    except Exception:
        pass

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass


def run_command(task, shell_name, log, worker_name):
    """Run the scheduler-provided command and return (exit_code, timed_out)."""
    timeout = task.get("max_time")
    if timeout is not None:
        timeout = int(timeout)

    env = os.environ.copy()
    env.update(build_runtime_env(task, worker_name))
    if PROTOBUF_LIB_DIR:
        old_lib_path = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = (
            PROTOBUF_LIB_DIR + (":" + old_lib_path if old_lib_path else "")
        )

    argv = build_shell_argv(task, shell_name, worker_name)
    timed_out = False
    start_time = time.time()

    log.write("[%s] Command started\n" % local_now())
    log.write("task_id    : %s\n" % task["task_id"])
    log.write("example_id : %s\n" % task["example_id"])
    log.write("attempt_id : %s\n" % task["attempt_id"])
    log.write("revision   : %s\n" % task.get("revision"))
    log.write("work_root  : %s\n" % task["work_root"])
    log.write("target_arg : %s\n" % task["target_arg"])
    log.write("zip_path   : %s\n" % task.get("zip_path"))
    log.write("shell      : %s\n" % shell_name)
    log.write("argv       : %s\n" % " ".join(argv))
    log.write("-" * 80 + "\n")
    log.flush()

    proc = subprocess.Popen(
        argv,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        preexec_fn=os.setsid,
    )
    register_active_process(worker_name, proc, "run.sh")

    try:
        while True:
            return_code = proc.poll()
            if return_code is not None:
                break

            if timeout and timeout > 0 and time.time() - start_time > timeout:
                timed_out = True
                log.write("\n[%s] Timeout after %d seconds\n" % (local_now(), timeout))
                log.flush()
                kill_process_group(proc)
                return_code = 124
                break

            time.sleep(2)
    finally:
        unregister_active_process(worker_name, "run.sh")

    elapsed = time.time() - start_time
    log.write("\n" + "-" * 80 + "\n")
    log.write("[%s] Command finished\n" % local_now())
    log.write("exit_code  : %s\n" % return_code)
    log.write("timed_out  : %s\n" % timed_out)
    log.write("elapsed_sec: %.1f\n" % elapsed)
    log.flush()

    return return_code, timed_out


def run_one_task(scheduler_url, worker_name, task, shell_name, install_root_override, state, args):
    """Execute one pulled task example and report the result."""
    log_dir, log_file = prepare_log_file(worker_name, task)
    exit_code = 1
    timed_out = False
    status = "failed"
    message = ""
    start_time = time.time()

    state.set(
        "running",
        task_id=task["task_id"],
        example_id=task["example_id"],
        attempt_id=task["attempt_id"],
        message="running %s" % task["target_arg"],
    )

    console_line(worker_name, "START task=%s ex=%s target=%s" % (
        short_task_id(task.get("task_id")),
        short_task_id(task.get("example_id")),
        short_target(task.get("target_arg")),
    ))

    try:
        with open(log_file, "w", encoding="utf-8", errors="ignore") as log:
            log.write("[%s] PJTest worker started example\n" % local_now())

            slot_task = prepare_task_for_slot(
                task,
                worker_name,
                args.slot_root,
                log=log,
            )

            if getattr(args, "verbose_console", False):
                console_line(worker_name, "install zip")
            install_galaxcore_zip(slot_task, install_root_override, log)

            if getattr(args, "verbose_console", False):
                console_line(worker_name, "update flow_config")
            updated, config_message = update_flow_config_file(
                slot_task["work_root"],
                slot_task.get("flow_config") or {},
            )
            log.write("[%s] flow_config update: %s %s\n" % (
                local_now(),
                updated,
                config_message,
            ))
            log.flush()

            if getattr(args, "verbose_console", False):
                console_line(worker_name, "pre-clean slot")
            pre_clean_rc = run_clean_script(slot_task["work_root"], shell_name, log, reason="before run.sh", worker_name=worker_name)
            log.write("[%s] pre-clean exit_code: %s\n" % (local_now(), pre_clean_rc))
            log.flush()

            if getattr(args, "verbose_console", False):
                console_line(worker_name, "run command")
            exit_code, timed_out = run_command(slot_task, shell_name, log, worker_name)

            result_env_path, result_env = find_result_env(
                slot_task["work_root"],
                slot_task.get("target_arg"),
            )
            if result_env_path:
                log.write("[%s] vivado_runner result_env: %s\n" % (local_now(), result_env_path))
                log.write("[%s] vivado_runner result_data: %s\n" % (
                    local_now(),
                    json.dumps(result_env, ensure_ascii=False, sort_keys=True),
                ))
            else:
                log.write("[%s] vivado_runner result_env: not found\n" % local_now())
            log.flush()

            status, exit_code, message, case_status, case_reason, case_runtime = classify_result(
                exit_code,
                timed_out,
                result_env,
            )

            if getattr(args, "verbose_console", False):
                console_line(worker_name, "case result status=%s reason=%s" % (
                    case_status or status,
                    case_reason or "-",
                ))

            if getattr(args, "verbose_console", False):
                console_line(worker_name, "clean slot")
            run_clean_script(slot_task["work_root"], shell_name, log, reason="after run.sh", worker_name=worker_name)

    except Exception as exc:
        status = "failed"
        exit_code = 1
        timed_out = False
        message = str(exc)

        try:
            with open(log_file, "a", encoding="utf-8", errors="ignore") as log:
                log.write("\n[%s] Worker exception: %s\n" % (local_now(), exc))
        except Exception:
            pass

    log_tail = tail_text(log_file)

    report = {
        "worker": worker_name,
        "task_id": task["task_id"],
        "example_id": task["example_id"],
        "attempt_id": task["attempt_id"],
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "message": message,
        "case_status": result_env.get("STATUS") if "result_env" in locals() else None,
        "case_reason": result_env.get("REASON") if "result_env" in locals() else None,
        "case_runtime_sec": result_env.get("RUNTIME_SEC") if "result_env" in locals() else None,
        "case_ret_code": result_env.get("RET_CODE") if "result_env" in locals() else None,
        "result_env_file": result_env_path if "result_env_path" in locals() else None,
        "log_file": log_file,
        "log_tail": log_tail,
        "run_log_dir": log_dir,
        "report_dir": None,
    }

    report_result_with_retry(scheduler_url, report, state, args, worker_name)

    elapsed = time.time() - start_time
    console_line(worker_name, "DONE status=%s rc=%s timeout=%s sec=%.1f %s log=%s" % (
        status,
        exit_code,
        timed_out,
        elapsed,
        short_target(message, 70),
        log_file,
    ))

    state.set("idle", None, None, None, "last result: %s" % status)
    return status

def build_capabilities(args):
    """Build worker capabilities reported to scheduler."""
    return {
        "shell": args.shell,
        "install_root": args.install_root,
        "hostname": socket.gethostname(),
        "share_zip_dir": str(SHARE_ZIP_DIR),
        "protobuf_lib_dir": PROTOBUF_LIB_DIR,
        "ignore_run_sh_rc": int(IGNORE_RUN_SH_RC),
        "slot_root": str(args.slot_root),
        "svn_url": str(args.svn_url),
    }


def worker_loop(args):
    """Main worker loop."""
    worker_name = args.worker_name or DEFAULT_WORKER_NAME or get_default_worker_name()
    state = WorkerState(worker_name)
    state.dump_json = args.dump_json
    capabilities = build_capabilities(args)

    register_worker(args.scheduler, worker_name, capabilities, dump_json=args.dump_json)

    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(
        target=heartbeat_loop,
        args=(args.scheduler, worker_name, state, stop_event, args.heartbeat_interval),
    )
    heartbeat_thread.daemon = True
    heartbeat_thread.start()

    console_line(worker_name, "READY scheduler=%s" % args.scheduler)

    try:
        while not is_shutdown_requested(args):
            state.set("idle", None, None, None, "requesting task")

            try:
                response = pull_task(args.scheduler, worker_name, capabilities, dump_json=args.dump_json)
            except Exception as exc:
                console_error(worker_name, "PULL_FAIL %s" % exc)
                state.set("error", None, None, None, "pull failed: %s" % exc)
                if args.once:
                    return 1
                if wait_or_shutdown(args, args.interval):
                    break
                continue

            if is_shutdown_requested(args):
                break

            task = response.get("task")
            if not task:
                message = response.get("message") or "no task"
                state.set("idle", None, None, None, message)
                if args.once:
                    console_line(worker_name, "IDLE %s" % message)
                    return 0
                if wait_or_shutdown(args, args.interval):
                    break
                continue

            try:
                run_one_task(
                    args.scheduler,
                    worker_name,
                    task,
                    args.shell,
                    args.install_root,
                    state,
                    args,
                )
            except Exception:
                if args.once:
                    return 1

            if args.once:
                return 0

        state.set("stopping", None, None, None, "graceful shutdown")
        console_line(worker_name, "STOP no new tasks")
        return 0

    finally:
        stop_event.set()
        try:
            send_heartbeat(args.scheduler, worker_name, state)
        except Exception:
            pass



def make_slot_worker_name(base_worker_name, slot_index):
    """Build scheduler-visible worker name for one local slot."""
    return "%s_slot%d" % (base_worker_name, slot_index)


def clone_args_for_slot(args, slot_worker_name):
    """Clone argparse args for one slot thread."""
    slot_args = argparse.Namespace(**vars(args))
    slot_args.worker_name = slot_worker_name
    return slot_args


def shell_join(argv):
    """Return a shell-safe command line."""
    return " ".join(shlex_quote(item) for item in argv)


def build_single_slot_command(args, slot_index):
    """Build the command used by tmux to run exactly one slot."""
    script_path = Path(__file__).expanduser().resolve()
    argv = [
        sys.executable or "python3",
        str(script_path),
        "--single-slot",
        str(slot_index),
        "--scheduler",
        str(args.scheduler),
        "--worker-name",
        str(args.worker_name or DEFAULT_WORKER_NAME or get_default_worker_name()),
        "--shell",
        str(args.shell),
        "--slot-root",
        str(args.slot_root),
        "--svn-url",
        str(args.svn_url),
        "--interval",
        str(args.interval),
        "--heartbeat-interval",
        str(args.heartbeat_interval),
        "--report-retry-interval",
        str(args.report_retry_interval),
        "--report-max-retries",
        str(args.report_max_retries),
        "--shutdown-timeout",
        str(getattr(args, "shutdown_timeout", 0)),
    ]

    if args.install_root:
        argv.extend(["--install-root", str(args.install_root)])
    if args.update_test2:
        argv.append("--update-test2")
    if args.recheckout_slots:
        argv.append("--recheckout-slots")
    if args.once:
        argv.append("--once")
    if args.dump_json:
        argv.append("--dump-json")

    return "cd %s && %s" % (shlex_quote(str(Path.cwd())), shell_join(argv))


def launch_tmux_slots(args):
    """Launch one tmux session with one pane per worker slot."""
    tmux = shutil.which("tmux")
    if not tmux:
        raise RuntimeError("tmux not found in PATH; install tmux or use --single-slot N manually")

    base_worker_name = args.worker_name or DEFAULT_WORKER_NAME or get_default_worker_name()
    jobs = int(args.jobs)
    if jobs < 1:
        raise ValueError("--jobs must be >= 1")

    session_name = args.tmux_session or "pjtest_%s" % base_worker_name

    check_rc = subprocess.call(
        [tmux, "has-session", "-t", session_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check_rc == 0:
        raise RuntimeError(
            "tmux session already exists: %s. Attach with `tmux attach -t %s` or kill it with `tmux kill-session -t %s`."
            % (session_name, session_name, session_name)
        )

    first_cmd = build_single_slot_command(args, 1)
    subprocess.check_call([
        tmux,
        "new-session",
        "-d",
        "-s",
        session_name,
        "-n",
        "workers",
        first_cmd,
    ])

    for slot_index in range(2, jobs + 1):
        cmd = build_single_slot_command(args, slot_index)
        subprocess.check_call([tmux, "split-window", "-t", "%s:0" % session_name, cmd])
        subprocess.call([tmux, "select-layout", "-t", "%s:0" % session_name, "tiled"])

    subprocess.call([tmux, "select-layout", "-t", "%s:0" % session_name, "tiled"])

    console_line(None, "tmux session started: %s" % session_name)
    console_line(None, "attach command: tmux attach -t %s" % session_name)

    if os.environ.get("TMUX"):
        subprocess.check_call([tmux, "switch-client", "-t", session_name])
    else:
        subprocess.check_call([tmux, "attach-session", "-t", session_name])

    return 0


def single_slot_loop(args):
    """Run exactly one slot in the current process."""
    base_worker_name = args.worker_name or DEFAULT_WORKER_NAME or get_default_worker_name()
    slot_index = int(args.single_slot)
    if slot_index < 1:
        raise ValueError("--single-slot must be >= 1")

    args.slot_root = Path(args.slot_root).expanduser().resolve()
    args.slot_root.mkdir(parents=True, exist_ok=True)

    if not hasattr(args, "shutdown_event"):
        args.shutdown_event = threading.Event()
    install_shutdown_signal_handlers(args.shutdown_event)

    slot_worker_name = make_slot_worker_name(base_worker_name, slot_index)
    console_line(slot_worker_name, "single-slot mode base=%s slot=%d scheduler=%s" % (
        base_worker_name,
        slot_index,
        args.scheduler,
    ))

    prepare_slot_checkout(slot_worker_name, args, log=None)
    slot_args = clone_args_for_slot(args, slot_worker_name)
    return worker_loop(slot_args)


def worker_manager_loop(args):
    """Start multiple local worker slots on this host."""
    base_worker_name = args.worker_name or DEFAULT_WORKER_NAME or get_default_worker_name()
    jobs = int(args.jobs)
    if jobs < 1:
        raise ValueError("--jobs must be >= 1")

    args.slot_root = Path(args.slot_root).expanduser().resolve()
    args.slot_root.mkdir(parents=True, exist_ok=True)

    if not hasattr(args, "shutdown_event"):
        args.shutdown_event = threading.Event()
    install_shutdown_signal_handlers(args.shutdown_event)

    console_line(None, "manager START base=%s jobs=%d scheduler=%s" % (
        base_worker_name,
        jobs,
        args.scheduler,
    ))
    console_line(None, "slots=%s update_test2=%s recheckout=%s" % (
        args.slot_root,
        args.update_test2,
        args.recheckout_slots,
    ))

    prepare_all_slots(args, base_worker_name, jobs)

    results = {}
    result_lock = threading.Lock()

    def run_slot(slot_index):
        slot_worker_name = make_slot_worker_name(base_worker_name, slot_index)
        slot_args = clone_args_for_slot(args, slot_worker_name)
        console_line(slot_worker_name, "slot thread started")
        rc = worker_loop(slot_args)
        with result_lock:
            results[slot_worker_name] = rc

    threads = []
    for slot_index in range(1, jobs + 1):
        thread = threading.Thread(target=run_slot, args=(slot_index,))
        thread.name = make_slot_worker_name(base_worker_name, slot_index)
        thread.daemon = True
        thread.start()
        threads.append(thread)

    try:
        if args.once:
            for thread in threads:
                thread.join()
            return 0 if all(value == 0 for value in results.values()) else 1

        while not args.shutdown_event.is_set():
            time.sleep(1)

        console_line(None, "manager DRAIN waiting for running slots to finish")
        deadline = None
        if args.shutdown_timeout and int(args.shutdown_timeout) > 0:
            deadline = time.time() + int(args.shutdown_timeout)

        last_notice = 0
        while any(thread.is_alive() for thread in threads):
            alive = [thread.name or "slot" for thread in threads if thread.is_alive()]
            now = time.time()
            if now - last_notice >= 15:
                console_line(None, "manager DRAIN alive_slots=%d" % len(alive))
                last_notice = now

            if deadline is not None and now >= deadline:
                console_error(None, "shutdown timeout reached; kill active run/clean processes and exit")
                kill_active_processes("shutdown timeout")
                os._exit(130)

            for thread in threads:
                thread.join(timeout=1)

        console_line(None, "manager STOP all slots exited")
        return 0

    except KeyboardInterrupt:
        args.shutdown_event.set()
        console_line(None, "Interrupted; shutdown requested")
        return 130

def build_parser():
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description="PJTest HTTP worker")
    parser.add_argument(
        "--scheduler",
        default=DEFAULT_SCHEDULER_URL,
        help="scheduler base URL, default: env SCHEDULER_URL/PJTEST_SCHEDULER_URL or http://192.168.10.11:8888",
    )
    parser.add_argument(
        "--worker-name",
        default=DEFAULT_WORKER_NAME,
        help="worker name, default: env WORKER_NAME/PJTEST_WORKER_NAME or hostname short name",
    )
    parser.add_argument(
        "--shell",
        choices=["bash", "csh"],
        default="csh",
        help="shell used to run run.sh, default: csh",
    )
    parser.add_argument(
        "--install-root",
        default=None,
        help="where to replace GalaxCore and flow; default: parent of work_root",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_WORKER_JOBS,
        help="local concurrent worker slots, default: env PJTEST_WORKER_JOBS or 8",
    )
    parser.add_argument(
        "--slot-root",
        default=str(WORKER_SLOT_ROOT),
        help="root directory for isolated worker slots, default: /home/user3/PJTest/worker_slots",
    )
    parser.add_argument(
        "--svn-url",
        default=SLOT_SVN_URL,
        help="SVN URL used to initialize slot GalaxCore checkouts, default: http://192.168.10.10/svn/galaxcore/galaxcore",
    )
    parser.add_argument(
        "--update-test2",
        action="store_true",
        help="before starting workers, run svn cleanup/revert flow_config/svn up on each slot galaxcore/test2",
    )
    parser.add_argument(
        "--recheckout-slots",
        action="store_true",
        help="delete this worker's slot checkouts and run svn co again before starting",
    )
    parser.add_argument(
        "--single-slot",
        type=int,
        default=None,
        help="run only one slot in this process, for example: --single-slot 3",
    )
    parser.add_argument(
        "--tmux-slots",
        action="store_true",
        help="open one tmux session with one pane per slot; each pane runs --single-slot N",
    )
    parser.add_argument(
        "--tmux-session",
        default=None,
        help="tmux session name used with --tmux-slots; default: pjtest_<worker-name>",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="seconds between pull requests when idle, default: 5",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=int,
        default=30,
        help="heartbeat interval seconds, default: 30",
    )
    parser.add_argument(
        "--report-retry-interval",
        type=int,
        default=REPORT_RETRY_INTERVAL,
        help="seconds between result report retries, default: env PJTEST_REPORT_RETRY_INTERVAL or 10",
    )
    parser.add_argument(
        "--report-max-retries",
        type=int,
        default=REPORT_MAX_RETRIES,
        help="max result report retries, default: env PJTEST_REPORT_MAX_RETRIES or 0 for forever",
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=int,
        default=int(os.environ.get("PJTEST_SHUTDOWN_TIMEOUT", "30")),
        help="seconds to wait for running slots during graceful shutdown, default: 30; 0 means wait until current examples finish",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="pull and execute at most one example",
    )
    parser.add_argument(
        "--verbose-console",
        action="store_true",
        help="print per-stage slot messages on console; default only prints START/DONE/errors",
    )
    parser.add_argument(
        "--dump-json",
        action="store_true",
        default=DEFAULT_DUMP_JSON,
        help="print register/pull/report JSON request and response",
    )
    return parser


def main():
    """Program entry."""
    args = build_parser().parse_args()

    try:
        if args.tmux_slots:
            return launch_tmux_slots(args)
        if args.single_slot is not None:
            return single_slot_loop(args)
        return worker_manager_loop(args)
    except KeyboardInterrupt:
        console_line(None, "Interrupted.")
        return 130
    except Exception as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
