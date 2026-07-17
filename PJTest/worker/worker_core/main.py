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
    ./worker.py --check
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
import fcntl
from datetime import datetime
from pathlib import Path

try:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
except ImportError:  # pragma: no cover, Python 2 fallback is not expected.
    from urllib2 import Request, urlopen, HTTPError, URLError


WORKER_ROOT = Path(__file__).resolve().parents[1]
PJTEST_ROOT = WORKER_ROOT.parent

LOG_ROOT = Path(os.environ.get(
    "PJTEST_WORKER_LOG_ROOT",
    str(PJTEST_ROOT / "logs"),
))

PENDING_REPORT_ROOT = Path(os.environ.get(
    "PJTEST_PENDING_REPORT_ROOT",
    str(PJTEST_ROOT / "pending_reports"),
))

DEFAULT_WORKER_JOBS = int(os.environ.get("PJTEST_WORKER_JOBS", "8"))
WORKER_SLOT_ROOT = Path(os.environ.get(
    "PJTEST_WORKER_SLOT_ROOT",
    str(WORKER_ROOT / "worker_slots"),
))
WORKER_PROCESS_LOCK_DIR = Path(os.environ.get(
    "PJTEST_WORKER_PROCESS_LOCK_DIR",
    str(WORKER_ROOT / "tmp"),
))
RUN_SH_LOCK_DIR = Path(os.environ.get(
    "RUN_SH_LOCK_DIR",
    str(WORKER_ROOT / "tmp"),
))
SLOT_BUSY_BACKOFF_SEC = int(os.environ.get(
    "PJTEST_SLOT_BUSY_BACKOFF_SEC",
    "60",
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
    "/home/xshare/zhouwei_runcache/GalaxCore/zip",
))

DEFAULT_DUMP_JSON = os.environ.get("PJTEST_DUMP_JSON", "0") == "1"

REPORT_RETRY_INTERVAL = int(os.environ.get("PJTEST_REPORT_RETRY_INTERVAL", "10"))
REPORT_MAX_RETRIES = int(os.environ.get("PJTEST_REPORT_MAX_RETRIES", "0"))
PULL_TIMEOUT = int(os.environ.get("PJTEST_PULL_TIMEOUT", "60"))

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


class HttpJsonError(RuntimeError):
    """HTTP JSON request error with status code and parsed scheduler message."""

    def __init__(self, status_code, url, raw_body):
        self.status_code = int(status_code)
        self.url = str(url)
        self.raw_body = str(raw_body or "")
        self.error_message = self._parse_error_message(self.raw_body)

        RuntimeError.__init__(
            self,
            "HTTP %s %s: %s"
            % (
                self.status_code,
                self.url,
                self.raw_body,
            ),
        )

    @staticmethod
    def _parse_error_message(raw_body):
        """Extract the scheduler error field from a JSON response body."""
        raw_body = str(raw_body or "").strip()

        if not raw_body:
            return ""

        try:
            data = json.loads(raw_body)
        except Exception:
            return raw_body

        if isinstance(data, dict):
            return str(data.get("error") or data.get("message") or raw_body).strip()

        return raw_body


class WorkerSlotLockError(RuntimeError):
    """Raised when another worker.py process already owns this worker slot."""


class SlotBusyError(RuntimeError):
    """Raised when the slot-level run lock is already held."""

    def __init__(self, lock_file):
        self.lock_file = str(lock_file)
        RuntimeError.__init__(
            self,
            "Current slot already has a run.sh task running: %s" % self.lock_file,
        )


