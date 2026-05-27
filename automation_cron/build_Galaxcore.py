#!/usr/bin/env python3
"""
build_Galaxcore - continuously compile each SVN revision of Galaxcore.
Working directory: /home/user3/workspace/galaxcore
Binary source:     /home/user3/workspace/galaxcore/bin/Linux_64/GalaxCore
ZIP archives:      /home/xiaonan/Share/zw_cache/Galaxcore_bin/zip/
Logs & state:      /home/xiaonan/Share/zw_cache/Galaxcore_bin/
"""

import os
import sys
import time
import signal
import shutil
import subprocess
import glob
import re
import struct

# ------------------ Configuration ------------------
WORK_DIR        = "/home/user3/workspace/galaxcore"
BIN_SRC         = os.path.join(WORK_DIR, "bin/Linux_64/GalaxCore")
SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
TAR_DIR         = os.path.join(SCRIPT_DIR, "zip")
HISTORY_LOG     = os.path.join(SCRIPT_DIR, "build_history.log")
FAIL_LOG        = os.path.join(SCRIPT_DIR, "mk_fail")
CHECKPOINT_FILE = os.path.join(SCRIPT_DIR, "last_revision.txt")
SVN_URL         = "http://192.168.10.10/svn/galaxcore/galaxcore"
MAX_BIN_KEEP    = 150

# Build command (adjust if needed)
MAKE_CMD        = ["csh", "-c", "mk"]
MAKE_CLEAN_CMD  = ["make", "clean"]

# Retry settings for svn update
SVN_RETRY       = 3
SVN_RETRY_DELAY = 5

# Idle sleep when no new revisions are available
IDLE_SLEEP      = 60

# Global shutdown flag
shutdown_flag   = False

