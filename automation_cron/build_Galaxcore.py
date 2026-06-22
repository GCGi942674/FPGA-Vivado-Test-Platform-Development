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

import argparse
import os
import re
import shutil
import sys
import time
import signal
import zipfile
import subprocess
import xml.etree.ElementTree as ET
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

SUBMIT_TEST_DIR = Path(os.environ.get(
    "GALAXCORE_SUBMIT_TEST_DIR",
    str(WORK_DIR / "test2"),
)).expanduser()

SUBMIT_TEST_SCRIPT = os.environ.get(
    "GALAXCORE_SUBMIT_TEST_SCRIPT",
    "./submit_test.sh",
)

SUBMIT_SUCCESS_MARKER = os.environ.get(
    "GALAXCORE_SUBMIT_SUCCESS_MARKER",
    "No Case Fail, You can submit your code now~",
)

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


def strip_ansi(text):
    """Remove terminal color/control sequences from command output."""
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text or "")


def run_cmd_capture(cmd, cwd=None, shell=False):
    """Run command and return (ok, return_code, output)."""
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            check=False,
        )
        output = strip_ansi(p.stdout or "")
        return p.returncode == 0, p.returncode, output
    except Exception as e:
        return False, -1, str(e)


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

def run_csh_command(command):
    """Run one command in the configured csh build environment."""
    return run_cmd(
        ["csh", "-c", "{}; exit $status".format(command)],
        cwd=WORK_DIR,
    )


def build():
    """Run the normal build command, equivalent to the interactive `mk` alias."""
    ok, _ = run_csh_command("mk")
    return ok


def rebuild_after_failure():
    """Run the full recovery flow after the first build failure.

    Recovery flow:
        make clean
        cmake .
        bd
        mk
    """
    steps = [
        ("make_clean_failed", ["make", "clean"], False),
        ("cmake_failed", "cmake .", True),
        ("build_prepare_failed", "bd", True),
        ("mk_failed_after_retry", "mk", True),
    ]

    for reason, command, use_csh in steps:
        if use_csh:
            ok, return_code = run_csh_command(command)
        else:
            ok, return_code = run_cmd(command, cwd=WORK_DIR)

        if not ok:
            return False, reason, return_code

    return True, "ok", 0


def run_submit_test():
    """Run test2/submit_test.sh and verify its success marker."""
    if not SUBMIT_TEST_DIR.exists():
        return False, "submit_test_dir_missing", f"missing: {SUBMIT_TEST_DIR}"

    ok, return_code, output = run_cmd_capture(
        ["csh", "-c", f"{SUBMIT_TEST_SCRIPT}; exit $status"],
        cwd=SUBMIT_TEST_DIR,
    )

    if SUBMIT_SUCCESS_MARKER in output:
        return True, "submit_success", output

    if ok:
        reason = "submit_output_check_failed"
    else:
        reason = f"submit_exit_{return_code}"

    return False, reason, output


def summarize_submit_output(output):
    """Keep the most useful submit_test.sh failure lines for mk_fail."""
    lines = []
    for raw_line in strip_ansi(output).splitlines():
        line = raw_line.strip()
        if line:
            lines.append(line)

    if not lines:
        return "no submit output"

    interesting = []
    patterns = ["fail", "error", "elapsed time", "case"]
    for line in lines:
        lower = line.lower()
        if any(pattern in lower for pattern in patterns):
            interesting.append(line)

    selected = interesting[-3:] if interesting else lines[-3:]
    return " | ".join(selected)[:240]


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
# Success  r14773  author_name     ok                    [2026-06-01 16:00:57]
# FAIL     r14775  author_name     mk_failed             [2026-06-01 17:20:57]
# FAIL     r14774  author_name     submit_failed: ...    [2026-06-01 16:20:57]

