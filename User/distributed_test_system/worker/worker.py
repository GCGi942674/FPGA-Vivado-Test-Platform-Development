#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
worker.py

Distributed GalaxCore worker.

Workflow:
1. Pull a task from the pudong scheduler.
2. Analyze the task:
   2.1 If the task specifies a revision, use the designated GalaxCore_xxx.zip;
       otherwise use the latest GalaxCore_xxx.zip from the shared directory.
       The source workspace is updated to the latest SVN revision only.
       The selected GalaxCore zip revision is passed to vivado_runner through
       environment variables and .galaxcore_build_info.
   2.2 Read the flow_config field from the task JSON and bulk-update the
       corresponding fields in the local test2/flow_config.
3. Execute the original task cmd, e.g. cd ~/workspace/galaxcore/test2 && ./run.sh .
4. Report success / failure back to the scheduler.

Notes:
- enable_copy is just an ordinary field in flow_config, consumed by vivado_runner.
- revision controls the selected GalaxCore binary zip version, not the source workspace revision.
- flow_config controls which stages vivado_runner executes.
- If the scheduler is unavailable, the worker waits and retries without flooding traceback logs.
"""

import fcntl
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple, Any


# ============================================================
# User configuration
# ============================================================

PROJECT_NAME = "galaxcore"

# Scheduler configuration
SCHEDULER_URL = os.environ.get("SCHEDULER_URL", "http://192.168.10.11:9000").rstrip("/")
PULL_API = os.environ.get("PULL_API", "/api/task/pull")
REPORT_API = os.environ.get("REPORT_API", "/api/task/report")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
SCHEDULER_HEALTH_API = os.environ.get("SCHEDULER_HEALTH_API", "/api/health")
SCHEDULER_CHECK_INTERVAL = int(os.environ.get("SCHEDULER_CHECK_INTERVAL", "10"))
SCHEDULER_WARN_INTERVAL = int(os.environ.get("SCHEDULER_WARN_INTERVAL", "60"))

# Worker name – recommended to set WORKER_NAME explicitly before starting the worker on each machine
WORKER_NAME = os.environ.get("WORKER_NAME", socket.gethostname().split(".")[0])

# Local GalaxCore workspace path
WORKSPACE_DIR = os.path.expanduser(os.environ.get("WORKSPACE_DIR", "~/workspace/galaxcore"))
TEST_DIR = os.environ.get("TEST_DIR", os.path.join(WORKSPACE_DIR, "test2"))
FLOW_CONFIG_FILE = os.environ.get("FLOW_CONFIG_FILE", os.path.join(TEST_DIR, "flow_config"))

# GalaxCore zip shared directory
SHARE_ZIP_DIR = os.environ.get("SHARE_ZIP_DIR", "/home/xiaonan/Share/zw_cache/distributed_test_system/GalaxCore_bin/zip")

# Local GalaxCore binary location
BIN_DIR = os.environ.get("BIN_DIR", os.path.join(WORKSPACE_DIR, "bin/Linux_64"))
TARGET_BINARY = os.environ.get("TARGET_BINARY", os.path.join(BIN_DIR, "GalaxCore"))

# Runtime library required by GalaxCore (same as night_build_galaxcore.sh / worker_run_test.py)
PROTOBUF_LIB_DIR = os.environ.get("PROTOBUF_LIB_DIR", "/home/fpga/lib/protobuf-3.9.0/lib")

# Default test target when cmd is not provided
TEST_RUN_DIR = os.environ.get("GALAXCORE_TEST_TARGET", ".")
DEFAULT_CMD = os.environ.get("DEFAULT_CMD", "cd \"{}\" && ./run.sh \"{}\"".format(TEST_DIR, TEST_RUN_DIR))

# Whether to ignore ./run.sh return code.
# By default (matching the old worker_run_test.py behaviour) a non‑zero
# test return code does **not** fail the whole worker flow.
# If you want a non‑zero rc to cause a failed task: export GALAXCORE_IGNORE_RUN_RC=0
IGNORE_RUN_SH_RC = os.environ.get("GALAXCORE_IGNORE_RUN_RC", "1") != "0"

# Maximum retries for a single task.
# The worker is a long‑running polling process; retrying many times is usually not desired.
MAX_ATTEMPTS = int(os.environ.get("GALAXCORE_WORKER_MAX_ATTEMPTS", "1"))
RETRY_INTERVAL = int(os.environ.get("GALAXCORE_WORKER_RETRY_INTERVAL", "1800"))

# Log location: /home/user3/distributed_test_system/logs/YYYY-MM-DD/
LOG_ROOT = os.path.expanduser(
    os.environ.get("LOG_ROOT", "~/distributed_test_system/logs")
)
DATE_TAG = datetime.now().strftime("%Y-%m-%d")
LOG_DIR = os.path.join(LOG_ROOT, DATE_TAG)
SUMMARY_FILE = os.path.join(LOG_ROOT, "worker_summary.tsv")

# Worker lock to prevent multiple workers from replacing GalaxCore concurrently on the same host
LOCK_FILE = os.environ.get("LOCK_FILE", "/tmp/galaxcore_worker.lock")

# Current-user run.sh guard.
# The worker only checks run.sh owned by the same Linux user, so it will not block
# other users on the same machine.
RUN_SH_BUSY_CHECK_ENABLED = os.environ.get("RUN_SH_BUSY_CHECK_ENABLED", "1") != "0"
RUN_SH_BUSY_SLEEP = int(os.environ.get("RUN_SH_BUSY_SLEEP", "60"))
RUN_SH_BUSY_LOG_INTERVAL = int(os.environ.get("RUN_SH_BUSY_LOG_INTERVAL", "60"))
RUN_SH_PATTERN = re.compile(r"(^|[\s/])run\.sh(\s|$)")

# Worker busy return code. The scheduler will requeue the task instead of marking it failed.
WORKER_BUSY_RC = int(os.environ.get("WORKER_BUSY_RC", "75"))

# GalaxCore binary guard. This prevents the worker from pulling a task while
# the local GalaxCore binary is still used by an existing manual or worker run.
GALAXCORE_BUSY_CHECK_ENABLED = os.environ.get("GALAXCORE_BUSY_CHECK_ENABLED", "1") != "0"
GALAXCORE_BUSY_SLEEP = int(os.environ.get("GALAXCORE_BUSY_SLEEP", str(RUN_SH_BUSY_SLEEP)))
GALAXCORE_BUSY_LOG_INTERVAL = int(os.environ.get("GALAXCORE_BUSY_LOG_INTERVAL", str(RUN_SH_BUSY_LOG_INTERVAL)))

# Temporary directory for zip extraction
TMP_DIR = os.environ.get("TMP_DIR", "/tmp/galaxcore_worker")
CLEAN_TMP_DIR = os.environ.get("CLEAN_TMP_DIR", "1") != "0"

# By default we keep test output for investigation
POST_RUN_CLEANUP = os.environ.get("POST_RUN_CLEANUP", "0") == "1"

# Format used when appending a new field to flow_config: "space" -> key value; "equal" -> key = value
FLOW_CONFIG_APPEND_STYLE = os.environ.get("FLOW_CONFIG_APPEND_STYLE", "space").strip().lower()

START_TS = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
START_EPOCH = int(time.time())
RUN_TAG = datetime.now().strftime("%H%M%S")
LOG_FILE = os.path.join(LOG_DIR, "worker_{}_{}.log".format(WORKER_NAME, RUN_TAG))
STEP_SUMMARY_FILE = os.path.join(LOG_DIR, "worker_steps_{}_{}.tsv".format(WORKER_NAME, RUN_TAG))

CURRENT_TASK_ID = ""
CURRENT_ATTEMPT = 0
CURRENT_STEP = ""
FAILED_STEP = ""
FAILED_REASON = ""


# ============================================================
# Logging helpers
# ============================================================

def ensure_log_dir() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(LOG_ROOT, exist_ok=True)


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    ensure_log_dir()
    line = "[{}] {}".format(now_ts(), msg)
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def append_step_summary(step_name: str, start_ts: str, end_ts: str, duration: int, rc: int) -> None:
    ensure_log_dir()
    if not os.path.exists(STEP_SUMMARY_FILE):
        with open(STEP_SUMMARY_FILE, "w", encoding="utf-8") as f:
            f.write("task_id\tstep_name\tstart_time\tend_time\tduration_sec\texit_code\tstatus\n")

    status = "SUCCESS" if rc == 0 else "FAILED"
    with open(STEP_SUMMARY_FILE, "a", encoding="utf-8") as f:
        f.write("{}\t{}\t{}\t{}\t{}\t{}\t{}\n".format(
            CURRENT_TASK_ID, step_name, start_ts, end_ts, duration, rc, status
        ))


def append_daily_summary(task_id: Any, revision: str, status: str) -> None:
    ensure_log_dir()
    end_ts = now_ts()
    duration = int(time.time()) - START_EPOCH

    if not os.path.exists(SUMMARY_FILE):
        with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
            f.write("date\ttask_id\trevision\tstart_time\tend_time\tduration_sec\tstatus\tattempt\tfailed_step\tlog_file\thost\n")

    with open(SUMMARY_FILE, "a", encoding="utf-8") as f:
        f.write(
            "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n".format(
                DATE_TAG, task_id, revision, START_TS, end_ts, duration, status,
                CURRENT_ATTEMPT, FAILED_STEP, LOG_FILE, socket.gethostname()
            )
        )


def fail(step_name: str, reason: str) -> int:
    global FAILED_STEP, FAILED_REASON
    FAILED_STEP = step_name
    FAILED_REASON = reason
    log("[ERROR] [{}] {}".format(step_name, reason))
    return 1


def get_log_tail(max_lines: int = 200) -> str:
    if not os.path.exists(LOG_FILE):
        return ""

    lines = deque(maxlen=max_lines)
    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            lines.append(line.rstrip("\n"))
    return "\n".join(lines)


# ============================================================
# Shell helpers
# ============================================================

def shell_quote(s: str) -> str:
    import shlex
    return shlex.quote(str(s))


def find_csh() -> Optional[str]:
    return shutil.which("tcsh") or shutil.which("csh")


def csh_runtime_prefix() -> str:
    """
    Emulate a manual terminal session:
    1. source ~/.cshrc
    2. set LD_LIBRARY_PATH so that even if it is undefined inside csh, we don't get an error
    """
    return """
