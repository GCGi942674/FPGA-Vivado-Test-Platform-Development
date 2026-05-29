#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import time
import shutil
import subprocess
from datetime import datetime

# ==============================
# Configuration
# ==============================

PROJECT_NAME = "galaxcore"

WORKSPACE_DIR = os.path.expanduser("~/workspace/galaxcore")
TEST_DIR = os.path.join(WORKSPACE_DIR, "test2")

SHARE_ZIP_DIR = "/home/xiaonan/Share/zw_cache/Galaxcore_bin/zip"

BIN_DIR = os.path.join(WORKSPACE_DIR, "bin/Linux_64")
TARGET_BINARY = os.path.join(BIN_DIR, "GalaxCore")

FLOW_CONFIG_FILE = os.path.join(TEST_DIR, "flow_config")

BASE_DIR = os.path.expanduser("~/distributed_ci")

LOG_ROOT = os.path.join(BASE_DIR, "logs")
STATE_ROOT = os.path.join(BASE_DIR, "state")
TMP_ROOT = os.path.join(BASE_DIR, "tmp")

STATE_FILE = os.path.join(STATE_ROOT, "last_revision.txt")

PROTOBUF_LIB_DIR = "/home/fpga/lib/protobuf-3.9.0/lib"

POST_CLEANUP = True

LOCK_FILE = "/tmp/galaxcore_worker.lock"

# ==============================
# Utilities
# ==============================

def log(msg: str, log_file: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(log_file, "a") as f:
        f.write(line + "\n")


def run_cmd(cmd, log_file):
    """Run shell command with logging."""
    log(f"[CMD] {cmd}", log_file)
    result = subprocess.run(cmd, shell=True)
    return result.returncode


def extract_revision(filename: str):
    """Extract revision number from GalaxCore_xxx.zip"""
    m = re.search(r"GalaxCore_(\d+)\.zip", filename)
    return int(m.group(1)) if m else -1


def find_latest_zip():
    """Find latest revision zip in share directory"""
    zips = []
    for f in os.listdir(SHARE_ZIP_DIR):
        if f.startswith("GalaxCore_") and f.endswith(".zip"):
            rev = extract_revision(f)
            if rev > 0:
                zips.append((rev, f))

    if not zips:
        return None, None

    zips.sort(key=lambda x: x[0], reverse=True)
    return zips[0]


def load_last_revision():
    if not os.path.exists(STATE_FILE):
        return -1
    try:
        with open(STATE_FILE, "r") as f:
            return int(f.read().strip())
    except:
        return -1


def save_revision(rev):
    os.makedirs(LOG_ROOT, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        f.write(str(rev))


def update_flow_config():
    """Ensure enable_copy = 1"""
    if not os.path.exists(FLOW_CONFIG_FILE):
        return False

    with open(FLOW_CONFIG_FILE, "r") as f:
        lines = f.readlines()

    found = False
    new_lines = []

    for line in lines:
        if "enable_copy" in line:
            new_lines.append("enable_copy 1\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append("\nenable_copy 1\n")

    with open(FLOW_CONFIG_FILE, "w") as f:
        f.writelines(new_lines)

    return True


def replace_binary(zip_path, log_file):
    """Extract zip and replace GalaxCore binary"""

    tmp_dir = "/tmp/galaxcore_worker"
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)

    os.makedirs(tmp_dir, exist_ok=True)

    log(f"Extracting {zip_path}", log_file)

    run_cmd(f"unzip -o {zip_path} -d {tmp_dir}", log_file)

    extracted_binary = os.path.join(tmp_dir, "GalaxCore")

    if not os.path.exists(extracted_binary):
        log("ERROR: GalaxCore not found in zip", log_file)
        return False

    os.makedirs(BIN_DIR, exist_ok=True)

    shutil.copy2(extracted_binary, TARGET_BINARY)
    os.chmod(TARGET_BINARY, 0o755)

    log(f"Binary replaced -> {TARGET_BINARY}", log_file)
    return True


def run_tests(log_file):
    """Execute run.sh"""
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = f"{PROTOBUF_LIB_DIR}:" + env.get("LD_LIBRARY_PATH", "")

    cmd = f"cd {TEST_DIR} && ./run.sh ."
    return run_cmd(cmd, log_file)


# ==============================
# Main worker logic
# ==============================

def main():
    os.makedirs(LOG_ROOT, exist_ok=True)

    log_file = os.path.join(LOG_ROOT, f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

    # ==============================
    # Lock
    # ==============================
    lock_fd = open(LOCK_FILE, "w")
    try:
        import fcntl
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except:
        print("Another worker job is running, exit.")
        return

    log("Worker started", log_file)

    # ==============================
    # Find latest zip
    # ==============================
    rev, zip_file = find_latest_zip()

    if not zip_file:
        log("No zip found", log_file)
        return

    log(f"Latest revision: {rev}, file: {zip_file}", log_file)

    last_rev = load_last_revision()
    if rev <= last_rev:
        log(f"Skip revision {rev}, already executed {last_rev}", log_file)
        return

    zip_path = os.path.join(SHARE_ZIP_DIR, zip_file)

    # ==============================
    # Replace binary
    # ==============================
    if not replace_binary(zip_path, log_file):
        log("Binary replace failed", log_file)
        return

    # ==============================
    # Flow config
    # ==============================
    update_flow_config()

    # ==============================
    # Run tests
    # ==============================
    rc = run_tests(log_file)

    # ==============================
    # Save state
    # ==============================
    save_revision(rev)

    # ==============================
    # Cleanup
    # ==============================
    if POST_CLEANUP:
        shutil.rmtree("/tmp/galaxcore_worker", ignore_errors=True)

    log(f"Worker finished with rc={rc}", log_file)


if __name__ == "__main__":
    main()