STATUS_WIDTH = 7
REV_WIDTH = 8
AUTHOR_WIDTH = int(os.environ.get("GALAXCORE_MK_FAIL_AUTHOR_WIDTH", "16"))
REASON_WIDTH = int(os.environ.get("GALAXCORE_MK_FAIL_REASON_WIDTH", "28"))


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
    """
    del zip_name

    safe_author = str(author).strip() or "unknown"
    safe_reason = str(reason).strip() or "ok"

    # User-facing status text: Success on success, FAIL on failure.
    if status.upper() == "SUCCESS":
        status_text = "Success"
    else:
        status_text = "FAIL"

    rev_text = "r{}".format(rev)

    return "{:<{sw}}  {:<{rw}}  {:<{aw}}  {:<{mw}}  [{}]".format(
        status_text,
        rev_text,
        safe_author,
        safe_reason,
        now(),
        sw=STATUS_WIDTH,
        rw=REV_WIDTH,
        aw=AUTHOR_WIDTH,
        mw=REASON_WIDTH,
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

    success_line = make_record("SUCCESS", rev, author, zip_name, "ok")
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

    # 2. First build attempt: mk.
    mk_ok = build()

    # 3. Recovery after the first build failure:
    #    make clean -> cmake . -> bd -> mk
    if not mk_ok:
        ci_log(
            f"r{rev} first mk failed; "
            "run make clean -> cmake . -> bd -> mk"
        )
        recovered, failure_reason, failure_code = rebuild_after_failure()

        if not recovered:
            ci_log(
                f"FAIL r{rev} {failure_reason} "
                f"rc={failure_code}"
            )
            record_failure(rev, author, failure_reason)
            return False

    # 4. Submit gate. Only generate zip when submit_test.sh really passes.
    ci_log(f"submit_test r{rev}")
    submit_ok, submit_reason, submit_output = run_submit_test()
    if not submit_ok:
        detail = summarize_submit_output(submit_output)
        ci_log(f"FAIL r{rev} submit_failed")
        ci_debug(f"submit failed for r{rev}: {submit_reason}: {detail}")
        record_failure(rev, author, "submit_failed")
        return False

    # 5. Create the zip only after mk and submit both succeed.
    if not compress_to_zip(rev):
        ci_log(f"FAIL r{rev} compress_failed")
        ci_debug(f"compress failed for r{rev}")
        record_failure(rev, author, "compress_failed")
        return False

    clean_old_zips()
    record_success(rev, author)
    ci_log(f"Success r{rev}")
    return True



# ================= CONFIG CHECK =================

class CheckReporter(object):
    """Collect and print configuration check results."""

    def __init__(self):
        self.counts = {
            "OK": 0,
            "WARN": 0,
            "FAIL": 0,
            "INFO": 0,
        }

    def emit(self, level, label, detail):
        """Print one aligned check result."""
        self.counts[level] += 1
        print("{:<5} {:<24} {}".format(level, label, detail))

    def ok(self, label, detail):
        self.emit("OK", label, detail)

    def warn(self, label, detail):
        self.emit("WARN", label, detail)

    def fail(self, label, detail):
        self.emit("FAIL", label, detail)

    def info(self, label, detail):
        self.emit("INFO", label, detail)

    def finish(self):
        """Print the final summary and return the process exit code."""
        print()
        print(
            "CHECK_SUMMARY ok={OK} warn={WARN} fail={FAIL} info={INFO}".format(
                **self.counts
            )
        )
        return 1 if self.counts["FAIL"] else 0


def normalize_path(path):
    """Return a normalized absolute path without requiring it to exist."""
    try:
        return Path(path).expanduser().resolve()
    except Exception:
        return Path(os.path.abspath(os.path.expanduser(str(path))))


def nearest_existing_parent(path):
    """Return the nearest existing parent directory."""
    current = normalize_path(path)

    while not current.exists() and current != current.parent:
        current = current.parent

    return current


def check_writable_target(reporter, label, path):
    """Check whether a target path can be created or updated."""
    path = normalize_path(path)

    if path.exists():
        parent = path if path.is_dir() else path.parent
    else:
        parent = nearest_existing_parent(path)

    if not parent.exists():
        reporter.fail(label, "no existing parent for {}".format(path))
        return

    if not parent.is_dir():
        reporter.fail(label, "parent is not a directory: {}".format(parent))
        return

    if os.access(str(parent), os.W_OK | os.X_OK):
        reporter.ok(label, "writable via {}".format(parent))
    else:
        reporter.fail(label, "not writable: {}".format(parent))


def run_check_command(argv, cwd=None, timeout=20):
    """Run one read-only check command and return (return_code, output)."""
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, strip_ansi(proc.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "command timed out after {} seconds".format(timeout)
    except Exception as exc:
        return -1, str(exc)


def parse_svn_info_xml(output):
    """Parse an `svn info --xml` response."""
    root = ET.fromstring(output)
    entry = root.find(".//entry")

    if entry is None:
        raise ValueError("missing SVN entry")

    url_node = entry.find("url")
    return {
        "revision": (entry.get("revision") or "").strip(),
        "url": (url_node.text or "").strip() if url_node is not None else "",
    }


def check_csh_command(reporter, command_name):
    """Check one command or alias inside csh."""
    csh_path = shutil.which("csh")
    if not csh_path:
        reporter.fail("{} in csh".format(command_name), "csh not found")
        return

    rc, output = run_check_command(
        [csh_path, "-c", "which {}".format(command_name)],
        cwd=WORK_DIR,
        timeout=10,
    )

    if rc == 0 and output:
        reporter.ok("{} in csh".format(command_name), output.replace("\n", " | "))
    else:
        reporter.fail(
            "{} in csh".format(command_name),
            "not available rc={}: {}".format(rc, output or "no output"),
        )


def check_svn_configuration(reporter):
    """Check the local working copy and configured remote repository."""
    svn_path = shutil.which("svn")
    if not svn_path:
        reporter.fail("svn command", "svn not found in PATH")
        return

    reporter.ok("svn command", svn_path)

    rc, output = run_check_command(
        [svn_path, "info", "--xml", str(WORK_DIR)],
        timeout=20,
    )
    if rc != 0:
        reporter.fail(
            "working copy",
            "svn info failed rc={}: {}".format(rc, output or "no output"),
        )
        return

    try:
        local_info = parse_svn_info_xml(output)
    except Exception as exc:
        reporter.fail("working copy", "cannot parse svn info: {}".format(exc))
        return

    reporter.ok(
        "working copy",
        "revision={} url={}".format(
            local_info["revision"] or "?",
            local_info["url"] or "?",
        ),
    )

    local_url = local_info["url"].rstrip("/")
    expected_url = str(SVN_URL).rstrip("/")

    if local_url == expected_url:
        reporter.ok("SVN URL match", expected_url)
    else:
        reporter.fail(
            "SVN URL match",
            "configured={} working_copy={}".format(expected_url, local_url),
        )

    rc, output = run_check_command(
        [svn_path, "info", "--xml", str(SVN_URL)],
        timeout=20,
    )
    if rc != 0:
        reporter.fail(
            "remote SVN",
            "svn info failed rc={}: {}".format(rc, output or "no output"),
        )
        return

    try:
        remote_info = parse_svn_info_xml(output)
        reporter.ok(
            "remote SVN",
            "revision={} url={}".format(
                remote_info["revision"] or "?",
                remote_info["url"] or "?",
            ),
        )
    except Exception as exc:
        reporter.fail("remote SVN", "cannot parse svn info: {}".format(exc))


def run_config_check():
    """Run read-only configuration checks without changing the working copy."""
    reporter = CheckReporter()

    script_path = normalize_path(__file__)
    script_dir = script_path.parent
    current_dir = normalize_path(Path.cwd())
    work_dir = normalize_path(WORK_DIR)
    bin_src = normalize_path(BIN_SRC)
    bin_dst = normalize_path(BIN_DST)
    zip_dir = normalize_path(ZIP_DIR)
    submit_dir = normalize_path(SUBMIT_TEST_DIR)

    submit_script_value = Path(SUBMIT_TEST_SCRIPT)
    if submit_script_value.is_absolute():
        submit_script = normalize_path(submit_script_value)
    else:
        submit_script = normalize_path(submit_dir / submit_script_value)

    reporter.info("script file", str(script_path))
    reporter.info("script directory", str(script_dir))
    reporter.info("current directory", str(current_dir))
    reporter.info("WORK_DIR", str(work_dir))
    reporter.info(
        "WORK_DIR source",
        "environment"
        if "GALAXCORE_WORK_DIR" in os.environ
        else "built-in default",
    )
    reporter.info("BIN_SRC", str(bin_src))
    reporter.info("BIN_DST", str(bin_dst))
    reporter.info("ZIP_DIR", str(zip_dir))
    reporter.info("SUBMIT_TEST_DIR", str(submit_dir))
    reporter.info("SVN_URL", str(SVN_URL))

    if work_dir.is_dir():
        reporter.ok("WORK_DIR exists", str(work_dir))
    elif work_dir.exists():
        reporter.fail("WORK_DIR exists", "not a directory: {}".format(work_dir))
    else:
        reporter.fail("WORK_DIR exists", "missing: {}".format(work_dir))

    if current_dir == work_dir:
        reporter.ok("current vs WORK_DIR", "same path")
    else:
        reporter.warn(
            "current vs WORK_DIR",
            "current={} configured={}".format(current_dir, work_dir),
        )

    if script_dir == work_dir:
        reporter.ok("script vs WORK_DIR", "same path")
    else:
        reporter.warn(
            "script vs WORK_DIR",
            "script={} configured={}".format(script_dir, work_dir),
        )

    if work_dir.is_dir():
        if os.access(str(work_dir), os.R_OK | os.W_OK | os.X_OK):
            reporter.ok("WORK_DIR access", "read/write/execute")
        else:
            reporter.fail("WORK_DIR access", "insufficient permissions")
    else:
        reporter.fail("WORK_DIR access", "cannot check missing directory")

    svn_dir = work_dir / ".svn"
    if svn_dir.exists():
        reporter.ok("WORK_DIR .svn", str(svn_dir))
    else:
        reporter.fail("WORK_DIR .svn", "missing: {}".format(svn_dir))

    makefiles = [
        work_dir / "Makefile",
        work_dir / "makefile",
        work_dir / "GNUmakefile",
    ]
    existing_makefiles = [path for path in makefiles if path.is_file()]

    if existing_makefiles:
        reporter.ok(
            "build Makefile",
            ", ".join(str(path) for path in existing_makefiles),
        )
    else:
        reporter.fail(
            "build Makefile",
            "none found in {}".format(work_dir),
        )

    for command_name in ("mk", "bd", "cmake"):
        check_csh_command(reporter, command_name)

    make_path = shutil.which("make")
    if make_path:
        reporter.ok("make command", make_path)
    else:
        reporter.fail("make command", "make not found in PATH")

    check_svn_configuration(reporter)

    if bin_src.is_file():
        if os.access(str(bin_src), os.X_OK):
            reporter.ok("GalaxCore binary", "{} (executable)".format(bin_src))
        else:
            reporter.warn(
                "GalaxCore binary",
                "{} exists but is not executable".format(bin_src),
            )
    else:
        reporter.warn(
            "GalaxCore binary",
            "not present before build: {}".format(bin_src),
        )

    if bin_src.parent.is_dir():
        reporter.ok("binary directory", str(bin_src.parent))
    else:
        reporter.warn(
            "binary directory",
            "missing before build: {}".format(bin_src.parent),
        )

    if submit_dir.is_dir():
        reporter.ok("submit test dir", str(submit_dir))
    else:
        reporter.fail("submit test dir", "missing: {}".format(submit_dir))

    if submit_script.is_file():
        if os.access(str(submit_script), os.X_OK):
            reporter.ok("submit script", "{} (executable)".format(submit_script))
        else:
            reporter.fail(
                "submit script",
                "not executable: {}".format(submit_script),
            )
    else:
        reporter.fail("submit script", "missing: {}".format(submit_script))

    check_writable_target(reporter, "BIN_DST writable", bin_dst)
    check_writable_target(reporter, "ZIP_DIR writable", zip_dir)
    check_writable_target(reporter, "mk_fail writable", MK_FAIL_FILE)
    check_writable_target(reporter, "last_version writable", LAST_VERSION_FILE)

    last_version = _read_int_file(LAST_VERSION_FILE)
    if last_version is not None:
        reporter.ok("last_version", "r{}".format(last_version))
    elif LAST_VERSION_FILE.exists():
        reporter.fail(
            "last_version",
            "invalid integer: {}".format(LAST_VERSION_FILE),
        )
    else:
        reporter.warn(
            "last_version",
            "missing; startup will use legacy file or HEAD - 1",
        )

    legacy_version = _read_int_file(LEGACY_LAST_REVISION_FILE)
    if legacy_version is not None:
        reporter.info("legacy version", "r{}".format(legacy_version))

    reporter.info(
        "failure recovery",
        "make clean -> cmake . -> bd -> mk",
    )

    return reporter.finish()

# ================= MAIN LOOP =================

def print_startup():
    ci_log("SVN build watcher started")
    ci_debug(f"WORK_DIR={WORK_DIR}")
    ci_debug(f"BIN_SRC={BIN_SRC}")
    ci_debug(f"BIN_DST={BIN_DST}")
    ci_debug(f"ZIP_DIR={ZIP_DIR}")
    ci_debug(f"SUBMIT_TEST_DIR={SUBMIT_TEST_DIR}")
    ci_debug(f"SUBMIT_TEST_SCRIPT={SUBMIT_TEST_SCRIPT}")
    ci_debug(f"MK_FAIL_FILE={MK_FAIL_FILE}")
    ci_debug(f"LAST_VERSION_FILE={LAST_VERSION_FILE}")
    ci_debug(f"Starting version: r{load_last_version()}")


def validate_paths():
    BIN_DST.mkdir(parents=True, exist_ok=True)
    ZIP_DIR.mkdir(parents=True, exist_ok=True)

    if not WORK_DIR.exists():
        ci_debug(f"warning: WORK_DIR does not exist: {WORK_DIR}")



def build_parser():
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description="GalaxCore SVN build watcher",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "check paths, csh commands, SVN, permissions, and build inputs "
            "without changing files"
        ),
    )
    return parser


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
    args = build_parser().parse_args()

    try:
        if args.check:
            sys.exit(run_config_check())
        sys.exit(main())
    except KeyboardInterrupt:
        running = False
        ci_log("exit by KeyboardInterrupt")
        sys.exit(0)
