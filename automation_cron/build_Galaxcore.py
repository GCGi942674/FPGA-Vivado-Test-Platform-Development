#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_GalaxCore.py

Python refactor of build_GalaxCore.cpp.

Main workflow:
1. Poll SVN HEAD revision.
2. Continue from last_version.
3. For each new revision:
   - svn revert / cleanup
   - svn update -r <rev>
   - run mk
   - if mk fails, run make clean and retry mk once
   - zip GalaxCore binary after success
   - keep only latest MAX_BIN_KEEP zip files
4. State files:
   - No history file is used anymore.
   - last_version is kept for resume after interruption.
   - mk_fail is kept and written in reverse order:
       line 1: latest SUCCESS revision
       line 2+: latest FAIL revisions, newest first
     Each record includes author/name information.
"""

import os
import re
import sys
import time
import signal
import zipfile
import subprocess
from pathlib import Path
from datetime import datetime


# ================= CONFIG =================

WORK_DIR = Path(os.environ.get(
    "GALAXCORE_WORK_DIR",
    "/home/user3/workspace/galaxcore",
)).expanduser()

BIN_SRC = Path(os.environ.get(
    "GALAXCORE_BIN_SRC",
    str(WORK_DIR / "bin" / "Linux_64" / "GalaxCore"),
)).expanduser()

# 注意：原 C++ 里是 Galaxcore_bin；这里按你现在说的 GalaxCore_bin。
# 如果你机器上实际目录仍然是 Galaxcore_bin，可以设置环境变量 GALAXCORE_BIN_DST 覆盖。
BIN_DST = Path(os.environ.get(
    "GALAXCORE_BIN_DST",
    "/home/xiaonan/Share/zw_cache/GalaxCore_bin",
)).expanduser()

ZIP_DIR = Path(os.environ.get(
    "GALAXCORE_ZIP_DIR",
    str(BIN_DST / "zip"),
)).expanduser()

MK_FAIL_FILE = Path(os.environ.get(
    "GALAXCORE_MK_FAIL_FILE",
    str(BIN_DST / "mk_fail"),
)).expanduser()

# 新文件名：last_version
LAST_VERSION_FILE = Path(os.environ.get(
    "GALAXCORE_LAST_VERSION_FILE",
    str(BIN_DST / "last_version"),
)).expanduser()

# 兼容旧 C++ 文件名：last_revision.txt
LEGACY_LAST_REVISION_FILE = BIN_DST / "last_revision.txt"

SVN_URL = os.environ.get(
    "GALAXCORE_SVN_URL",
    "http://192.168.10.10/svn/galaxcore/galaxcore",
)

MAX_BIN_KEEP = int(os.environ.get("GALAXCORE_MAX_BIN_KEEP", "150"))
ZIP_PREFIX = os.environ.get("GALAXCORE_ZIP_PREFIX", "GalaxCore")
POLL_INTERVAL = int(os.environ.get("GALAXCORE_POLL_INTERVAL", "2"))
IDLE_SLEEP = int(os.environ.get("GALAXCORE_IDLE_SLEEP", "1"))
QUIET_CMD_OUTPUT = os.environ.get("GALAXCORE_QUIET", "1") != "0"
VERBOSE_OUTPUT = os.environ.get("GALAXCORE_VERBOSE", "0") == "1"

running = True


# ================= BASIC UTILS =================

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ci_log(message):
    # Keep terminal output compact by default.
    print(f"[CI] {message}", flush=True)


def ci_debug(message):
    # Enable detailed path/debug output only when needed:
    #   GALAXCORE_VERBOSE=1 ./build_GalaxCore.py
    if VERBOSE_OUTPUT:
        print(f"[CI] {message}", flush=True)


def signal_handler(signum, frame):
    global running
    running = False
    print("\n[CI] stopping safely...", flush=True)


def ensure_parent(path):
    path.parent.mkdir(parents=True, exist_ok=True)


def atomic_write(path, content):
    ensure_parent(path)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def read_nonempty_lines(path):
    if not path.exists():
        return []
    try:
        return [line.rstrip("\n") for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception:
        return []


def write_lines(path, lines):
    content = "\n".join(lines)
    if content:
        content += "\n"
    atomic_write(path, content)


# ================= COMMAND EXEC =================

def run_cmd(
    cmd,
    cwd=None,
    shell=False,
    quiet=None,
):
    """Run command and return (ok, return_code)."""
    if quiet is None:
        quiet = QUIET_CMD_OUTPUT

    stdout = subprocess.DEVNULL if quiet else None
    stderr = subprocess.DEVNULL if quiet else None

    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            shell=shell,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
        return p.returncode == 0, p.returncode
    except Exception:
        return False, -1


def run_in_workdir(cmd, shell=False):
    ok, _ = run_cmd(cmd, cwd=WORK_DIR, shell=shell)
    return ok


def read_cmd_output(cmd):
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            check=False,
        )
        if p.returncode != 0:
            return None
        return p.stdout.strip()
    except Exception:
        return None


# ================= SVN =================

def get_head():
    output = read_cmd_output([
        "svn", "info", SVN_URL,
        "--show-item", "revision",
    ])
    if not output:
        return -1
    try:
        return int(output.strip())
    except ValueError:
        return -1


def get_author(rev):
    output = read_cmd_output([
        "svn", "info", "-r", str(rev), SVN_URL,
        "--show-item", "last-changed-author",
    ])
    if not output:
        return "unknown"
    return output.strip() or "unknown"


def svn_clean():
    run_in_workdir(["svn", "revert", "-R", "."])
    run_in_workdir(["svn", "cleanup"])


def checkout(rev):
    return run_in_workdir(["svn", "update", "-r", str(rev)])


# ================= CHECKPOINT / LAST_VERSION =================

def _read_int_file(path):
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return None
        return int(text)
    except Exception:
        return None


def load_last_version():
    """
    Load last finished/interrupted revision.

    Priority:
    1. New file: last_version
    2. Legacy file: last_revision.txt
    3. If neither exists, start from HEAD - 1, same as original C++ logic.
    """
    value = _read_int_file(LAST_VERSION_FILE)
    if value is not None:
        return value

    value = _read_int_file(LEGACY_LAST_REVISION_FILE)
    if value is not None:
        return value

    head = get_head()
    return head - 1 if head > 0 else 0


def save_last_version(version):
    atomic_write(LAST_VERSION_FILE, f"{version}\n")


# ================= BUILD =================

def build():
    # Preserve original C++ behavior:
    # csh -c 'mk; exit $status'
    return run_in_workdir(["csh", "-c", "mk; exit $status"])


def make_clean():
    run_in_workdir(["make", "clean"])


# ================= ZIP =================

def zip_name_for_rev(rev):
    return f"{ZIP_PREFIX}_{rev}.zip"


def compress_to_zip(rev):
    if not BIN_SRC.exists():
        ci_log("FAIL binary not found")
        ci_debug(f"binary not found: {BIN_SRC}")
        return False

    ZIP_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = ZIP_DIR / zip_name_for_rev(rev)

    tmp_zip_path = zip_path.with_name(zip_path.name + ".tmp")
    try:
        if tmp_zip_path.exists():
            tmp_zip_path.unlink()

        with zipfile.ZipFile(tmp_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Equivalent to `zip -j`: only store basename, not full directory path.
            zf.write(BIN_SRC, arcname=BIN_SRC.name)

        os.replace(tmp_zip_path, zip_path)
        return True
    except Exception as e:
        ci_debug(f"compress failed: {e}")
        try:
            if tmp_zip_path.exists():
                tmp_zip_path.unlink()
        except Exception:
            pass
        return False


def _revision_from_zip_name(path):
    m = re.search(r"_(\d+)\.zip$", path.name)
    return int(m.group(1)) if m else -1


def clean_old_zips():
    if MAX_BIN_KEEP <= 0 or not ZIP_DIR.exists():
        return

    zip_files = list(ZIP_DIR.glob(f"{ZIP_PREFIX}_*.zip"))
    zip_files.sort(key=lambda p: (_revision_from_zip_name(p), p.stat().st_mtime))

    remove_count = len(zip_files) - MAX_BIN_KEEP
    if remove_count <= 0:
        return

    for path in zip_files[:remove_count]:
        try:
            path.unlink()
        except Exception as e:
            ci_debug(f"warning: failed to remove old zip {path}: {e}")


# ================= MK_FAIL STATE FILE =================

# mk_fail display format, newest records at the top:
# Success  r14773  author_name     [2026-06-01 16:00:57]
# FAIL     r14775  author_name     [2026-06-01 17:20:57]
# FAIL     r14774  author_name     [2026-06-01 16:20:57]

STATUS_WIDTH = 7
REV_WIDTH = 8
AUTHOR_WIDTH = int(os.environ.get("GALAXCORE_MK_FAIL_AUTHOR_WIDTH", "16"))


def make_record(
    status,
    rev,
    author,
    zip_name,
    reason="",
):
    """
    Create one aligned mk_fail record.

    Required format:
        Success  r14773  author_name     [2026-06-01 16:00:57]
        FAIL     r14775  author_name     [2026-06-01 17:20:57]

    Notes:
    - `zip_name` is kept in the function argument for compatibility with the
      previous workflow, but it is not printed in mk_fail now.
    - `reason` is also kept for compatibility, but mk_fail stays concise.
    """
    del zip_name
    del reason

    safe_author = str(author).strip() or "unknown"

    # User-facing status text: Success on success, FAIL on failure.
    if status.upper() == "SUCCESS":
        status_text = "Success"
    else:
        status_text = "FAIL"

    rev_text = "r{}".format(rev)

    return "{:<{sw}}  {:<{rw}}  {:<{aw}}  [{}]".format(
        status_text,
        rev_text,
        safe_author,
        now(),
        sw=STATUS_WIDTH,
        rw=REV_WIDTH,
        aw=AUTHOR_WIDTH,
    ).rstrip()


def line_status(line):
    stripped = line.strip()
    upper = stripped.upper()

    if upper.startswith("SUCCESS ") or upper.startswith("SUCCESS\t"):
        return "SUCCESS"
    if upper.startswith("SUCCESS"):
        return "SUCCESS"
    if upper.startswith("FAIL ") or upper.startswith("FAIL\t"):
        return "FAIL"
    if upper.startswith("FAIL"):
        return "FAIL"

    # Compatible with old C++ format: [time] FAIL r123
    if " FAIL " in " {} ".format(upper):
        return "FAIL"

    return "OTHER"


def line_revision(line):
    m = re.search(r"\br(\d+)\b", line)
    if m:
        return int(m.group(1))
    m = re.search(r"\bversion=(\d+)\b", line)
    if m:
        return int(m.group(1))
    return None


def update_mk_fail_success(rev, author, zip_name):
    """
    Write latest success as line 1.
    Keep failure records below it, newest first.
    If this revision existed in fail records, remove it.
    """
    old_lines = read_nonempty_lines(MK_FAIL_FILE)

    fail_lines = []
    for line in old_lines:
        if line_status(line) == "FAIL" and line_revision(line) != rev:
            fail_lines.append(line)

    success_line = make_record("SUCCESS", rev, author, zip_name)
    write_lines(MK_FAIL_FILE, [success_line] + fail_lines)


def update_mk_fail_failure(rev, author, zip_name, reason):
    """
    Keep latest success at line 1.
    Insert latest failure at line 2.
    Failure list is newest first.
    Duplicate fail records for same revision are removed.
    """
    old_lines = read_nonempty_lines(MK_FAIL_FILE)

    success_line = None
    fail_lines = []

    for line in old_lines:
        status = line_status(line)
        if status == "SUCCESS" and success_line is None:
            success_line = line
        elif status == "FAIL" and line_revision(line) != rev:
            fail_lines.append(line)

    new_fail_line = make_record("FAIL", rev, author, zip_name, reason)

    if success_line:
        new_lines = [success_line, new_fail_line] + fail_lines
    else:
        # Before first success exists, latest failure temporarily stays at line 1.
        new_lines = [new_fail_line] + fail_lines

    write_lines(MK_FAIL_FILE, new_lines)


# ================= BUILD FLOW =================

def record_failure(rev, author, reason):
    zip_name = zip_name_for_rev(rev)
    update_mk_fail_failure(rev, author, zip_name, reason)
    save_last_version(rev)


def record_success(rev, author):
    zip_name = zip_name_for_rev(rev)
    update_mk_fail_success(rev, author, zip_name)
    save_last_version(rev)


def build_revision(rev):
    ci_log(f"build r{rev}")

    author = get_author(rev)
    ci_debug(f"r{rev} author={author}")

    svn_clean()

    # 1. checkout/update target revision
    if not checkout(rev):
        ci_log(f"FAIL r{rev}")
        ci_debug(f"svn update failed for r{rev}")
        record_failure(rev, author, "svn_update_failed")
        return False

    # 2. first build attempt
    if build():
        if not compress_to_zip(rev):
            ci_log(f"FAIL r{rev}")
            ci_debug(f"compress failed for r{rev}")
            record_failure(rev, author, "compress_failed")
            return False

        clean_old_zips()
        record_success(rev, author)
        ci_log(f"Success r{rev}")
        return True

    # 3. retry after make clean
    ci_debug(f"r{rev} first build failed, retry after make clean")
    make_clean()

    if build():
        if not compress_to_zip(rev):
            ci_log(f"FAIL r{rev}")
            ci_debug(f"compress failed for r{rev}")
            record_failure(rev, author, "compress_failed_after_retry")
            return False

        clean_old_zips()
        record_success(rev, author)
        ci_log(f"Success r{rev}")
        return True

    # 4. failed after retry
    ci_log(f"FAIL r{rev}")
    record_failure(rev, author, "build_failed_after_retry")
    return False


# ================= MAIN LOOP =================

def print_startup():
    ci_log("SVN build watcher started")
    ci_debug(f"WORK_DIR={WORK_DIR}")
    ci_debug(f"BIN_SRC={BIN_SRC}")
    ci_debug(f"BIN_DST={BIN_DST}")
    ci_debug(f"ZIP_DIR={ZIP_DIR}")
    ci_debug(f"MK_FAIL_FILE={MK_FAIL_FILE}")
    ci_debug(f"LAST_VERSION_FILE={LAST_VERSION_FILE}")
    ci_debug(f"Starting version: r{load_last_version()}")


def validate_paths():
    BIN_DST.mkdir(parents=True, exist_ok=True)
    ZIP_DIR.mkdir(parents=True, exist_ok=True)

    if not WORK_DIR.exists():
        ci_debug(f"warning: WORK_DIR does not exist: {WORK_DIR}")


def main():
    global running

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    validate_paths()
    print_startup()

    while running:
        head = get_head()
        local = load_last_version()

        if head < 0:
            ci_log("failed to get SVN HEAD, retry later")
            time.sleep(10)
            continue

        if head <= local:
            wait_start = time.time()
            while running:
                current_head = get_head()
                if current_head > local:
                    break

                elapsed = int(time.time() - wait_start)
                print(
                    f"\r[CI] Idle | local={local} head={current_head} wait: {elapsed}s",
                    end="",
                    flush=True,
                )
                time.sleep(IDLE_SLEEP)
            print(flush=True)
            continue

        ci_log(f"update detected {local} -> {head}")

        for rev in range(local + 1, head + 1):
            if not running:
                break
            build_revision(rev)

        time.sleep(POLL_INTERVAL)

    ci_log("exit")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        running = False
        ci_log("exit by KeyboardInterrupt")
        sys.exit(0)
