#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import shutil
import shlex
import subprocess
import fcntl
from datetime import datetime

# ==============================
# Configuration
# ==============================

PROJECT_NAME = "galaxcore"

WORKSPACE_DIR = os.path.expanduser("~/workspace/galaxcore")
TEST_DIR = os.path.join(WORKSPACE_DIR, "test2")

SHARE_ZIP_DIR = "/home/xiaonan/Share/zw_cache/GalaxCore_bin/zip"

BIN_DIR = os.path.join(WORKSPACE_DIR, "bin/Linux_64")
TARGET_BINARY = os.path.join(BIN_DIR, "GalaxCore")

FLOW_CONFIG_FILE = os.path.join(TEST_DIR, "flow_config")

BASE_DIR = os.path.expanduser("~/distributed_ci")

LOG_ROOT = os.path.join(BASE_DIR, "logs")

PROTOBUF_LIB_DIR = "/home/fpga/lib/protobuf-3.9.0/lib"

POST_CLEANUP = True

LOCK_FILE = "/tmp/galaxcore_worker.lock"
TMP_DIR = "/tmp/galaxcore_worker"

# ----- 你可以在这里修改测试时传入的文件夹名 -----
TEST_RUN_DIR = "kintexuplus/"   # 原来用的是 "."，这里换成你想要的文件夹名

# ==============================
# Utilities
# ==============================