def sanitize_lock_tag(value):
    """Return the same safe lock tag format used by run.sh."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown"))


_held_worker_process_locks = {}
_held_worker_process_locks_lock = threading.Lock()


def acquire_flock(lock_file, description, blocking=False):
    """Open and lock one file, returning the file handle that holds the lock."""
    lock_file = Path(lock_file).expanduser().resolve()
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(str(lock_file), "a+")
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB

    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        try:
            handle.seek(0)
            owner = handle.read().strip()
        except Exception:
            owner = ""
        handle.close()
        detail = "already locked"
        if owner:
            detail += "; owner=%s" % owner.replace("\n", " ")[:300]
        raise WorkerSlotLockError("%s lock %s %s" % (description, lock_file, detail))

    handle.seek(0)
    handle.truncate()
    handle.write(
        "pid=%s hostname=%s started_at=%s description=%s\n"
        % (os.getpid(), socket.gethostname(), local_now(), description)
    )
    handle.flush()
    os.fsync(handle.fileno())
    return handle


def acquire_worker_process_lock(worker_name):
    """Ensure only one worker.py process/thread owns one scheduler-visible slot."""
    safe = sanitize_lock_tag(worker_name)
    lock_file = WORKER_PROCESS_LOCK_DIR / ("pjtest_worker_%s.lock" % safe)

    with _held_worker_process_locks_lock:
        if worker_name in _held_worker_process_locks:
            raise WorkerSlotLockError("duplicate worker loop in this process: %s" % worker_name)
        handle = acquire_flock(lock_file, "worker-process:%s" % worker_name, blocking=False)
        _held_worker_process_locks[worker_name] = handle
        return handle


def release_worker_process_lock(worker_name):
    """Release a worker-process lock."""
    with _held_worker_process_locks_lock:
        handle = _held_worker_process_locks.pop(worker_name, None)
    if handle is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            handle.close()
        except Exception:
            pass


def run_sh_lock_file_for_worker(worker_name):
    """Return the slot run.sh lock path before any destructive slot operation."""
    safe = sanitize_lock_tag(worker_name)
    return RUN_SH_LOCK_DIR.expanduser().resolve() / ("galaxcore_run_%s.lock" % safe)


def acquire_slot_run_lock(worker_name, log=None):
    """Hold the run.sh slot lock across install/pre-clean/run/post-clean.

    run.sh itself honors RUN_SH_LOCK_HELD=1, so the child process does not try
    to lock the same file again while the worker process keeps the lock held.
    """
    lock_file = run_sh_lock_file_for_worker(worker_name)
    try:
        handle = acquire_flock(lock_file, "run-slot:%s" % worker_name, blocking=False)
    except WorkerSlotLockError:
        if log is not None:
            log_line(log, "Current slot already has a run.sh task running: %s" % lock_file)
        raise SlotBusyError(lock_file)
    if log is not None:
        log_line(log, "slot run lock acquired: %s" % lock_file)
    return handle, str(lock_file)


def release_slot_run_lock(handle, log=None):
    """Release the slot run lock held across one example."""
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        path = handle.name
    except Exception:
        path = "-"
    try:
        handle.close()
    except Exception:
        pass
    if log is not None:
        log_line(log, "slot run lock released: %s" % path)


def slot_is_busy_before_pull(worker_name):
    """Return True if this slot already has an active run lock.

    This guard runs before pulling a scheduler task.  If an old run.sh or a
    duplicate legacy worker still owns the slot lock, the worker must not pull
    another example and create an immediate infra_failed/requeue loop.
    """
    handle = None
    try:
        handle, _lock_file = acquire_slot_run_lock(worker_name, log=None)
        return False
    except SlotBusyError:
        return True
    finally:
        release_slot_run_lock(handle, log=None)


def is_report_target_gone(exc):
    """Return True when retrying a report can never succeed."""
    if not isinstance(exc, HttpJsonError):
        return False

    error_message = exc.error_message.strip().lower()
    permanent_errors_by_status = {
        400: {
            "attempt assignment mismatch",
            "attempt worker mismatch",
            "example task mismatch",
            "invalid status",
            "missing attempt_id",
            "missing example_id",
            "missing task_id",
            "missing worker",
        },
        404: {
            "attempt not found",
            "task not found",
            "example not found",
        },
    }
    return error_message in permanent_errors_by_status.get(exc.status_code, set())


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
        raise HttpJsonError(exc.code, url, raw)
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


def pull_task(
    scheduler_url,
    worker_name,
    capabilities,
    dump_json=False,
    timeout=PULL_TIMEOUT,
):
    """Pull one task example using an explicit, configurable timeout."""
    return http_json(
        scheduler_url,
        "/api/task/pull",
        {
            "worker": worker_name,
            "hostname": socket.gethostname(),
            "capabilities": capabilities,
        },
        timeout=timeout,
        dump_json=dump_json,
    )


def report_result(scheduler_url, payload, dump_json=False):
    """Report one execution result to scheduler."""
    return http_json(scheduler_url, "/api/task/report", payload, timeout=60, dump_json=dump_json)


def report_result_with_retry(scheduler_url, payload, state, args, worker_name):
    """Report one result, abandoning only permanently deleted assignments."""
    retry_count = 0
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

            if retry_count > 0:
                console_line(
                    worker_name,
                    "REPORT_OK_AFTER_RETRY ex=%s retry_count=%d"
                    % (
                        short_task_id(example_id),
                        retry_count,
                    ),
                )

            return response

        except HttpJsonError as exc:
            if is_report_target_gone(exc):
                state.set(
                    "idle",
                    task_id=None,
                    example_id=None,
                    attempt_id=None,
                    message=(
                        "orphan report discarded: %s"
                        % exc.error_message
                    ),
                )

                console_error(
                    worker_name,
                    "REPORT_DISCARDED task=%s ex=%s attempt=%s "
                    "reason=%s"
                    % (
                        short_task_id(task_id),
                        short_task_id(example_id),
                        short_task_id(attempt_id),
                        exc.error_message or "report target not found",
                    ),
                )

                return {
                    "ok": False,
                    "discarded": True,
                    "error": exc.error_message,
                    "http_status": exc.status_code,
                    "task_id": task_id,
                    "example_id": example_id,
                    "attempt_id": attempt_id,
                }

            retry_count += 1
            state.set(
                "reporting",
                task_id=task_id,
                example_id=example_id,
                attempt_id=attempt_id,
                message="report retry %d: %s" % (retry_count, exc),
            )
            console_error(
                worker_name,
                "REPORT_RETRY ex=%s attempt=%d err=%s"
                % (
                    short_task_id(example_id),
                    retry_count,
                    exc,
                ),
            )

        except Exception as exc:
            retry_count += 1
            state.set(
                "reporting",
                task_id=task_id,
                example_id=example_id,
                attempt_id=attempt_id,
                message="report retry %d: %s" % (retry_count, exc),
            )
            console_error(
                worker_name,
                "REPORT_RETRY ex=%s attempt=%d err=%s"
                % (
                    short_task_id(example_id),
                    retry_count,
                    exc,
                ),
            )

        if max_retries > 0 and retry_count >= max_retries:
            raise RuntimeError(
                "report failed after %d retries for example %s"
                % (
                    retry_count,
                    example_id,
                )
            )

        wait_seconds = max(1, interval)
        wait_or_shutdown(args, wait_seconds)



def pending_report_dir(worker_name):
    """Return pending report directory for one worker slot."""
    return PENDING_REPORT_ROOT / str(worker_name)


def pending_report_path(worker_name, payload):
    """Return stable pending report file path for one payload."""
    example_id = str(payload.get("example_id") or "unknown_example")
    attempt_id = str(payload.get("attempt_id") or "unknown_attempt")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", "%s_%s.json" % (example_id, attempt_id))
    return pending_report_dir(worker_name) / safe_name


def atomic_write_json(path, data):
    """Write JSON atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(".%s.%s.tmp" % (path.name, os.getpid()))
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(str(tmp_path), str(path))