if ( -f ~/.cshrc ) source ~/.cshrc
if ( $?LD_LIBRARY_PATH ) then
    setenv LD_LIBRARY_PATH {0}:$LD_LIBRARY_PATH
else
    setenv LD_LIBRARY_PATH {0}
endif
""".format(PROTOBUF_LIB_DIR)


def run_process(args, step_log_prefix: Optional[str] = None) -> int:
    display = " ".join(args) if isinstance(args, (list, tuple)) else str(args)
    log("[CMD] {}".format(display))

    try:
        p = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        log("[ERROR] command not found: {}".format(exc))
        return 127

    assert p.stdout is not None
    for line in p.stdout:
        line = line.rstrip("\n")
        if step_log_prefix:
            log("{}{}".format(step_log_prefix, line))
        else:
            log(line)

    rc = p.wait()
    log("[CMD_EXIT] rc={}".format(rc))
    return rc


def run_bash(script: str) -> int:
    return run_process(["/bin/bash", "-lc", script])


def run_csh(script: str) -> int:
    csh_bin = find_csh()
    if not csh_bin:
        log("[ERROR] neither tcsh nor csh found in PATH")
        return 127

    # -f : do not automatically load cshrc; we source ~/.cshrc explicitly in the script
    return run_process([csh_bin, "-f", "-c", script])


def run_step(step_name: str, func: Callable[[], int]) -> int:
    global CURRENT_STEP
    CURRENT_STEP = step_name

    step_start_epoch = int(time.time())
    step_start_ts = now_ts()

    log("============================================================")
    log("[STEP START] {}".format(step_name))
    log("============================================================")

    rc = func()

    step_end_ts = now_ts()
    duration = int(time.time()) - step_start_epoch
    append_step_summary(step_name, step_start_ts, step_end_ts, duration, rc)

    if rc == WORKER_BUSY_RC:
        log("[STEP BUSY ] {} (cost {}s)".format(step_name, duration))
        CURRENT_STEP = ""
        return rc

    if rc != 0:
        log("[STEP FAIL ] {} (cost {}s)".format(step_name, duration))
        CURRENT_STEP = ""
        fail(step_name, "exit code {}".format(rc))
        return rc

    log("[STEP DONE ] {} (cost {}s)".format(step_name, duration))
    CURRENT_STEP = ""
    return 0


# ============================================================
# HTTP helpers
# ============================================================

class SchedulerConnectionError(RuntimeError):
    """Raised when the scheduler cannot be reached."""


def build_scheduler_url(api_path: str) -> str:
    """Build a scheduler API URL from a relative API path."""
    if api_path.startswith("http://") or api_path.startswith("https://"):
        return api_path
    return SCHEDULER_URL + api_path


def get_json(api_path: str, timeout: int = 10) -> Dict[str, Any]:
    """Send a GET request to the scheduler and parse the JSON response."""
    url = build_scheduler_url(api_path)
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
            if not body:
                return {}
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("HTTP {} from {}: {}".format(exc.code, url, body))
    except urllib.error.URLError as exc:
        raise SchedulerConnectionError("cannot connect to scheduler {}: {}".format(url, exc))


def check_scheduler_health() -> Tuple[bool, str]:
    """Check whether the scheduler health API is reachable."""
    try:
        resp = get_json(SCHEDULER_HEALTH_API, timeout=5)
    except Exception as exc:
        return False, str(exc)

    if resp.get("ok"):
        return True, "ok"

    return False, "unexpected health response: {}".format(resp)


def wait_for_scheduler() -> None:
    """Wait until the scheduler is reachable before polling tasks."""
    last_log_time = 0

    while True:
        ok, message = check_scheduler_health()
        if ok:
            log("[INFO] Scheduler is available: {}".format(SCHEDULER_URL))
            return

        now = time.time()
        if now - last_log_time >= SCHEDULER_WARN_INTERVAL:
            log("[WARN] Scheduler is not available yet: {}".format(message))
            log("[INFO] Waiting for scheduler, retry every {}s".format(SCHEDULER_CHECK_INTERVAL))
            last_log_time = now

        time.sleep(SCHEDULER_CHECK_INTERVAL)


def post_json(api_path: str, payload: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
    url = build_scheduler_url(api_path)
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
            if not body:
                return {}
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("HTTP {} from {}: {}".format(exc.code, url, body))
    except urllib.error.URLError as exc:
        raise SchedulerConnectionError("cannot connect to scheduler {}: {}".format(url, exc))


def pull_task() -> Optional[Dict[str, Any]]:
    payload = {
        "worker": WORKER_NAME,
        "hostname": socket.gethostname(),
    }

    resp = post_json(PULL_API, payload, timeout=30)
    if not resp.get("ok", False):
        raise RuntimeError("pull task failed: {}".format(resp.get("error", "unknown error")))

    task = resp.get("task")
    if not task:
        return None
    if not isinstance(task, dict):
        raise RuntimeError("scheduler returned invalid task: {}".format(task))
    return task


def report_task(task: Dict[str, Any], status: str, exit_code: Optional[int] = None, message: str = "") -> None:
    task_id = get_task_id(task)

    payload = {
        "id": task_id,
        "task_id": task_id,
        "worker": WORKER_NAME,
        "hostname": socket.gethostname(),
        "status": status,
        "exit_code": exit_code,
        "message": message,
        "failed_step": FAILED_STEP,
        "failed_reason": FAILED_REASON,
        "log_file": LOG_FILE,
        "log_tail": get_log_tail(),
        "updated_at": now_ts(),
    }

    try:
        resp = post_json(REPORT_API, payload, timeout=20)
        if not resp.get("ok", True):
            log("[WARN] report task failed: {}".format(resp))
    except Exception as exc:
        log("[WARN] report task exception: {}".format(exc))


def requeue_task(task: Dict[str, Any], message: str) -> None:
    """Return a pulled task to pending because this worker is locally busy."""
    log("[INFO] Requeue task without failure: {}".format(message))
    report_task(task, "requeue", exit_code=WORKER_BUSY_RC, message=message)


# ============================================================
# Task parsing
# ============================================================

def get_task_id(task: Dict[str, Any]) -> Any:
    return task.get("id") or task.get("task_id")


def get_task_cmd(task: Dict[str, Any]) -> str:
    return (
        task.get("cmd")
        or task.get("command")
        or task.get("run_cmd")
        or task.get("test_cmd")
        or DEFAULT_CMD
    )


def normalize_revision_value(rev: Any) -> str:
    if rev is None:
        return ""
    rev = str(rev).strip()
    if rev.lower() in ("", "none", "null", "latest"):
        return ""
    if rev.startswith("r") or rev.startswith("R"):
        rev = rev[1:]
    return rev


def get_task_revision(task: Dict[str, Any]) -> str:
    return normalize_revision_value(
        task.get("revision")
        or task.get("rev")
        or task.get("svn_revision")
        or task.get("build_revision")
    )


def parse_flow_config_string(text: str) -> Dict[str, Any]:
    text = text.strip()
    if not text:
        return {}

    # Try to parse as JSON first
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # Fallback to key=value or key value text
    result = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        # Remove inline comments
        if "#" in line:
            line = line.split("#", 1)[0].strip()

        if not line:
            continue

        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
        else:
            parts = line.split(None, 1)
            if len(parts) == 2:
                result[parts[0].strip()] = parts[1].strip()

    return result


def get_task_flow_config(task: Dict[str, Any]) -> Dict[str, Any]:
    flow_config = task.get("flow_config", {})

    if flow_config is None:
        return {}

    if isinstance(flow_config, str):
        flow_config = parse_flow_config_string(flow_config)

    if not isinstance(flow_config, dict):
        raise RuntimeError("flow_config must be dict or json/key-value string, got {}".format(type(flow_config)))

    return flow_config


def normalize_flow_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ============================================================
# GalaxCore revision / zip handling
# ============================================================

def extract_revision(filename: str) -> int:
    patterns = [
        r"^GalaxCore_(\d+)\.zip$",
        r"^GalaxCore_r(\d+)\.zip$",
        r"^GalaxCore-(\d+)\.zip$",
    ]
    for pat in patterns:
        m = re.search(pat, filename)
        if m:
            return int(m.group(1))
    return -1


def find_latest_zip() -> Tuple[Optional[int], Optional[str]]:
    if not os.path.isdir(SHARE_ZIP_DIR):
        log("[ERROR] share zip directory not found: {}".format(SHARE_ZIP_DIR))
        return None, None

    zips = []
    for filename in os.listdir(SHARE_ZIP_DIR):
        rev = extract_revision(filename)
        if rev > 0:
            zips.append((rev, filename))

    if not zips:
        log("[ERROR] no GalaxCore_xxxxx.zip found in {}".format(SHARE_ZIP_DIR))
        return None, None

    zips.sort(key=lambda x: x[0], reverse=True)
    rev, filename = zips[0]
    zip_path = os.path.join(SHARE_ZIP_DIR, filename)
    log("[INFO] Latest zip found: {}".format(filename))
    log("[INFO] Parsed SVN revision: {}".format(rev))
    return rev, zip_path


def find_revision_zip(revision: str) -> Tuple[Optional[int], Optional[str]]:
    revision = normalize_revision_value(revision)
    if not revision:
        return None, None

    if not os.path.isdir(SHARE_ZIP_DIR):
        log("[ERROR] share zip directory not found: {}".format(SHARE_ZIP_DIR))
        return None, None

    exact_names = [
        "GalaxCore_{}.zip".format(revision),
        "GalaxCore_r{}.zip".format(revision),
        "GalaxCore-{}.zip".format(revision),
    ]

    for name in exact_names:
        path = os.path.join(SHARE_ZIP_DIR, name)
        if os.path.isfile(path):
            log("[INFO] Specific zip found: {}".format(name))
            return int(revision), path

    # Fuzzy fallback – still must contain the revision digits somewhere
    fuzzy = []
    for filename in os.listdir(SHARE_ZIP_DIR):
        if filename.endswith(".zip") and revision in filename:
            fuzzy.append(filename)

    if fuzzy:
        fuzzy.sort(reverse=True)
        path = os.path.join(SHARE_ZIP_DIR, fuzzy[0])
        log("[INFO] Specific zip fuzzy matched: {}".format(fuzzy[0]))
        return int(revision), path

    log("[ERROR] GalaxCore zip for revision {} not found in {}".format(revision, SHARE_ZIP_DIR))
    return None, None


def select_zip_for_task(task: Dict[str, Any]) -> Tuple[Optional[int], Optional[str], str]:
    """
    If the task specifies a revision, use that version.
    Otherwise use the latest GalaxCore_xxx.zip from the shared directory.
    """
    task_rev = get_task_revision(task)

    if task_rev:
        rev, zip_path = find_revision_zip(task_rev)
        mode = "specific"
    else:
        rev, zip_path = find_latest_zip()
        mode = "latest"

    return rev, zip_path, mode


def svn_update_latest() -> int:
    """Update workspace source code to latest revision.

    The GalaxCore binary version is controlled by the selected GalaxCore_xxx.zip,
    not by the source workspace revision. Avoid svn update -r <old_rev> here to
    prevent conflicts when the local workspace has newer files.
    """
    script = """
{prefix}
cd "{workspace}"
svn update
""".format(prefix=csh_runtime_prefix(), workspace=WORKSPACE_DIR)
    return run_csh(script)


def write_galaxcore_build_info(task_id: str, build_revision: int, zip_path: str) -> str:
    """Write selected GalaxCore build metadata for vivado_runner scripts.

    This file is intentionally key-value text instead of JSON so bash/csh/awk
    scripts can read it easily.
    """
    info_path = os.path.join(TEST_DIR, ".galaxcore_build_info")

    lines = [
        "GALAXCORE_BUILD_REVISION {}\n".format(build_revision),
        "GALAXCORE_REVISION {}\n".format(build_revision),
        "GALAXCORE_BUILD_ZIP {}\n".format(zip_path),
        "DTS_TASK_ID {}\n".format(task_id),
        "DTS_WORKER {}\n".format(WORKER_NAME),
        "CREATED_AT {}\n".format(now_ts()),
    ]

    with open(info_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    log("[INFO] GalaxCore build info written: {}".format(info_path))
    log("[INFO] GALAXCORE_BUILD_REVISION={}".format(build_revision))
    return info_path


def get_galaxcore_binary_busy_details() -> list:
    """Return fuser details when the target GalaxCore binary is in use."""
    if not GALAXCORE_BUSY_CHECK_ENABLED:
        return []

    if not os.path.exists(TARGET_BINARY):
        return []

    if not shutil.which("fuser"):
        log("[WARN] fuser not found in PATH, skip pre-pull GalaxCore busy check")
        return []

    try:
        p = subprocess.Popen(
            ["fuser", TARGET_BINARY],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        stdout, stderr = p.communicate()
    except Exception as exc:
        log("[WARN] Failed to run fuser for GalaxCore busy check: {}".format(exc))
        return []

    stdout = stdout.strip()
    stderr = stderr.strip()

    if p.returncode == 0:
        details = []
        if stdout:
            details.append("fuser stdout: {}".format(stdout))
        if stderr:
            details.append("fuser stderr: {}".format(stderr))
        if not details:
            details.append("fuser reports target GalaxCore binary is in use")
        return details

    if p.returncode == 1:
        return []

    if stdout:
        log("[WARN] fuser stdout: {}".format(stdout))
    if stderr:
        log("[WARN] fuser stderr: {}".format(stderr))
    log("[WARN] fuser returned unexpected rc={}, skip pre-pull busy decision".format(p.returncode))
    return []


def check_galaxcore_binary_not_in_use() -> int:
    if not os.path.exists(TARGET_BINARY):
        log("[WARN] target binary does not exist yet: {}".format(TARGET_BINARY))
        return 0

    if not shutil.which("fuser"):
        return fail("check binary in use", "fuser not found in PATH")

    log("[INFO] Checking whether target GalaxCore binary is in use: {}".format(TARGET_BINARY))
    busy_details = get_galaxcore_binary_busy_details()

    if busy_details:
        for detail in busy_details:
            log(detail)
        log("[INFO] Target GalaxCore binary is in use. Treat worker as busy and requeue task.")
        return WORKER_BUSY_RC

    log("[INFO] Target GalaxCore binary is not in use")
    return 0

def extract_zip(zip_path: str) -> int:
    if os.path.exists(TMP_DIR):
        shutil.rmtree(TMP_DIR)
    os.makedirs(TMP_DIR, exist_ok=True)

    log("[INFO] Extracting {} -> {}".format(zip_path, TMP_DIR))
    return run_bash("unzip -o {} -d {}".format(shell_quote(zip_path), shell_quote(TMP_DIR)))


def find_extracted_galaxcore_binary(tmp_dir: str) -> Optional[str]:
    direct_path = os.path.join(tmp_dir, "GalaxCore")
    if os.path.isfile(direct_path):
        return direct_path

    for root, _, files in os.walk(tmp_dir):
        if "GalaxCore" in files:
            return os.path.join(root, "GalaxCore")

    return None


def replace_binary_from_tmp() -> int:
    extracted_binary = find_extracted_galaxcore_binary(TMP_DIR)
    if not extracted_binary:
        return fail("replace binary", "GalaxCore executable not found in extracted zip")

    rc = check_galaxcore_binary_not_in_use()
    if rc != 0:
        return rc

    os.makedirs(BIN_DIR, exist_ok=True)
    shutil.copy2(extracted_binary, TARGET_BINARY)
    os.chmod(TARGET_BINARY, 0o755)
    log("[INFO] Binary replaced: {} -> {}".format(extracted_binary, TARGET_BINARY))
    return 0


# ============================================================
# flow_config handling
# ============================================================

def split_inline_comment(body: str) -> Tuple[str, str]:
    """
    Simple inline comment splitter. flow_config is usually plain key-value,
    we do not handle complex quoting scenarios.
    """
    if "#" not in body:
        return body.rstrip(), ""
    left, comment = body.split("#", 1)
    return left.rstrip(), " #" + comment


def line_key(line: str) -> Optional[str]:
    body = line.strip()
    if not body or body.startswith("#"):
        return None

    base, _ = split_inline_comment(line.rstrip("\n"))
    base = base.strip()
    if not base:
        return None

    if "=" in base:
        key = base.split("=", 1)[0].strip()
        return key or None

    parts = base.split(None, 1)
    if parts:
        return parts[0].strip()
    return None


def format_flow_line(original_line: str, key: str, value: str) -> str:
    has_newline = original_line.endswith("\n")
    body = original_line.rstrip("\n")
    base, comment = split_inline_comment(body)
    indent_match = re.match(r"^(\s*)", body)
    indent = indent_match.group(1) if indent_match else ""

    if "=" in base:
        new_body = "{}{} = {}{}".format(indent, key, value, comment)
    else:
        new_body = "{}{} {}{}".format(indent, key, value, comment)

    return new_body + ("\n" if has_newline else "")


def append_flow_line(key: str, value: str) -> str:
    if FLOW_CONFIG_APPEND_STYLE == "equal":
        return "{} = {}\n".format(key, value)
    return "{} {}\n".format(key, value)


def backup_flow_config() -> Optional[str]:
    if not os.path.exists(FLOW_CONFIG_FILE):
        return None

    backup_path = "{}.bak.{}".format(FLOW_CONFIG_FILE, datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(FLOW_CONFIG_FILE, backup_path)
    return backup_path


def update_local_flow_config(flow_config_updates: Dict[str, Any]) -> int:
    """
    Bulk-update the local test2/flow_config with the fields from the task JSON.
    Only the provided fields are modified; others are left unchanged.

    Supports both local formats:
        key value
        key = value
    """
    if not flow_config_updates:
        log("[INFO] task flow_config is empty, skip flow_config update")
        return 0

    if not os.path.exists(FLOW_CONFIG_FILE):
        return fail("update flow_config", "flow_config file not found: {}".format(FLOW_CONFIG_FILE))

    backup_path = backup_flow_config()
    if backup_path:
        log("[INFO] flow_config backup created: {}".format(backup_path))

    updates = {}
    for k, v in flow_config_updates.items():
        key = str(k).strip()
        if key:
            updates[key] = normalize_flow_value(v)

    with open(FLOW_CONFIG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    updated_keys = set()
    new_lines = []

    for line in lines:
        key = line_key(line)
        if key and key in updates:
            new_value = updates[key]
            new_lines.append(format_flow_line(line, key, new_value))
            updated_keys.add(key)
            log("[INFO] flow_config set: {} = {}".format(key, new_value))
        else:
            new_lines.append(line)

    missing_keys = [k for k in updates.keys() if k not in updated_keys]
    if missing_keys:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append("\n# added by distributed worker\n")
        for key in missing_keys:
            new_value = updates[key]
            new_lines.append(append_flow_line(key, new_value))
            log("[INFO] flow_config add: {} = {}".format(key, new_value))

    with open(FLOW_CONFIG_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    log("[INFO] flow_config updated: {}".format(FLOW_CONFIG_FILE))
    return 0



# ============================================================
# Current-user run.sh guard
# ============================================================

def get_current_user_run_sh_processes() -> list:
    """Return run.sh processes owned by the current Linux user."""
    if not RUN_SH_BUSY_CHECK_ENABLED:
        return []

    current_pid = os.getpid()
    current_user = getpass.getuser()
    matched_processes = []

    try:
        result = subprocess.run(
            ["ps", "-elf"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=False,
        )
    except Exception as exc:
        log("[WARN] Failed to check current-user run.sh processes: {}".format(exc))
        return []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue

        columns = line.split(None, 14)
        if len(columns) < 15:
            continue

        process_user = columns[2]
        pid_text = columns[3]
        cmdline = columns[14]

        if process_user != current_user:
            continue

        try:
            pid = int(pid_text)
        except ValueError:
            continue

        if pid == current_pid:
            continue

        if "worker.py" in cmdline or "worker_run_test.py" in cmdline:
            continue

        if RUN_SH_PATTERN.search(cmdline):
            matched_processes.append((pid, cmdline))

    return matched_processes


def log_run_sh_busy(processes: list, reason: str) -> None:
    """Log current-user run.sh processes that block worker polling or execution."""
    log("[INFO] {}".format(reason))
    for pid, cmdline in processes:
        log("[INFO] Existing run.sh: pid={}, cmd={}".format(pid, cmdline))


def wait_until_current_user_run_sh_free(reason: str) -> None:
    """Wait until the current user no longer has a run.sh process."""
    last_log_time = 0

    while True:
        processes = get_current_user_run_sh_processes()
        if not processes:
            return

        now = time.time()
        if now - last_log_time >= RUN_SH_BUSY_LOG_INTERVAL:
            log_run_sh_busy(processes, reason)
            log("[INFO] Worker will wait {}s before checking run.sh again".format(RUN_SH_BUSY_SLEEP))
            last_log_time = now

        time.sleep(RUN_SH_BUSY_SLEEP)


def clean_test_dir_before_cmd() -> int:
    """Run test2/clean.sh before executing the task command."""
    clean_script = os.path.join(TEST_DIR, "clean.sh")

    if not os.path.isfile(clean_script):
        log("[WARN] clean.sh not found, skip pre-command cleanup: {}".format(clean_script))
        return 0

    log("[INFO] Run pre-command cleanup: {}".format(clean_script))
    script = """
