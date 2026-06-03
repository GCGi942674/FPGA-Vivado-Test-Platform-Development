#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
worker_run_test.py

Role:
  - Pull one task from scheduler.
  - Use the latest GalaxCore zip produced by the always-running GalaxCore_build.
  - Replace local ~/workspace/galaxcore/bin/Linux_64/GalaxCore.
  - Run ~/workspace/galaxcore/test2/run.sh <target>.
  - Report result back to scheduler.

This script is intentionally NOT responsible for building GalaxCore.
GalaxCore_build / build_GalaxCore.py should run separately and continuously
produce GalaxCore_latest.zip.
"""

import argparse
import errno
import fcntl
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime


# ==============================
# Default configuration
# ==============================

PROJECT_NAME = "galaxcore"

WORKSPACE_DIR = os.environ.get(
    "GALAXCORE_WORKSPACE",
    os.path.expanduser("~/workspace/galaxcore"),
)
TEST_DIR = os.path.join(WORKSPACE_DIR, "test2")

BIN_DIR = os.path.join(WORKSPACE_DIR, "bin/Linux_64")
TARGET_BINARY = os.path.join(BIN_DIR, "GalaxCore")

FLOW_CONFIG_FILE = os.path.join(TEST_DIR, "flow_config")

SHARE_ZIP_DIR = os.environ.get(
    "GALAXCORE_ZIP_DIR",
    "/home/xiaonan/Share/zw_cache/GalaxCore_bin/zip",
)

# build_GalaxCore.py should continuously create/update this file.
LATEST_ZIP_NAME = os.environ.get("GALAXCORE_LATEST_ZIP", "GalaxCore_latest.zip")

PROTOBUF_LIB_DIR = os.environ.get(
    "PROTOBUF_LIB_DIR",
    "/home/fpga/lib/protobuf-3.9.0/lib",
)

TEST_RUN_DIR = os.environ.get("GALAXCORE_TEST_TARGET", "kintexuplus/")

LOG_ROOT = os.environ.get(
    "GALAXCORE_WORKER_LOG_DIR",
    os.path.expanduser("~/logs/galaxcore_worker"),
)

STATE_ROOT = os.environ.get(
    "GALAXCORE_WORKER_STATE_DIR",
    os.path.expanduser("~/logs/galaxcore_worker/state"),
)

TMP_ROOT = os.environ.get(
    "GALAXCORE_WORKER_TMP_DIR",
    "/tmp/galaxcore_worker",
)

LOCK_FILE = os.environ.get(
    "GALAXCORE_WORKER_LOCK",
    "/tmp/galaxcore_worker.lock",
)

DEFAULT_SERVER = os.environ.get("SCHEDULER_URL", "http://127.0.0.1:9000")

PULL_API = os.environ.get("SCHEDULER_PULL_API", "/api/task/pull")
REPORT_API = os.environ.get("SCHEDULER_REPORT_API", "/api/task/report")

POST_CLEANUP = os.environ.get("GALAXCORE_POST_CLEANUP", "1") != "0"


# ==============================
# Runtime globals
# ==============================

LOG_FILE = None
VERBOSE = False


# ==============================
# Small utilities
# ==============================

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path)


def make_log_file(worker_name):
    ensure_dir(LOG_ROOT)
    date_tag = datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = "{}_{}_{}.log".format(date_tag, worker_name, ts)
    return os.path.join(LOG_ROOT, filename)


def log(msg, console=True):
    global LOG_FILE

    line = "[{}] {}".format(now_str(), msg)

    if console:
        print(line)
        sys.stdout.flush()

    if LOG_FILE:
        ensure_dir(os.path.dirname(LOG_FILE))
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")


def log_cmd_output(line):
    """Command output goes to log file. It is printed only in verbose mode."""
    line = line.rstrip("\n")

    if VERBOSE:
        print(line)
        sys.stdout.flush()

    if LOG_FILE:
        ensure_dir(os.path.dirname(LOG_FILE))
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")


def run_cmd(cmd, env=None, cwd=None):
    log("[CMD] {}".format(cmd), console=VERBOSE)

    try:
        p = subprocess.Popen(
            cmd,
            shell=True,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        )
    except Exception as e:
        log("[ERROR] failed to start command: {}".format(e))
        return 1

    if p.stdout is not None:
        for line in p.stdout:
            log_cmd_output(line)

    return p.wait()


def atomic_copy(src, dst):
    ensure_dir(os.path.dirname(dst))
    tmp = dst + ".new"
    shutil.copy2(src, tmp)
    os.chmod(tmp, 0o755)
    os.rename(tmp, dst)


def remove_tree(path):
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)


def json_post(server, api_path, data, timeout=30):
    url = server.rstrip("/") + api_path
    body = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
    )

    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        raise RuntimeError("HTTP {} {}: {}".format(e.code, url, raw))
    except Exception as e:
        raise RuntimeError("request failed {}: {}".format(url, e))

    if not raw.strip():
        return {}

    try:
        return json.loads(raw)
    except Exception:
        raise RuntimeError("invalid JSON response from {}: {}".format(url, raw))


def get_task_id(task):
    for key in ("task_id", "id"):
        if key in task and task.get(key) is not None:
            return task.get(key)
    return None


def get_task_project(task):
    return str(task.get("project") or task.get("project_name") or "").lower()


def get_task_params(task):
    params = task.get("params")
    if isinstance(params, dict):
        return params

    if isinstance(params, str) and params.strip():
        try:
            obj = json.loads(params)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    return {}


# ==============================
# Scheduler API
# ==============================

def pull_task(server, worker_name):
    data = {
        "worker": worker_name,
    }

    resp = json_post(server, PULL_API, data)

    # Accept several common response shapes:
    #   {"ok": true, "task": {...}}
    #   {"ok": true, "data": {...}}
    #   {"ok": false, "error": "no task"}
    #   {...task...}
    if isinstance(resp, dict):
        if resp.get("ok") is False:
            return None, resp.get("error", "no task")

        task = resp.get("task")
        if task is None:
            task = resp.get("data")

        if isinstance(task, dict) and task:
            return task, None

        # If response itself looks like a task.
        if "task_id" in resp or "id" in resp:
            return resp, None

    return None, "no task"


def report_task(server, worker_name, task, status, exit_code, message,
                revision=None, zip_name=None):
    task_id = get_task_id(task)

    data = {
        "worker": worker_name,
        "task_id": task_id,
        "status": status,
        "exit_code": exit_code,
        "message": message,
        "project": PROJECT_NAME,
        "revision": revision,
        "zip_name": zip_name,
        "log_file": LOG_FILE,
        "finished_at": now_str(),
    }

    # Also include id for scheduler implementations using "id".
    data["id"] = task_id

    try:
        resp = json_post(server, REPORT_API, data)
        log("[Worker] reported task={} status={} rc={}".format(
            task_id, status, exit_code
        ))
        return resp
    except Exception as e:
        log("[ERROR] report failed: {}".format(e))
        return None


# ==============================
# Zip selection
# ==============================

def extract_revision_from_name(name):
    if not name:
        return -1

    base = os.path.basename(name)

    patterns = [
        r"^GalaxCore_(\d+)\.zip$",
        r"^Galaxcore_(\d+)\.zip$",
    ]

    for pat in patterns:
        m = re.match(pat, base)
        if m:
            return int(m.group(1))

    return -1


def latest_zip_candidates():
    names = [
        LATEST_ZIP_NAME,
        "GalaxCore_latest.zip",
        "Galaxcore_latest.zip",
    ]

    seen = set()
    out = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        path = os.path.join(SHARE_ZIP_DIR, name)
        if os.path.exists(path):
            out.append(path)

    return out


def find_revision_zip(revision):
    if not revision:
        return None, -1

    try:
        rev = int(revision)
    except Exception:
        return None, -1

    names = [
        "GalaxCore_{}.zip".format(rev),
        "Galaxcore_{}.zip".format(rev),
    ]

    for name in names:
        path = os.path.join(SHARE_ZIP_DIR, name)
        if os.path.exists(path):
            return path, rev

    return None, -1


def find_highest_revision_zip():
    if not os.path.isdir(SHARE_ZIP_DIR):
        return None, -1

    zips = []
    for name in os.listdir(SHARE_ZIP_DIR):
        rev = extract_revision_from_name(name)
        if rev > 0:
            zips.append((rev, os.path.join(SHARE_ZIP_DIR, name)))

    if not zips:
        return None, -1

    zips.sort(key=lambda x: x[0], reverse=True)
    return zips[0][1], zips[0][0]


def detect_revision_from_path(path):
    rev = extract_revision_from_name(os.path.basename(path))
    if rev > 0:
        return rev

    real_path = os.path.realpath(path)
    rev = extract_revision_from_name(os.path.basename(real_path))
    if rev > 0:
        return rev

    return -1


def choose_zip_for_task(task):
    params = get_task_params(task)

    revision = (
        task.get("revision")
        or task.get("rev")
        or params.get("revision")
        or params.get("rev")
    )

    if revision:
        path, rev = find_revision_zip(revision)
        if path:
            return path, rev

        # The task asked for a fixed revision, so do not silently use latest.
        return None, -1

    # Default mode: consume GalaxCore_latest.zip.
    for path in latest_zip_candidates():
        return path, detect_revision_from_path(path)

    # Fallback: consume highest GalaxCore_<rev>.zip.
    return find_highest_revision_zip()


def wait_until_file_stable(path, checks=3, interval=1.0):
    """
    Avoid reading GalaxCore_latest.zip while build_GalaxCore.py is replacing it.

    The file is considered stable if size and mtime stay unchanged for several
    consecutive checks.
    """
    if not path or not os.path.exists(path):
        return False

    last = None
    stable_count = 0

    while stable_count < checks:
        try:
            st = os.stat(path)
            cur = (st.st_size, int(st.st_mtime))
        except OSError:
            stable_count = 0
            last = None
            time.sleep(interval)
            continue

        if cur == last and st.st_size > 0:
            stable_count += 1
        else:
            stable_count = 0
            last = cur

        time.sleep(interval)

    return True


def copy_zip_to_tmp(src_zip, worker_name, task_id):
    ensure_dir(TMP_ROOT)

    task_part = "task_{}".format(task_id if task_id is not None else "unknown")
    dst_dir = os.path.join(TMP_ROOT, worker_name, task_part)

    remove_tree(dst_dir)
    ensure_dir(dst_dir)

    dst_zip = os.path.join(dst_dir, os.path.basename(src_zip))
    shutil.copy2(src_zip, dst_zip)

    return dst_zip, dst_dir


# ==============================
# Worker test flow
# ==============================

def basic_checks():
    if not os.path.isdir(WORKSPACE_DIR):
        return False, "workspace not found: {}".format(WORKSPACE_DIR)

    if not os.path.isdir(TEST_DIR):
        return False, "test2 dir not found: {}".format(TEST_DIR)

    if not os.path.isdir(SHARE_ZIP_DIR):
        return False, "share zip dir not found: {}".format(SHARE_ZIP_DIR)

    if not os.path.exists(FLOW_CONFIG_FILE):
        return False, "flow_config not found: {}".format(FLOW_CONFIG_FILE)

    return True, ""


def check_galaxcore_binary_not_in_use():
    if not os.path.exists(TARGET_BINARY):
        return True, ""

    cmd = "fuser {}".format(shlex.quote(TARGET_BINARY))

    try:
        p = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        stdout, stderr = p.communicate()
    except Exception as e:
        return False, "failed to run fuser: {}".format(e)

    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    output = "\n".join([x for x in (stdout, stderr) if x]).strip()

    # fuser:
    #   0 -> file is in use
    #   1 -> no process is using it
    if p.returncode == 0:
        return False, "GalaxCore binary is in use: {}".format(output or TARGET_BINARY)

    if p.returncode == 1:
        return True, ""

    return False, "unexpected fuser rc={} output={}".format(p.returncode, output)


def find_extracted_galaxcore_binary(extract_dir):
    direct = os.path.join(extract_dir, "GalaxCore")
    if os.path.isfile(direct):
        return direct

    for root, dirs, files in os.walk(extract_dir):
        if "GalaxCore" in files:
            return os.path.join(root, "GalaxCore")

    return None


def replace_binary_from_zip(local_zip, extract_dir):
    remove_tree(extract_dir)
    ensure_dir(extract_dir)

    try:
        with zipfile.ZipFile(local_zip, "r") as zf:
            zf.extractall(extract_dir)
    except Exception as e:
        return False, "unzip failed: {}".format(e)

    binary = find_extracted_galaxcore_binary(extract_dir)
    if not binary:
        return False, "GalaxCore executable not found in zip"

    ok, reason = check_galaxcore_binary_not_in_use()
    if not ok:
        return False, reason

    try:
        atomic_copy(binary, TARGET_BINARY)
    except Exception as e:
        return False, "replace binary failed: {}".format(e)

    return True, ""


def update_flow_config():
    try:
        with open(FLOW_CONFIG_FILE, "r") as f:
            lines = f.readlines()
    except Exception as e:
        return False, "read flow_config failed: {}".format(e)

    found = False
    new_lines = []

    for line in lines:
        # run.sh/config.sh recognizes "enable_copy 1".
        # Avoid "enable_copy = 1".
        if re.match(r"^\s*enable_copy\b", line):
            new_lines.append("enable_copy 1\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append("\nenable_copy 1\n")

    try:
        with open(FLOW_CONFIG_FILE, "w") as f:
            f.writelines(new_lines)
    except Exception as e:
        return False, "write flow_config failed: {}".format(e)

    return True, ""


def run_tests(test_target):
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = "{}:{}".format(
        PROTOBUF_LIB_DIR,
        env.get("LD_LIBRARY_PATH", ""),
    )

    cmd = "cd {} && ./run.sh {}".format(
        shlex.quote(TEST_DIR),
        shlex.quote(test_target),
    )

    return run_cmd(cmd, env=env)


def execute_task(worker_name, task):
    task_id = get_task_id(task)
    params = get_task_params(task)

    test_target = (
        task.get("target")
        or task.get("test_target")
        or params.get("target")
        or params.get("test_target")
        or TEST_RUN_DIR
    )

    log("[Worker] task={} start project={} target={}".format(
        task_id, PROJECT_NAME, test_target
    ))

    ok, reason = basic_checks()
    if not ok:
        return 1, reason, None, None

    zip_path, rev = choose_zip_for_task(task)
    if not zip_path:
        return 1, "no suitable GalaxCore zip found in {}".format(SHARE_ZIP_DIR), None, None

    log("[Worker] use zip {}".format(zip_path))
    if rev > 0:
        log("[Worker] revision r{}".format(rev))

    if not wait_until_file_stable(zip_path):
        return 1, "zip is not stable or not found: {}".format(zip_path), rev, os.path.basename(zip_path)

    try:
        local_zip, task_tmp_dir = copy_zip_to_tmp(zip_path, worker_name, task_id)
    except Exception as e:
        return 1, "copy zip to tmp failed: {}".format(e), rev, os.path.basename(zip_path)

    extract_dir = os.path.join(task_tmp_dir, "extract")

    ok, reason = replace_binary_from_zip(local_zip, extract_dir)
    if not ok:
        return 1, reason, rev, os.path.basename(zip_path)

    ok, reason = update_flow_config()
    if not ok:
        return 1, reason, rev, os.path.basename(zip_path)

    rc = run_tests(test_target)

    if rc == 0:
        return 0, "success", rev, os.path.basename(zip_path)

    return rc, "run.sh failed rc={}".format(rc), rev, os.path.basename(zip_path)


# ==============================
# Main loop
# ==============================

def acquire_lock():
    lock_fd = open(LOCK_FILE, "w")

    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError as e:
        if getattr(e, "errno", None) in (errno.EACCES, errno.EAGAIN):
            return None
        return None
    except Exception:
        return None

    return lock_fd


def release_lock(lock_fd):
    if not lock_fd:
        return

    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        lock_fd.close()


def idle_print(worker_name, wait_sec):
    msg = "\r[Worker] Idle | worker={} wait: {}s".format(worker_name, wait_sec)
    sys.stdout.write(msg)
    sys.stdout.flush()


def parse_args():
    p = argparse.ArgumentParser(description="Distributed GalaxCore test worker")

    p.add_argument("--worker", default=os.environ.get("WORKER_NAME", os.uname()[1]))
    p.add_argument("--server", default=DEFAULT_SERVER)
    p.add_argument("--once", action="store_true", help="pull and run one task, then exit")
    p.add_argument("--interval", type=int, default=5, help="idle polling interval seconds")
    p.add_argument("--verbose", action="store_true", help="print command output to console")
    p.add_argument("--zip-dir", default=SHARE_ZIP_DIR)
    p.add_argument("--latest-name", default=LATEST_ZIP_NAME)
    p.add_argument("--test-target", default=TEST_RUN_DIR)

    return p.parse_args()


def main():
    global LOG_FILE, VERBOSE, SHARE_ZIP_DIR, LATEST_ZIP_NAME, TEST_RUN_DIR

    args = parse_args()

    VERBOSE = args.verbose
    SHARE_ZIP_DIR = args.zip_dir
    LATEST_ZIP_NAME = args.latest_name
    TEST_RUN_DIR = args.test_target

    LOG_FILE = make_log_file(args.worker)

    ensure_dir(LOG_ROOT)
    ensure_dir(STATE_ROOT)

    log("[Worker] started worker={} server={}".format(args.worker, args.server))
    log("[Worker] zip_dir={} latest={}".format(SHARE_ZIP_DIR, LATEST_ZIP_NAME))
    log("[Worker] workspace={} target={}".format(WORKSPACE_DIR, TEST_RUN_DIR))

    idle_start = time.time()

    while True:
        try:
            task, err = pull_task(args.server, args.worker)
        except Exception as e:
            log("[Worker] pull failed: {}".format(e))
            task = None
            err = str(e)

        if not task:
            if args.once:
                log("[Worker] no task")
                return 0

            wait_sec = int(time.time() - idle_start)
            idle_print(args.worker, wait_sec)
            time.sleep(args.interval)
            continue

        sys.stdout.write("\n")
        sys.stdout.flush()

        idle_start = time.time()

        task_id = get_task_id(task)
        project = get_task_project(task)

        if project and project != PROJECT_NAME:
            log("[Worker] skip unsupported task={} project={}".format(task_id, project))
            report_task(
                args.server,
                args.worker,
                task,
                "failed",
                1,
                "unsupported project: {}".format(project),
            )
            if args.once:
                return 1
            continue

        lock_fd = acquire_lock()
        if not lock_fd:
            log("[Worker] locked, another local task is running")
            report_task(
                args.server,
                args.worker,
                task,
                "failed",
                1,
                "worker locked, another local task is running",
            )
            if args.once:
                return 1
            continue

        rc = 1
        message = "unknown"
        rev = None
        zip_name = None

        try:
            rc, message, rev, zip_name = execute_task(args.worker, task)
        except Exception as e:
            rc = 1
            message = "exception: {}".format(e)
            log("[ERROR] {}".format(message))
        finally:
            release_lock(lock_fd)

            if POST_CLEANUP:
                remove_tree(os.path.join(TMP_ROOT, args.worker, "task_{}".format(task_id)))

        status = "success" if rc == 0 else "failed"

        report_task(
            args.server,
            args.worker,
            task,
            status,
            rc,
            message,
            revision=rev,
            zip_name=zip_name,
        )

        log("[Worker] task={} done status={} rc={} message={}".format(
            task_id, status, rc, message
        ))

        if args.once:
            return rc

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