def save_pending_report(worker_name, payload):
    """Save a report payload before sending it to scheduler."""
    path = pending_report_path(worker_name, payload)
    atomic_write_json(path, payload)
    return path


def delete_pending_report(path):
    """Delete a pending report after scheduler accepted it."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return
    except Exception as exc:
        console_error(None, "pending report delete failed %s: %s" % (path, exc))


def load_pending_report(path):
    """Load one pending report JSON file."""
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def flush_pending_reports(scheduler_url, worker_name, state, args):
    """Send pending reports before pulling new work."""
    root = pending_report_dir(worker_name)
    if not root.is_dir():
        return

    files = sorted(root.glob("*.json"))
    for path in files:
        if is_shutdown_requested(args):
            return
        try:
            payload = load_pending_report(path)
        except Exception as exc:
            bad_path = path.with_suffix(path.suffix + ".bad")
            console_error(worker_name, "PENDING_REPORT_BAD file=%s err=%s" % (path, exc))
            try:
                os.replace(str(path), str(bad_path))
            except Exception:
                pass
            continue

        console_line(worker_name, "PENDING_REPORT_FLUSH ex=%s file=%s" % (
            short_task_id(payload.get("example_id")),
            path,
        ))
        response = report_result_with_retry(
            scheduler_url,
            payload,
            state,
            args,
            worker_name,
        )
        delete_pending_report(path)

        if response.get("discarded"):
            console_line(
                worker_name,
                "PENDING_REPORT_DISCARDED ex=%s reason=%s"
                % (
                    short_task_id(payload.get("example_id")),
                    response.get("error") or "assignment deleted",
                ),
            )
        else:
            console_line(
                worker_name,
                "PENDING_REPORT_DONE ex=%s"
                % short_task_id(payload.get("example_id")),
            )

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


def find_result_env(work_root, target_arg, min_mtime=None):
    """Find vivado_runner result.env for the current single example.

    min_mtime prevents a failed/non-started run from reading stale result.env
    files left by an earlier example in the same slot.
    """
    def is_new_enough(path):
        if min_mtime is None:
            return True
        try:
            return Path(path).stat().st_mtime >= float(min_mtime)
        except Exception:
            return False

    expected = expected_result_env_path(work_root, target_arg)
    if expected and expected.is_file() and is_new_enough(expected):
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
                if str(Path(run_tcl).resolve()) == target_abs and is_new_enough(path):
                    return str(path), data
            except Exception:
                pass

        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0
        if is_new_enough(path) and mtime > newest_mtime:
            newest_mtime = mtime
            newest_path = path

    if newest_path:
        return str(newest_path), parse_env_file(newest_path)

    return None, {}


def classify_result(exit_code, timed_out, result_env, raw_exit_code=None):
    """Convert vivado_runner result.env into scheduler status/message."""
    case_status = str(result_env.get("STATUS") or "").upper()
    reason = result_env.get("REASON") or ""
    ret_code = result_env.get("RET_CODE")
    runtime_sec = result_env.get("RUNTIME_SEC")
    stage = result_env.get("STAGE") or ""
    outer_exit_code = raw_exit_code if raw_exit_code is not None else exit_code
    outer_timed_out = bool(timed_out) or outer_exit_code == 124 or exit_code == 124

    if outer_timed_out:
        status = "timeout"
    elif case_status == "PASS":
        status = "success"
    elif case_status == "TIMEOUT":
        status = "timeout"
    elif case_status in ("FAIL", "FAILED", "ERROR"):
        status = "failed"
    elif exit_code == 0:
        # If run.sh exited normally but no case result was produced, do not
        # silently mark the example as passed.  A single dispatched run.tcl
        # should always create result.env.
        status = "failed"
        if not reason:
            reason = "RESULT_ENV_MISSING"
    else:
        status = "failed"

    report_exit_code = 124 if status == "timeout" else exit_code
    if status != "timeout" and ret_code is not None:
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
    if task.get("_pjtest_run_lock_held"):
        env["RUN_SH_LOCK_HELD"] = "1"
        if task.get("_pjtest_run_lock_file"):
            env["RUN_SH_LOCK_FILE"] = str(task.get("_pjtest_run_lock_file"))
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
    raw_exit_code = None
    timed_out = False
    status = "failed"
    message = ""
    infra_reason = None
    result_env = {}
    result_env_path = None
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
            slot_lock_handle = None

            try:
                # Hold the same slot lock before install/pre-clean.  This
                # prevents a duplicate worker process from cleaning or
                # reinstalling a slot while a real run.sh is still active.
                slot_lock_handle, slot_lock_file = acquire_slot_run_lock(worker_name, log=log)

                slot_task = prepare_task_for_slot(
                    task,
                    worker_name,
                    args.slot_root,
                    log=log,
                )
                slot_task["_pjtest_run_lock_held"] = True
                slot_task["_pjtest_run_lock_file"] = slot_lock_file

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
                pre_clean_rc = run_clean_script(
                    slot_task["work_root"],
                    shell_name,
                    log,
                    reason="before run.sh",
                    worker_name=worker_name,
                )
                log.write("[%s] pre-clean exit_code: %s\n" % (local_now(), pre_clean_rc))
                log.flush()

                if getattr(args, "verbose_console", False):
                    console_line(worker_name, "run command")
                command_start_time = time.time()
                exit_code, timed_out = run_command(slot_task, shell_name, log, worker_name)
                raw_exit_code = exit_code

                # Only read result.env files produced by this command.  Without
                # the mtime guard, a non-started run can pick up a stale
                # result.env from an earlier example in the same slot and hide
                # the true raw exit code.
                result_env_path, result_env = find_result_env(
                    slot_task["work_root"],
                    slot_task.get("target_arg"),
                    min_mtime=command_start_time,
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
                    raw_exit_code=raw_exit_code,
                )
                if status == "timeout":
                    exit_code = 124
                    timed_out = True
                    raw_exit_code = 124

                if raw_exit_code == 75:
                    recent_text = tail_text(log_file, max_lines=120, max_chars=8000)
                    if "Current slot already has a run.sh task running" in recent_text:
                        infra_reason = "slot_busy"
                        status = "failed"
                        exit_code = raw_exit_code
                        message = "infra_slot_busy raw_exit_code=75"

                if getattr(args, "verbose_console", False):
                    console_line(worker_name, "case result status=%s reason=%s" % (
                        result_env.get("STATUS") or status,
                        result_env.get("REASON") or infra_reason or "-",
                    ))

                if infra_reason == "slot_busy":
                    log.write("[%s] clean.sh skipped because slot is busy\n" % local_now())
                    log.flush()
                else:
                    if getattr(args, "verbose_console", False):
                        console_line(worker_name, "clean slot")
                    run_clean_script(slot_task["work_root"], shell_name, log, reason="after run.sh", worker_name=worker_name)

            finally:
                release_slot_run_lock(slot_lock_handle, log=log)

    except SlotBusyError as exc:
        status = "failed"
        exit_code = 75
        raw_exit_code = 75
        timed_out = False
        infra_reason = "slot_busy"
        message = "infra_slot_busy: %s" % exc

        try:
            with open(log_file, "a", encoding="utf-8", errors="ignore") as log:
                log.write("\n[%s] Slot busy: %s\n" % (local_now(), exc))
        except Exception:
            pass

    except Exception as exc:
        status = "failed"
        exit_code = 1
        raw_exit_code = raw_exit_code if raw_exit_code is not None else 1
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
        "raw_exit_code": raw_exit_code,
        "timed_out": timed_out,
        "infra_error": bool(infra_reason),
        "infra_reason": infra_reason,
        "message": message,
        "case_status": result_env.get("STATUS") if result_env else None,
        "case_reason": result_env.get("REASON") if result_env else None,
        "case_runtime_sec": result_env.get("RUNTIME_SEC") if result_env else None,
        "case_ret_code": result_env.get("RET_CODE") if result_env else None,
        "result_env_file": result_env_path,
        "log_file": log_file,
        "log_tail": log_tail,
        "run_log_dir": log_dir,
        "report_dir": None,
    }

    pending_path = save_pending_report(worker_name, report)
    report_response = report_result_with_retry(
        scheduler_url,
        report,
        state,
        args,
        worker_name,
    )
    delete_pending_report(pending_path)

    if report_response.get("discarded"):
        console_error(
            worker_name,
            "RESULT_NOT_RECORDED task=%s ex=%s attempt=%s "
            "because scheduler assignment was deleted"
            % (
                short_task_id(task.get("task_id")),
                short_task_id(task.get("example_id")),
                short_task_id(task.get("attempt_id")),
            ),
        )

    elapsed = time.time() - start_time
    console_line(worker_name, "DONE status=%s rc=%s raw_rc=%s timeout=%s infra=%s sec=%.1f %s log=%s" % (
        status,
        exit_code,
        raw_exit_code if raw_exit_code is not None else "-",
        timed_out,
        infra_reason or "-",
        elapsed,
        short_target(message, 70),
        log_file,
    ))

    state.set("idle", None, None, None, "last result: %s" % status)
    return status, infra_reason

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

    try:
        acquire_worker_process_lock(worker_name)
    except WorkerSlotLockError as exc:
        console_error(worker_name, "worker slot already active; refusing duplicate process: %s" % exc)
        return 75

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
    flush_pending_reports(args.scheduler, worker_name, state, args)

    try:
        while not is_shutdown_requested(args):
            state.set("idle", None, None, None, "requesting task")
            flush_pending_reports(args.scheduler, worker_name, state, args)

            if slot_is_busy_before_pull(worker_name):
                state.set(
                    "idle",
                    None,
                    None,
                    None,
                    "slot busy before pull; wait",
                )
                console_error(
                    worker_name,
                    "SLOT_BUSY_BEFORE_PULL skip pulling task; backoff=%ss"
                    % SLOT_BUSY_BACKOFF_SEC,
                )

                if args.once:
                    return 75

                if wait_or_shutdown(args, max(args.interval, SLOT_BUSY_BACKOFF_SEC)):
                    break

                continue

            try:
                response = pull_task(
                    args.scheduler,
                    worker_name,
                    capabilities,
                    dump_json=args.dump_json,
                    timeout=args.pull_timeout,
                )
            except Exception as exc:
                console_error(worker_name, "PULL_FAIL response may be lost; retry idempotently: %s" % exc)
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
                result_status, result_infra_reason = run_one_task(
                    args.scheduler,
                    worker_name,
                    task,
                    args.shell,
                    args.install_root,
                    state,
                    args,
                )

                if result_infra_reason == "slot_busy":
                    console_error(
                        worker_name,
                        "SLOT_BUSY_AFTER_REPORT backoff=%ss"
                        % SLOT_BUSY_BACKOFF_SEC,
                    )

                    if args.once:
                        return 75

                    if wait_or_shutdown(args, max(args.interval, SLOT_BUSY_BACKOFF_SEC)):
                        break

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
        release_worker_process_lock(worker_name)



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
    script_path = Path(__file__).expanduser().resolve().parents[1] / "worker.py"
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
        "--pull-timeout",
        str(args.pull_timeout),
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
            with result_lock:
                finished_slots = sorted(results)
            if finished_slots:
                console_error(
                    None,
                    "manager detected stopped slot threads: %s; shutting down remaining slots"
                    % ",".join(finished_slots),
                )
                args.shutdown_event.set()
                break
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
        with result_lock:
            final_results = dict(results)
        if len(final_results) != jobs or any(value != 0 for value in final_results.values()):
            console_error(None, "manager stopped because one or more slots failed: %s" % final_results)
            return 1
        return 0

    except KeyboardInterrupt:
        args.shutdown_event.set()
        console_line(None, "Interrupted; shutdown requested")
        return 130


class CheckReporter(object):
    """Collect and print worker configuration check results."""

    def __init__(self):
        self.counts = {
            "OK": 0,
            "WARN": 0,
            "FAIL": 0,
            "INFO": 0,
        }

    def add(self, status, item, detail):
        """Print one aligned check result and update counters."""
        status = str(status).upper()
        if status not in self.counts:
            status = "INFO"
        self.counts[status] += 1
        console_line(None, "%-4s %-24s %s" % (status, item, detail))

    def ok(self, item, detail):
        self.add("OK", item, detail)

    def warn(self, item, detail):
        self.add("WARN", item, detail)

    def fail(self, item, detail):
        self.add("FAIL", item, detail)

    def info(self, item, detail):
        self.add("INFO", item, detail)

    def finish(self):
        """Print summary and return a shell-friendly exit code."""
        console_line(
            None,
            "CHECK_SUMMARY ok=%d warn=%d fail=%d info=%d" % (
                self.counts["OK"],
                self.counts["WARN"],
                self.counts["FAIL"],
                self.counts["INFO"],
            ),
        )
        return 1 if self.counts["FAIL"] else 0


def nearest_existing_parent(path):
    """Return the nearest existing path at or above the requested path."""
    current = Path(path).expanduser()
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def check_directory_access(reporter, item, path, required=False):
    """Check whether a directory exists or can be created by the worker."""
    path = Path(path).expanduser()
    if path.is_dir():
        readable = os.access(str(path), os.R_OK | os.X_OK)
        writable = os.access(str(path), os.W_OK | os.X_OK)
        if readable and writable:
            reporter.ok(item, "%s (read/write)" % path)
        elif readable:
            if required:
                reporter.fail(item, "%s (read-only)" % path)
            else:
                reporter.warn(item, "%s (read-only)" % path)
        else:
            reporter.fail(item, "%s (not readable)" % path)
        return

    if path.exists():
        reporter.fail(item, "%s exists but is not a directory" % path)
        return

    parent = nearest_existing_parent(path.parent)
    can_create = parent.is_dir() and os.access(str(parent), os.W_OK | os.X_OK)
    if can_create:
        reporter.warn(item, "%s missing; parent is writable: %s" % (path, parent))
    else:
        reporter.fail(item, "%s missing and cannot be created from %s" % (path, parent))


def check_file_access(reporter, item, path, executable=False, required=True):
    """Check one regular file and optional executable permission."""
    path = Path(path).expanduser()
    if not path.is_file():
        if required:
            reporter.fail(item, "missing: %s" % path)
        else:
            reporter.warn(item, "missing: %s" % path)
        return False

    if not os.access(str(path), os.R_OK):
        reporter.fail(item, "not readable: %s" % path)
        return False

    if executable and not os.access(str(path), os.X_OK):
        reporter.fail(item, "not executable: %s" % path)
        return False

    suffix = "readable/executable" if executable else "readable"
    reporter.ok(item, "%s (%s)" % (path, suffix))
    return True


def find_command(candidates):
    """Return the first available command from a candidate list."""
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def check_command_available(reporter, item, candidates, required=True):
    """Check whether at least one command candidate is available."""
    found = find_command(candidates)
    if found:
        reporter.ok(item, found)
        return found

    message = "not found in PATH: %s" % ", ".join(candidates)
    if required:
        reporter.fail(item, message)
    else:
        reporter.warn(item, message)
    return None


def parse_flow_config_summary(path):
    """Return a lightweight summary of a worker slot flow_config file."""
    path = Path(path)
    keys = []
    set_re = re.compile(r"^\s*set\s+([A-Za-z_][A-Za-z0-9_]*)\s+")
    kv_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)(?:\s*=\s*|\s+)")

    with path.open("r", encoding="utf-8", errors="ignore") as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            match = set_re.match(line)
            if not match:
                match = kv_re.match(line)
            if match:
                keys.append(match.group(1))

    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    return len(keys), duplicates


def check_slot_configuration(reporter, args, base_worker_name, slot_index):
    """Check one configured worker slot without creating or changing it."""
    slot_worker_name = make_slot_worker_name(base_worker_name, slot_index)
    root = slot_galaxcore_root(args.slot_root, slot_worker_name)
    status = slot_checkout_status(root)
    item_prefix = "slot%d" % slot_index

    if not status["root_exists"]:
        reporter.warn(item_prefix, "not initialized; startup will checkout %s" % root)
        return

    missing = []
    if not status["svn_dir"]:
        missing.append(".svn")
    if not status["test2_dir"]:
        missing.append("test2")
    if not status["run_sh"]:
        missing.append("test2/run.sh")

    if missing:
        reporter.fail(item_prefix, "incomplete checkout %s; missing=%s" % (
            root,
            ",".join(missing),
        ))
        return

    reporter.ok(item_prefix, "checkout ready: %s" % root)

    test2 = root / "test2"
    check_file_access(
        reporter,
        "%s run.sh" % item_prefix,
        test2 / "run.sh",
        executable=True,
        required=True,
    )

    flow_config = test2 / "flow_config"
    if check_file_access(
        reporter,
        "%s flow_config" % item_prefix,
        flow_config,
        executable=False,
        required=False,
    ):
        try:
            key_count, duplicates = parse_flow_config_summary(flow_config)
            detail = "recognized_keys=%d" % key_count
            if duplicates:
                reporter.warn(
                    "%s flow keys" % item_prefix,
                    "%s duplicate_keys=%s" % (detail, ",".join(duplicates)),
                )
            else:
                reporter.info("%s flow keys" % item_prefix, detail)
        except Exception as exc:
            reporter.warn("%s flow parse" % item_prefix, str(exc))

    if CLEAN_AFTER_RUN:
        check_file_access(
            reporter,
            "%s clean.sh" % item_prefix,
            test2 / "clean.sh",
            executable=True,
            required=False,
        )
    else:
        reporter.info("%s clean.sh" % item_prefix, "disabled by PJTEST_CLEAN_AFTER_RUN=0")

    effective_install_root = (
        Path(args.install_root).expanduser().resolve()
        if args.install_root
        else root
    )
    target_binary, flow_target = resolve_install_targets(effective_install_root)
    target_parent = target_binary.parent
    if target_parent.is_dir() and os.access(str(target_parent), os.W_OK | os.X_OK):
        reporter.ok("%s binary dst" % item_prefix, str(target_binary))
    elif target_parent.is_dir():
        reporter.fail("%s binary dst" % item_prefix, "parent not writable: %s" % target_parent)
    else:
        parent = nearest_existing_parent(target_parent)
        if parent.is_dir() and os.access(str(parent), os.W_OK | os.X_OK):
            reporter.warn(
                "%s binary dst" % item_prefix,
                "parent missing but can be created: %s" % target_parent,
            )
        else:
            reporter.fail(
                "%s binary dst" % item_prefix,
                "cannot create under: %s" % parent,
            )

    if flow_target.exists():
        reporter.info("%s flow dst" % item_prefix, str(flow_target))
    else:
        reporter.warn("%s flow dst" % item_prefix, "missing: %s" % flow_target)


def zip_revision(path):
    """Extract a numeric revision from a GalaxCore zip filename."""
    match = re.search(r"(?:_|-|_r)(\d+)\.zip$", path.name, re.IGNORECASE)
    return int(match.group(1)) if match else -1


def check_shared_zip_directory(reporter):
    """Check shared zip directory and inspect the newest GalaxCore package."""
    if not SHARE_ZIP_DIR.is_dir():
        reporter.fail("shared zip dir", "missing: %s" % SHARE_ZIP_DIR)
        return

    if not os.access(str(SHARE_ZIP_DIR), os.R_OK | os.X_OK):
        reporter.fail("shared zip dir", "not readable: %s" % SHARE_ZIP_DIR)
        return

    zip_paths = sorted(
        SHARE_ZIP_DIR.glob("*.zip"),
        key=lambda path: (zip_revision(path), path.stat().st_mtime),
    )
    if not zip_paths:
        reporter.warn("shared zip dir", "%s contains no zip files" % SHARE_ZIP_DIR)
        return

    latest = zip_paths[-1]
    reporter.ok(
        "shared zip dir",
        "%s zip_count=%d latest=%s" % (SHARE_ZIP_DIR, len(zip_paths), latest.name),
    )

    if not zipfile.is_zipfile(str(latest)):
        reporter.fail("latest zip", "invalid zip: %s" % latest)
        return

    try:
        with zipfile.ZipFile(str(latest), "r") as archive:
            bad_member = archive.testzip()
            names = archive.namelist()
    except Exception as exc:
        reporter.fail("latest zip", "%s: %s" % (latest, exc))
        return

    if bad_member:
        reporter.fail("latest zip", "CRC failure member=%s file=%s" % (bad_member, latest))
        return

    base_names = {Path(name.rstrip("/")).name for name in names if name.rstrip("/")}
    has_binary = "GalaxCore" in base_names or "Galaxcore" in base_names
    has_flow = any(
        part == "flow"
        for name in names
        for part in Path(name).parts
    )

    if has_binary:
        reporter.ok("latest zip", "%s contains GalaxCore" % latest)
    else:
        reporter.fail("latest zip", "%s does not contain GalaxCore" % latest)

    if has_flow:
        reporter.info("latest zip flow", "flow directory is included")
    else:
        reporter.info("latest zip flow", "flow is not included; existing slot flow will be kept")


def check_scheduler_endpoint(reporter, scheduler_url):
    """Validate scheduler URL and test TCP reachability without registering."""
    try:
        from urllib.parse import urlparse
    except ImportError:  # pragma: no cover
        from urlparse import urlparse

    parsed = urlparse(str(scheduler_url))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        reporter.fail("scheduler URL", "invalid: %s" % scheduler_url)
        return

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    reporter.ok(
        "scheduler URL",
        "%s host=%s port=%d" % (scheduler_url, parsed.hostname, port),
    )

    try:
        connection = socket.create_connection((parsed.hostname, port), timeout=3)
        connection.close()
        reporter.ok("scheduler TCP", "%s:%d reachable" % (parsed.hostname, port))
    except Exception as exc:
        reporter.warn("scheduler TCP", "%s:%d unreachable: %s" % (
            parsed.hostname,
            port,
            exc,
        ))


def check_svn_endpoint(reporter, svn_command, svn_url):
    """Validate SVN URL with a read-only svn info request."""
    if not svn_command:
        return

    if not str(svn_url).strip():
        reporter.fail("SVN URL", "empty")
        return

    try:
        proc = subprocess.run(
            [svn_command, "info", str(svn_url)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        reporter.warn("SVN URL", "svn info timed out after 10s: %s" % svn_url)
        return
    except Exception as exc:
        reporter.warn("SVN URL", "%s: %s" % (svn_url, exc))
        return

    if proc.returncode == 0:
        revision = "unknown"
        for line in (proc.stdout or "").splitlines():
            if line.lower().startswith("revision:"):
                revision = line.split(":", 1)[1].strip()
                break
        reporter.ok("SVN URL", "%s revision=%s" % (svn_url, revision))
    else:
        detail = " ".join((proc.stdout or "").strip().split())
        reporter.warn("SVN URL", "svn info rc=%d %s" % (
            proc.returncode,
            short_target(detail or svn_url, 140),
        ))


def run_configuration_check(args):
    """Check effective worker configuration without starting worker slots."""
    reporter = CheckReporter()
    base_worker_name = args.worker_name or DEFAULT_WORKER_NAME or get_default_worker_name()

    console_line(None, "PJTest worker configuration check")
    reporter.info("worker name", base_worker_name)
    reporter.info("hostname", socket.gethostname())
    reporter.info("scheduler", str(args.scheduler))
    reporter.info("jobs", str(args.jobs))
    reporter.info("single slot", str(args.single_slot or "disabled"))
    reporter.info("shell", str(args.shell))
    reporter.info("install root", str(args.install_root or "slot-local"))
    reporter.info("slot root", str(Path(args.slot_root).expanduser()))
    reporter.info("SVN URL", str(args.svn_url))
    reporter.info("shared zip", str(SHARE_ZIP_DIR))
    reporter.info("log root", str(LOG_ROOT))
    reporter.info("pending reports", str(PENDING_REPORT_ROOT))
    reporter.info("protobuf lib", str(PROTOBUF_LIB_DIR or "disabled"))
    reporter.info("binary relative", str(BIN_RELATIVE_DIR))
    reporter.info("binary override", str(TARGET_BINARY_ENV or "disabled"))
    reporter.info("flow override", str(FLOW_TARGET_DIR_ENV or "disabled"))
    reporter.info(
        "flow ignored keys",
        ",".join(sorted(FLOW_CONFIG_IGNORE_KEYS)) or "none",
    )
    reporter.info("clean after run", str(int(CLEAN_AFTER_RUN)))
    reporter.info("clean timeout", str(CLEAN_TIMEOUT))
    reporter.info("update test2", str(int(bool(args.update_test2))))
    reporter.info("recheckout slots", str(int(bool(args.recheckout_slots))))
    reporter.info("tmux slots", str(int(bool(args.tmux_slots))))
    reporter.info("once mode", str(int(bool(args.once))))
    reporter.info("dump JSON", str(int(bool(args.dump_json))))
    reporter.info("SVN timeout", str(SVN_COMMAND_TIMEOUT))
    reporter.info("report retry", "%ss max=%s" % (
        args.report_retry_interval,
        args.report_max_retries,
    ))

    if int(args.jobs) < 1:
        reporter.fail("jobs value", "--jobs must be >= 1")
    else:
        reporter.ok("jobs value", str(args.jobs))

    if int(args.interval) < 0:
        reporter.fail("pull interval", "must be >= 0")
    else:
        reporter.ok("pull interval", "%ss" % args.interval)

    if int(args.heartbeat_interval) <= 0:
        reporter.fail("heartbeat interval", "must be > 0")
    else:
        reporter.ok("heartbeat interval", "%ss" % args.heartbeat_interval)

    if int(args.report_retry_interval) <= 0:
        reporter.fail("report retry", "interval must be > 0")
    if int(args.report_max_retries) < 0:
        reporter.fail("report retry", "max retries must be >= 0")
    if int(args.shutdown_timeout) < 0:
        reporter.fail("shutdown timeout", "must be >= 0")

    check_scheduler_endpoint(reporter, args.scheduler)

    svn_command = check_command_available(reporter, "command svn", ["svn"], required=True)
    if args.shell == "csh":
        check_command_available(reporter, "command csh", ["tcsh", "csh"], required=True)
    else:
        check_command_available(reporter, "command bash", ["bash"], required=True)

    check_command_available(
        reporter,
        "command tmux",
        ["tmux"],
        required=bool(args.tmux_slots),
    )
    check_svn_endpoint(reporter, svn_command, args.svn_url)

    check_directory_access(reporter, "slot root", args.slot_root, required=True)
    check_directory_access(reporter, "log root", LOG_ROOT, required=False)
    check_directory_access(reporter, "pending root", PENDING_REPORT_ROOT, required=False)
    check_directory_access(reporter, "temporary root", tempfile.gettempdir(), required=True)

    if PROTOBUF_LIB_DIR:
        protobuf_path = Path(PROTOBUF_LIB_DIR).expanduser()
        if protobuf_path.is_dir():
            libraries = sorted(protobuf_path.glob("libprotobuf*.so*"))
            if libraries:
                reporter.ok(
                    "protobuf lib",
                    "%s files=%d" % (protobuf_path, len(libraries)),
                )
            else:
                reporter.warn(
                    "protobuf lib",
                    "%s exists but no libprotobuf*.so* found" % protobuf_path,
                )
        else:
            reporter.fail("protobuf lib", "missing: %s" % protobuf_path)

    check_shared_zip_directory(reporter)

    if args.install_root:
        install_root = Path(args.install_root).expanduser()
        check_directory_access(
            reporter,
            "install root",
            install_root,
            required=True,
        )
        if int(args.jobs) > 1 and args.single_slot is None:
            reporter.warn(
                "install root sharing",
                "all %d slots will install into the same path: %s" % (
                    int(args.jobs),
                    install_root,
                ),
            )

    if args.single_slot is not None:
        if int(args.single_slot) < 1:
            reporter.fail("single slot", "--single-slot must be >= 1")
            slot_indices = []
        else:
            slot_indices = [int(args.single_slot)]
    elif int(args.jobs) > 0:
        slot_indices = list(range(1, int(args.jobs) + 1))
    else:
        slot_indices = []

    for slot_index in slot_indices:
        check_slot_configuration(
            reporter,
            args,
            base_worker_name,
            slot_index,
        )

    return reporter.finish()

def build_parser():
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description="PJTest HTTP worker")
    parser.add_argument(
        "--check",
        action="store_true",
        help="check effective configuration, paths, commands, scheduler/SVN reachability, slots, and latest zip without starting workers",
    )
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
        help="root directory for isolated worker slots, default: <worker>/worker_slots",
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
        "--pull-timeout",
        type=int,
        default=PULL_TIMEOUT,
        help="task pull HTTP timeout seconds, default: env PJTEST_PULL_TIMEOUT or 60",
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
        if args.check:
            return run_configuration_check(args)
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