set -e
cd {test_dir}
if [ -x ./clean.sh ]; then
    ./clean.sh
else
    /bin/bash ./clean.sh
fi
""".format(test_dir=shell_quote(TEST_DIR))
    return run_bash(script)


def get_worker_busy_state() -> Dict[str, Any]:
    """Collect local busy state before pulling or executing a task."""
    return {
        "run_sh_processes": get_current_user_run_sh_processes(),
        "galaxcore_busy_details": get_galaxcore_binary_busy_details(),
    }


def is_worker_busy_state(state: Dict[str, Any]) -> bool:
    """Return True if this worker should not pull or run a task now."""
    return bool(state.get("run_sh_processes") or state.get("galaxcore_busy_details"))


def log_worker_busy_state(state: Dict[str, Any], reason: str) -> None:
    """Log why this worker is busy."""
    log("[INFO] {}".format(reason))

    for pid, cmdline in state.get("run_sh_processes", []):
        log("[INFO] Existing run.sh: pid={}, cmd={}".format(pid, cmdline))

    for detail in state.get("galaxcore_busy_details", []):
        log("[INFO] Existing GalaxCore use: {}".format(detail))


def check_worker_not_busy_for_task() -> int:
    """Return WORKER_BUSY_RC if run.sh or GalaxCore becomes busy during a task."""
    state = get_worker_busy_state()
    if is_worker_busy_state(state):
        log_worker_busy_state(state, "Worker became busy before running task command. Requeue task.")
        return WORKER_BUSY_RC
    return 0


# ============================================================
# Environment / command checks
# ============================================================

def basic_checks() -> int:
    if not os.path.isdir(WORKSPACE_DIR):
        return fail("basic checks", "Workspace directory not found: {}".format(WORKSPACE_DIR))
    if not os.path.isdir(TEST_DIR):
        return fail("basic checks", "Test directory not found: {}".format(TEST_DIR))
    if not os.path.exists(FLOW_CONFIG_FILE):
        return fail("basic checks", "flow_config file not found: {}".format(FLOW_CONFIG_FILE))
    if not os.path.isdir(SHARE_ZIP_DIR):
        return fail("basic checks", "Share zip directory not found: {}".format(SHARE_ZIP_DIR))
    if not find_csh():
        return fail("basic checks", "tcsh/csh not found in PATH")
    if not shutil.which("svn"):
        return fail("basic checks", "svn not found in PATH")
    if not shutil.which("unzip"):
        return fail("basic checks", "unzip not found in PATH")

    log("[INFO] host: {}".format(socket.gethostname()))
    log("[INFO] worker name: {}".format(WORKER_NAME))
    log("[INFO] csh path: {}".format(find_csh()))
    log("[INFO] workspace: {}".format(WORKSPACE_DIR))
    log("[INFO] test dir: {}".format(TEST_DIR))
    log("[INFO] flow_config: {}".format(FLOW_CONFIG_FILE))
    log("[INFO] share zip dir: {}".format(SHARE_ZIP_DIR))
    log("[INFO] target binary: {}".format(TARGET_BINARY))
    log("[INFO] protobuf lib dir: {}".format(PROTOBUF_LIB_DIR))
    log("[INFO] ignore run.sh rc: {}".format(int(IGNORE_RUN_SH_RC)))
    log("[INFO] log file: {}".format(LOG_FILE))
    return 0


def dump_csh_runtime_env() -> int:
    env_dump = "/tmp/galaxcore_worker_env_{}_{}.txt".format(socket.gethostname(), os.getpid())
    script = """
{prefix}
cd "{test_dir}"
echo '[DEBUG] pwd:' `pwd`
echo '[DEBUG] csh command loaded successfully'
echo '[DEBUG] PATH:' $PATH
echo '[DEBUG] LD_LIBRARY_PATH:' $LD_LIBRARY_PATH
echo '[DEBUG] env dump: {env_dump}'
env | sort > "{env_dump}"
""".format(prefix=csh_runtime_prefix(), test_dir=TEST_DIR, env_dump=env_dump)
    return run_csh(script)


def check_galaxcore_libs() -> int:
    script = """
{prefix}
cd "{test_dir}"
echo '[DEBUG] pwd:' `pwd`
echo '[DEBUG] GalaxCore path:'
ls -l "{target_binary}"
echo '[DEBUG] ldd missing libs:'
ldd "{target_binary}" | grep 'not found' || true
""".format(prefix=csh_runtime_prefix(), test_dir=TEST_DIR, target_binary=TARGET_BINARY)
    return run_csh(script)


def run_task_command(cmd: str, build_revision: int, zip_path: str, build_info_path: str) -> int:
    ignore_flag = "1" if IGNORE_RUN_SH_RC else "0"

    script = """
{prefix}
setenv GALAXCORE_RUN_MODE "distributed"
setenv DTS_RUN_MODE "distributed"
setenv GALAXCORE_BUILD_REVISION "{build_revision}"
setenv GALAXCORE_REVISION "{build_revision}"
setenv GALAXCORE_BUILD_ZIP "{zip_path}"
setenv GALAXCORE_BUILD_INFO "{build_info_path}"
setenv GALAXCORE_WORKER_NAME "{worker}"
setenv GALAXCORE_TASK_ID "{task_id}"
setenv GALAXCORE_FLOW_CONFIG "{flow_config}"
setenv DTS_WORKER "{worker}"
setenv DTS_TASK_ID "{task_id}"
setenv DTS_FLOW_CONFIG "{flow_config}"