def log(msg: str, log_file: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_cmd(cmd: str, log_file: str, env=None):
    """Run shell command with logging."""
    log(f"[CMD] {cmd}", log_file)
    result = subprocess.run(cmd, shell=True, env=env)
    return result.returncode


def extract_revision(filename: str):
    """Extract revision number from GalaxCore_xxx.zip."""
    m = re.search(r"^GalaxCore_(\d+)\.zip$", filename)
    return int(m.group(1)) if m else -1


def find_latest_zip():
    """Find latest GalaxCore revision zip in the shared directory."""
    if not os.path.isdir(SHARE_ZIP_DIR):
        return None, None

    zips = []
    for filename in os.listdir(SHARE_ZIP_DIR):
        rev = extract_revision(filename)
        if rev > 0:
            zips.append((rev, filename))

    if not zips:
        return None, None

    zips.sort(key=lambda x: x[0], reverse=True)
    return zips[0]


def svn_update_to_revision(rev: int, log_file: str):
    """Update local workspace to the revision parsed from the zip filename."""
    log(f"Updating workspace to SVN revision {rev}", log_file)
    cmd = f"svn update -r {rev} {shlex.quote(WORKSPACE_DIR)}"
    return run_cmd(cmd, log_file)


def check_galaxcore_binary_not_in_use(log_file: str):
    if not os.path.exists(TARGET_BINARY):
        return True

    log("Checking whether target GalaxCore binary is in use", log_file)
    cmd = f"fuser {shlex.quote(TARGET_BINARY)}"
    log(f"[CMD] {cmd}", log_file)

    result = subprocess.run(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    # fuser 退出码 0：至少有一个进程在使用文件
    if result.returncode == 0:
        output = stdout + "\n" + stderr if stdout or stderr else ""
        log(f"fuser output: {output}", log_file)
        log(f"ERROR: target GalaxCore binary is in use: {TARGET_BINARY}", log_file)
        return False

    # 退出码 1：通常表示没有进程使用该文件（可能有 stderr 警告）
    # 这种情况下我们认为二进制未被占用，可以安全替换
    if result.returncode == 1:
        if stderr:
            log(f"fuser warning (ignored): {stderr}", log_file)
        log("Target GalaxCore binary is not in use", log_file)
        return True

    # 其他退出码（如 127）视为异常，保守 abort
    log(f"ERROR: fuser returned unexpected rc={result.returncode}", log_file)
    if stdout or stderr:
        log(f"stdout: {stdout}", log_file)
        log(f"stderr: {stderr}", log_file)
    return False

def find_extracted_galaxcore_binary(tmp_dir: str):
    """Find GalaxCore executable extracted from the zip package."""
    direct_path = os.path.join(tmp_dir, "GalaxCore")
    if os.path.isfile(direct_path):
        return direct_path

    for root, _, files in os.walk(tmp_dir):
        if "GalaxCore" in files:
            return os.path.join(root, "GalaxCore")

    return None


def replace_binary(zip_path: str, log_file: str):
    """Extract latest zip and replace local GalaxCore binary."""

    if os.path.exists(TMP_DIR):
        shutil.rmtree(TMP_DIR)

    os.makedirs(TMP_DIR, exist_ok=True)

    log(f"Extracting {zip_path}", log_file)

    rc_unzip = run_cmd(
        f"unzip -o {shlex.quote(zip_path)} -d {shlex.quote(TMP_DIR)}",
        log_file,
    )
    if rc_unzip != 0:
        log("ERROR: unzip failed", log_file)
        return False

    extracted_binary = find_extracted_galaxcore_binary(TMP_DIR)
    if not extracted_binary:
        log("ERROR: GalaxCore executable not found in zip", log_file)
        return False

    os.makedirs(BIN_DIR, exist_ok=True)

    # Check again immediately before copy to reduce the race window.
    if not check_galaxcore_binary_not_in_use(log_file):
        return False

    shutil.copy2(extracted_binary, TARGET_BINARY)
    os.chmod(TARGET_BINARY, 0o755)

    log(f"Binary replaced -> {TARGET_BINARY}", log_file)
    return True


def update_flow_config(log_file: str):
    """Ensure test2/flow_config contains: enable_copy = 1."""
    if not os.path.exists(FLOW_CONFIG_FILE):
        log(f"ERROR: flow_config not found: {FLOW_CONFIG_FILE}", log_file)
        return False

    with open(FLOW_CONFIG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    found = False
    new_lines = []

    for line in lines:
        if re.match(r"^\s*enable_copy\b", line):
            new_lines.append("enable_copy = 1\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append("\nenable_copy 1\n")

    with open(FLOW_CONFIG_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    log("flow_config updated: enable_copy = 1", log_file)
    return True


def run_tests(log_file: str):
    """Enter test2 and execute ./run.sh <TEST_RUN_DIR>."""
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = f"{PROTOBUF_LIB_DIR}:" + env.get("LD_LIBRARY_PATH", "")

    # 原来这里是 ./run.sh . ，现在改为 ./run.sh <TEST_RUN_DIR>
    cmd = f"cd {shlex.quote(TEST_DIR)} && ./run.sh {shlex.quote(TEST_RUN_DIR)}"
    return run_cmd(cmd, log_file, env=env)


def cleanup(log_file: str):
    """Clean temporary worker directory."""
    if POST_CLEANUP:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        log(f"Temporary directory cleaned: {TMP_DIR}", log_file)


# ==============================
# Main worker logic
# ==============================

def main():
    os.makedirs(LOG_ROOT, exist_ok=True)
    log_file = os.path.join(LOG_ROOT, f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another worker job is running, exit.")
        return 0

    log("Worker started", log_file)

    rc = 1

    try:
        # 1. Find latest GalaxCore_xxxxx.zip
        rev, zip_file = find_latest_zip()
        if not zip_file:
            log(f"No GalaxCore_xxxxx.zip found in {SHARE_ZIP_DIR}", log_file)
            return 1

        zip_path = os.path.join(SHARE_ZIP_DIR, zip_file)
        log(f"Latest zip found: {zip_file}", log_file)

        # 2. Parse revision from zip filename
        log(f"Parsed SVN revision: {rev}", log_file)

        # 3. Update local workspace to the corresponding revision
        rc_svn = svn_update_to_revision(rev, log_file)
        if rc_svn != 0:
            log("SVN update failed, aborting.", log_file)
            return rc_svn

        # 4. Check whether GalaxCore binary is occupied. Do not kill anything.
        if not check_galaxcore_binary_not_in_use(log_file):
            return 1

        # 5. Extract latest zip and replace local GalaxCore binary
        if not replace_binary(zip_path, log_file):
            log("Binary replace failed, aborting.", log_file)
            return 1

        # 6. Update test2/flow_config
        if not update_flow_config(log_file):
            log("flow_config update failed, aborting.", log_file)
            return 1

        # 7. Run test2/run.sh; run.sh handles Place/Route/Bitgen/Report/Summary/Copy Result
        rc = run_tests(log_file)

        log(f"Worker finished with rc={rc}", log_file)
        return rc

    finally:
        cleanup(log_file)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            lock_fd.close()


if __name__ == "__main__":
    sys.exit(main())