# ------------------ Utility functions ------------------
def log(msg: str) -> None:
    """Print a timestamped message to the terminal."""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def run_cmd(cmd, cwd: str = WORK_DIR, timeout: int = 600) -> int:
    """
    Execute a command, discard stdout/stderr, return exit code.
    Returns -1 on timeout or exception.
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        log(f"Command timed out: {' '.join(cmd)}")
        return -1
    except Exception as e:
        log(f"Command exception: {e}")
        return -1

def svn_update(revision: int) -> bool:
    """
    Update working copy to a specific revision.
    Performs revert first to avoid conflicts.
    Retries on failure.
    """
    # Discard local modifications before update
    run_cmd(["svn", "revert", "-R", "."])
    cmd = ["svn", "update", "-r", str(revision)]
    for attempt in range(1, SVN_RETRY + 1):
        ret = run_cmd(cmd)
        if ret == 0:
            return True
        log(f"svn update -r {revision} failed, attempt {attempt}/{SVN_RETRY}")
        time.sleep(SVN_RETRY_DELAY)
    return False

def get_svn_info(revision: int):
    """
    Retrieve author and date for a revision.
    Returns ('unknown', 'unknown') on failure.
    """
    cmd = ["svn", "log", "-r", str(revision), "--xml", SVN_URL]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode != 0:
            return "unknown", "unknown"
        # Simple XML parsing (no external library)
        author_match = re.search(r"<author>(.*?)</author>", result.stdout)
        date_match   = re.search(r"<date>(.*?)</date>", result.stdout)
        author = author_match.group(1) if author_match else "unknown"
        date   = date_match.group(1) if date_match else "unknown"
        # Format date to "YYYY-MM-DD HH:MM:SS"
        if date != "unknown":
            date = date.replace("T", " ").replace("Z", "")
        return author, date
    except Exception:
        return "unknown", "unknown"

def do_make() -> bool:
    """
    Attempt build. On first failure, runs make clean and tries again.
    Returns True on success.
    """
    if run_cmd(MAKE_CMD) == 0:
        return True
    log("First build attempt failed, trying make clean ...")
    run_cmd(MAKE_CLEAN_CMD)
    return run_cmd(MAKE_CMD) == 0

def compress_to_zip(revision: int) -> bool:
    """
    Zip the compiled binary into the tar directory.
    Returns True on success.
    """
    os.makedirs(TAR_DIR, exist_ok=True)
    zipfile = os.path.join(TAR_DIR, f"Galaxcore_{revision}.zip")
    # -j : junk directory names, store just the file
    cmd = ["zip", "-j", zipfile, BIN_SRC]
    return run_cmd(cmd, cwd=SCRIPT_DIR) == 0

def clean_old_archives() -> None:
    """Keep only the latest MAX_BIN_KEEP zip files, remove older ones."""
    pattern = os.path.join(TAR_DIR, "Galaxcore_*.zip")
    files = glob.glob(pattern)
    # Extract revision numbers
    archives = []
    for f in files:
        base = os.path.basename(f)
        if base.startswith("Galaxcore_") and base.endswith(".zip"):
            rev_str = base[len("Galaxcore_"):-4]
            if rev_str.isdigit():
                archives.append((int(rev_str), f))
    if len(archives) <= MAX_BIN_KEEP:
        return
    # Sort by revision ascending, delete the oldest ones
    archives.sort(key=lambda x: x[0])
    to_delete = archives[:len(archives) - MAX_BIN_KEEP]
    for _, path in to_delete:
        try:
            os.remove(path)
            log(f"Deleted old archive: {os.path.basename(path)}")
        except Exception as e:
            log(f"Failed to delete {path}: {e}")

# ------------------ Logging ------------------
def write_build_log(revision: int, author: str, date: str, status: str) -> None:
    """
    Append one build record to build_history.log using the required format:
    ----------------------------------------------------
    rREV | AUTHOR | DATE | STATUS
    ----------------------------------------------------
    """
    with open(HISTORY_LOG, 'a') as f:
        f.write("-" * 60 + "\n")
        f.write(f"r{revision} | {author} | {date} | {status}\n")
        f.write("-" * 60 + "\n")

def write_fail_log(revision: int) -> None:
    """Append a failure entry to mk_fail (kept simple)."""
    with open(FAIL_LOG, 'a') as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] FAIL r{revision}\n")

def init_logs() -> None:
    """Write a startup marker to the history log."""
    with open(HISTORY_LOG, 'a') as f:
        f.write("\n" + "=" * 60 + "\n")
        f.write(f"Monitor started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 60 + "\n")

# ------------------ Checkpoint management ------------------
def load_checkpoint() -> int:
    """Return the last successfully built revision (or HEAD-1 if no file)."""
    try:
        with open(CHECKPOINT_FILE, 'r') as f:
            return int(f.read().strip())
    except:
        # Start from HEAD-1 to only build the very latest revision
        head = get_head_revision()
        return max(head - 1, 0) if head else 0

def save_checkpoint(revision: int) -> None:
    """Store the latest built revision."""
    with open(CHECKPOINT_FILE, 'w') as f:
        f.write(str(revision))

def get_head_revision() -> int:
    """Return the newest revision in the repository (or -1 on error)."""
    cmd = ["svn", "info", "--show-item", "revision", SVN_URL]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except:
        pass
    return -1

# ------------------ Signal handling ------------------
def signal_handler(sig, frame):
    global shutdown_flag
    log("Received termination signal, exiting gracefully...")
    shutdown_flag = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ------------------ Build one revision ------------------
def build_revision(revision: int) -> None:
    """Checkout, build, archive, and log a single revision."""
    log(f"Building r{revision}")

    author, date = get_svn_info(revision)

    if not svn_update(revision):
        write_build_log(revision, author, date, "FAILED (svn update)")
        write_fail_log(revision)
        save_checkpoint(revision)
        return

    # First build attempt
    if do_make():
        # Success
        if os.path.isfile(BIN_SRC):
            if compress_to_zip(revision):
                clean_old_archives()
            else:
                log(f"Warning: zip failed for r{revision}")
            write_build_log(revision, author, date, "SUCCESS")
            save_checkpoint(revision)
            return
        else:
            log(f"Binary not found after build: {BIN_SRC}")
            write_build_log(revision, author, date, "FAILED (binary missing)")
            write_fail_log(revision)
            save_checkpoint(revision)
            return

    # Second attempt after clean
    log("Retrying after make clean...")
    run_cmd(MAKE_CLEAN_CMD)
    if do_make():
        if os.path.isfile(BIN_SRC):
            if compress_to_zip(revision):
                clean_old_archives()
            else:
                log(f"Warning: zip failed for r{revision}")
            write_build_log(revision, author, date, "SUCCESS")
            save_checkpoint(revision)
            return

    # Final failure
    write_build_log(revision, author, date, "FAILED")
    write_fail_log(revision)
    save_checkpoint(revision)   # advance to avoid infinite loop

# ------------------ Main loop ------------------
def main():
    global shutdown_flag

    # ... 前面的检查与初始化不变 ...

    while not shutdown_flag:
        head = get_head_revision()
        if head < 0:
            log("Cannot retrieve HEAD revision, retrying in 10s...")
            time.sleep(10)
            continue

        if head <= checkpoint:
            # 进入 idle，动态显示等待时间
            wait_start = time.time()
            log(f"Idle (local={checkpoint}, head={head}). Waiting for new revisions...")
            while not shutdown_flag:
                head = get_head_revision()
                if head > checkpoint:
                    break
                elapsed = int(time.time() - wait_start)
                print(f"\r[CI] Idle | local={checkpoint}  head={head}  wait: {elapsed}s", end='', flush=True)
                time.sleep(1)
            print()  # 换行，准备输出编译信息
            continue

        log(f"New revisions found: {checkpoint+1} -> {head}")
        for rev in range(checkpoint + 1, head + 1):
            if shutdown_flag:
                break
            build_revision(rev)
            checkpoint = rev

        checkpoint = load_checkpoint()
        time.sleep(2)

    log("Monitor stopped.")

if __name__ == "__main__":
    main()