echo "[INFO] GALAXCORE_RUN_MODE: $GALAXCORE_RUN_MODE"
echo "[INFO] GALAXCORE_BUILD_REVISION: $GALAXCORE_BUILD_REVISION"
echo "[INFO] GALAXCORE_BUILD_ZIP: $GALAXCORE_BUILD_ZIP"
echo "[INFO] GALAXCORE_BUILD_INFO: $GALAXCORE_BUILD_INFO"

{cmd}
set run_rc = $status
echo "[INFO] task command finished with exit code $run_rc"
if ( "{ignore_flag}" == "1" ) then
    echo "[INFO] Ignore task command rc and continue worker flow, same as night_build style"
    exit 0
else
    exit $run_rc
endif
""".format(
        prefix=csh_runtime_prefix(),
        build_revision=build_revision,
        zip_path=zip_path,
        build_info_path=build_info_path,
        worker=WORKER_NAME,
        task_id=CURRENT_TASK_ID,
        flow_config=FLOW_CONFIG_FILE,
        cmd=cmd,
        ignore_flag=ignore_flag,
    )

    return run_csh(script)


def post_run_cleanup() -> int:
    if not POST_RUN_CLEANUP:
        log("[INFO] POST_RUN_CLEANUP=0, keep test output files for inspection")
        return 0

    script = """
set -e
cd {test_dir}
./clean.sh 2>/dev/null || true
[ -d kintexuplus ] && rm -rf kintexuplus/*
[ -d virtexuplus ] && rm -rf virtexuplus/*
""".format(test_dir=shell_quote(TEST_DIR))
    return run_bash(script)


def cleanup_tmp() -> None:
    if CLEAN_TMP_DIR:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        log("[INFO] Temporary directory cleaned: {}".format(TMP_DIR))


# ============================================================
# Per-task pipeline
# ============================================================

def run_attempt(task: Dict[str, Any]) -> int:
    global FAILED_STEP, FAILED_REASON, CURRENT_STEP
    FAILED_STEP = ""
    FAILED_REASON = ""
    CURRENT_STEP = ""

    task_id = get_task_id(task)
    cmd = get_task_cmd(task)
    task_revision = get_task_revision(task)
    flow_config_updates = get_task_flow_config(task)

    log("============================================================")
    log("Worker task started")
    log("Project    : {}".format(PROJECT_NAME))
    log("Task ID    : {}".format(task_id))
    log("Worker     : {}".format(WORKER_NAME))
    log("Revision   : {}".format(task_revision if task_revision else "latest"))
    log("Workspace  : {}".format(WORKSPACE_DIR))
    log("Test dir   : {}".format(TEST_DIR))
    log("Attempt    : {}/{}".format(CURRENT_ATTEMPT, MAX_ATTEMPTS))
    log("Cmd        : {}".format(cmd))
    log("Flow fields: {}".format(", ".join(sorted(flow_config_updates.keys())) if flow_config_updates else "empty"))
    log("Log file   : {}".format(LOG_FILE))
    log("============================================================")

    rc = run_step("basic checks", basic_checks)
    if rc != 0:
        return rc

    rev_zip = {"rev": None, "zip_path": None, "mode": None}

    def step_select_zip() -> int:
        rev, zip_path, mode = select_zip_for_task(task)
        if rev is None or zip_path is None:
            return 1
        rev_zip["rev"] = rev
        rev_zip["zip_path"] = zip_path
        rev_zip["mode"] = mode
        if mode == "specific":
            log("[INFO] Use specific GalaxCore revision: {}".format(rev))
        else:
            log("[INFO] Use latest GalaxCore revision: {}".format(rev))
        log("[INFO] Use zip: {}".format(zip_path))
        return 0

    rc = run_step("select GalaxCore zip", step_select_zip)
    if rc != 0:
        return rc

    rc = run_step("svn update latest", svn_update_latest)
    if rc != 0:
        return rc

    rc = run_step("check binary in use", check_galaxcore_binary_not_in_use)
    if rc != 0:
        return rc

    rc = run_step("extract GalaxCore zip", lambda: extract_zip(str(rev_zip["zip_path"])))
    if rc != 0:
        return rc

    rc = run_step("replace GalaxCore binary", replace_binary_from_tmp)
    if rc != 0:
        return rc

    build_info_path_holder = {"path": ""}

    def step_write_build_info() -> int:
        build_info_path_holder["path"] = write_galaxcore_build_info(
            task_id=str(task_id),
            build_revision=int(rev_zip["rev"]),
            zip_path=str(rev_zip["zip_path"]),
        )
        return 0

    rc = run_step("write GalaxCore build info", step_write_build_info)
    if rc != 0:
        return rc

    rc = run_step("update flow_config from task", lambda: update_local_flow_config(flow_config_updates))
    if rc != 0:
        return rc

    rc = run_step("dump csh runtime env", dump_csh_runtime_env)
    if rc != 0:
        return rc

    rc = run_step("check GalaxCore libs", check_galaxcore_libs)
    if rc != 0:
        return rc

    rc = run_step("pre-command clean.sh", clean_test_dir_before_cmd)
    if rc != 0:
        return rc

    rc = run_step("pre-command worker busy check", check_worker_not_busy_for_task)
    if rc != 0:
        return rc

    rc = run_step(
        "run task command",
        lambda: run_task_command(
            cmd,
            build_revision=int(rev_zip["rev"]),
            zip_path=str(rev_zip["zip_path"]),
            build_info_path=build_info_path_holder["path"],
        ),
    )
    if rc != 0:
        return rc

    rc = run_step("post run cleanup", post_run_cleanup)
    if rc != 0:
        return rc

    log("Worker task attempt finished successfully")
    return 0


def execute_task(task: Dict[str, Any]) -> int:
    global CURRENT_TASK_ID, CURRENT_ATTEMPT

    task_id = get_task_id(task)
    if not task_id:
        raise RuntimeError("task has no id/task_id: {}".format(task))

    CURRENT_TASK_ID = str(task_id)
    task_revision = get_task_revision(task)

    report_task(task, "running", exit_code=None, message="task started")

    final_rc = 1
    for attempt in range(1, MAX_ATTEMPTS + 1):
        CURRENT_ATTEMPT = attempt
        final_rc = run_attempt(task)

        if final_rc == WORKER_BUSY_RC:
            message = "worker local busy detected during task execution; requeue without failure"
            append_daily_summary(task_id, task_revision or "latest", "BUSY_REQUEUED")
            requeue_task(task, message)
            return 0

        if final_rc == 0:
            append_daily_summary(task_id, task_revision or "latest", "SUCCESS")
            report_task(task, "success", exit_code=0, message="task finished successfully")
            return 0

        if attempt < MAX_ATTEMPTS:
            log("[INFO] Attempt {} failed at step [{}]".format(attempt, FAILED_STEP))
            log("[INFO] Reason: {}".format(FAILED_REASON))
            log("[INFO] Sleep {}s, then restart whole task flow".format(RETRY_INTERVAL))
            time.sleep(RETRY_INTERVAL)

    append_daily_summary(task_id, task_revision or "latest", "FAILED")
    report_task(task, "failed", exit_code=final_rc, message="task failed at step [{}]: {}".format(FAILED_STEP, FAILED_REASON))
    return final_rc


# ============================================================
# Main worker loop
# ============================================================

def main() -> int:
    ensure_log_dir()

    lock_fd = open(LOCK_FILE, "w")
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("[WARN] Another worker is still running. Exit without starting a new one.")
            return 0

        log("============================================================")
        log("Distributed worker started")
        log("Worker     : {}".format(WORKER_NAME))
        log("Scheduler  : {}".format(SCHEDULER_URL))
        log("Pull API   : {}".format(PULL_API))
        log("Report API : {}".format(REPORT_API))
        log("Workspace  : {}".format(WORKSPACE_DIR))
        log("Test dir   : {}".format(TEST_DIR))
        log("flow_config: {}".format(FLOW_CONFIG_FILE))
        log("Zip dir    : {}".format(SHARE_ZIP_DIR))
        log("Binary     : {}".format(TARGET_BINARY))
        log("Log file   : {}".format(LOG_FILE))
        log("============================================================")

        wait_for_scheduler()

        last_idle_log_time = 0
        last_scheduler_error_log_time = 0
        last_worker_busy_log_time = 0

        while True:
            try:
                busy_state = get_worker_busy_state()
                if is_worker_busy_state(busy_state):
                    now = time.time()
                    if now - last_worker_busy_log_time >= max(RUN_SH_BUSY_LOG_INTERVAL, GALAXCORE_BUSY_LOG_INTERVAL):
                        log_worker_busy_state(
                            busy_state,
                            "Worker is busy. It will not pull a task."
                        )
                        last_worker_busy_log_time = now
                    time.sleep(max(RUN_SH_BUSY_SLEEP, GALAXCORE_BUSY_SLEEP))
                    continue

                task = pull_task()

                if not task:
                    now = time.time()
                    if now - last_idle_log_time >= 60:
                        log("Idle | no task")
                        last_idle_log_time = now
                    time.sleep(POLL_INTERVAL)
                    continue

                busy_state = get_worker_busy_state()
                if is_worker_busy_state(busy_state):
                    log_worker_busy_state(
                        busy_state,
                        "Worker became busy immediately after pulling task. Requeue without failure."
                    )
                    requeue_task(task, "worker became busy immediately after pulling task")
                    time.sleep(max(RUN_SH_BUSY_SLEEP, GALAXCORE_BUSY_SLEEP))
                    continue

                execute_task(task)
                cleanup_tmp()

            except KeyboardInterrupt:
                log("Worker stopped by user")
                return 0

            except SchedulerConnectionError as exc:
                now = time.time()
                if now - last_scheduler_error_log_time >= SCHEDULER_WARN_INTERVAL:
                    log("[WARN] Scheduler communication failed: {}".format(exc))
                    log("[INFO] Worker will keep waiting and retry polling tasks")
                    last_scheduler_error_log_time = now
                time.sleep(POLL_INTERVAL)

            except Exception as exc:
                log("[ERROR] worker loop exception: {}".format(exc))
                log(traceback.format_exc())
                time.sleep(POLL_INTERVAL)

    finally:
        cleanup_tmp()
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            lock_fd.close()


if __name__ == "__main__":
    sys.exit(main())