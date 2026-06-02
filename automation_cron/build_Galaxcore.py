#!/usr/bin/env python3
"""
build_GalaxCore.py

Python rewrite of build_Galaxcore.cpp.

Main work:
  1. Continuously monitor SVN HEAD revision.
  2. For every new revision, svn clean + svn update -r REV.
  3. Build GalaxCore with csh mk.
  4. If first build fails, run make clean and build once again.
  5. Zip bin/Linux_64/GalaxCore into shared zip directory.
  6. Keep only latest MAX_BIN_KEEP zip packages.
  7. Record build history, failure log, and checkpoint.
"""

from __future__ import annotations

import glob
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


# ================= CONFIG =================

WORK_DIR = Path("/home/user3/workspace/galaxcore")

BIN_SRC = Path("/home/user3/workspace/galaxcore/bin/Linux_64/GalaxCore")

BIN_DST = Path("/home/xiaonan/Share/zw_cache/Galaxcore_bin")

TAR_DIR = BIN_DST / "zip"

HISTORY_LOG = BIN_DST / "build_history.log"

FAIL_LOG = BIN_DST / "mk_fail"

CHECKPOINT_FILE = BIN_DST / "last_revision.txt"

SVN_URL = "http://192.168.10.10/svn/galaxcore/galaxcore"

MAX_BIN_KEEP = 150

running = True


# ================= SIGNAL =================

def signal_handler(signum, frame) -> None:
    global running
    running = False
    print("\n[CI] stopping safely...", flush=True)


# ================= TIME =================

def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


# ================= CORE EXEC =================

def run_cmd(
    cmd: list[str],
    cwd: Optional[Path] = None,
    timeout: Optional[int] = None,
    quiet: bool = True,
) -> bool:
    """Run a command and return True only when exit code is 0."""
    try:
        stdout = subprocess.DEVNULL if quiet else None
        stderr = subprocess.DEVNULL if quiet else None

        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=stdout,
            stderr=stderr,
            timeout=timeout,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def run_capture(
    cmd: list[str],
    cwd: Optional[Path] = None,
    timeout: Optional[int] = None,
) -> Optional[str]:
    """Run a command and return stripped stdout. Return None on failure."""
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            return None

        return result.stdout.strip()
    except Exception:
        return None


def run_in_workdir(cmd: list[str], timeout: Optional[int] = None) -> bool:
    """Equivalent to C++: cd WORK_DIR && cmd > /dev/null 2>&1."""
    return run_cmd(cmd, cwd=WORK_DIR, timeout=timeout, quiet=True)


# ================= SVN =================

def get_head() -> int:
    out = run_capture(
        ["svn", "info", SVN_URL, "--show-item", "revision"],
        timeout=30,
    )

    if not out:
        return -1

    try:
        return int(out)
    except ValueError:
        return -1


def get_author(rev: int) -> str:
    out = run_capture(
        [
            "svn",
            "info",
            "-r",
            str(rev),
            SVN_URL,
            "--show-item",
            "last-changed-author",
        ],
        timeout=30,
    )

    return out if out else "unknown"


# ================= CHECKPOINT =================

def load_checkpoint() -> int:
    try:
        text = CHECKPOINT_FILE.read_text(encoding="utf-8").strip()
        return int(text)
    except Exception:
        head = get_head()
        return head - 1 if head > 0 else 0


def save_checkpoint(rev: int) -> None:
    BIN_DST.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(str(rev), encoding="utf-8")


# ================= SVN CLEAN =================

def svn_clean() -> None:
    run_in_workdir(["svn", "revert", "-R", "."])
    run_in_workdir(["svn", "cleanup"])


# ================= BUILD =================

def build() -> bool:
    # Same intention as C++: csh -c 'mk; exit $status'
    return run_in_workdir(["csh", "-c", "mk; exit $status"])


def make_clean() -> None:
    run_in_workdir(["make", "clean"])


# ================= CHECKOUT =================

def checkout(rev: int) -> bool:
    return run_in_workdir(["svn", "update", "-r", str(rev)])


# ================= ZIP =================

def compress_to_tar(rev: int) -> bool:
    TAR_DIR.mkdir(parents=True, exist_ok=True)

    if not BIN_SRC.is_file():
        return False

    zipfile = TAR_DIR / f"Galaxcore_{rev}.zip"
    return run_cmd(["zip", "-j", str(zipfile), str(BIN_SRC)], quiet=True)


def _archive_revision(path: str) -> int:
    name = os.path.basename(path)
    prefix = "Galaxcore_"
    suffix = ".zip"

    if not (name.startswith(prefix) and name.endswith(suffix)):
        return -1

    rev_text = name[len(prefix) : -len(suffix)]

    return int(rev_text) if rev_text.isdigit() else -1


def clean_old_tars() -> None:
    TAR_DIR.mkdir(parents=True, exist_ok=True)

    files = glob.glob(str(TAR_DIR / "Galaxcore_*.zip"))
    archives = []

    for path in files:
        rev = _archive_revision(path)
        if rev >= 0:
            archives.append((rev, path))

    if len(archives) <= MAX_BIN_KEEP:
        return

    archives.sort(key=lambda item: item[0])
    delete_count = len(archives) - MAX_BIN_KEEP

    for _, path in archives[:delete_count]:
        try:
            os.remove(path)
        except OSError:
            pass


# ================= LOGGING =================

def init_log() -> None:
    BIN_DST.mkdir(parents=True, exist_ok=True)

    with HISTORY_LOG.open("a", encoding="utf-8") as f:
        f.write("============================================================\n")
        f.write(f"Starting monitor time: {now()}\n")
        f.write(f"Starting version: r{load_checkpoint()}\n")
        f.write("============================================================\n")
        f.write("Revision\tAuthor\t\tTime\t\t\tResult\n")
        f.write("------------------------------------------------------------\n")


def log_row(rev: int, author: str, log_time: str, result: str) -> None:
    BIN_DST.mkdir(parents=True, exist_ok=True)

    with HISTORY_LOG.open("a", encoding="utf-8") as f:
        f.write(f"r{rev} | {author} | {log_time} | {result}\n")
        f.write("------------------------------------------------------------\n")


def log_fail(rev: int) -> None:
    BIN_DST.mkdir(parents=True, exist_ok=True)

    with FAIL_LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{now()}] FAIL r{rev}\n")


# ================= BUILD FLOW =================

def build_revision(rev: int) -> None:
    print(f"[CI] build r{rev}", flush=True)

    author = get_author(rev)

    svn_clean()

    # 1. checkout
    if not checkout(rev):
        print(f"[CI] svn update failed for r{rev}", file=sys.stderr, flush=True)
        log_row(rev, author, now(), "failed (svn update)")
        log_fail(rev)
        save_checkpoint(rev)
        return

    # 2. first build attempt
    if build():
        if not compress_to_tar(rev):
            print(f"[CI] compress failed for r{rev}", file=sys.stderr, flush=True)
            log_row(rev, author, now(), "failed (compress)")
            log_fail(rev)
            save_checkpoint(rev)
            return

        clean_old_tars()
        log_row(rev, author, now(), "successful")
        save_checkpoint(rev)
        return

    # 3. retry after make clean
    make_clean()

    if build():
        if not compress_to_tar(rev):
            print(f"[CI] compress failed for r{rev}", file=sys.stderr, flush=True)
            log_row(rev, author, now(), "failed (compress)")
            log_fail(rev)
            save_checkpoint(rev)
            return

        clean_old_tars()
        log_row(rev, author, now(), "successful")
        save_checkpoint(rev)
        return

    # 4. build failed after retry
    log_row(rev, author, now(), "failed")
    log_fail(rev)
    save_checkpoint(rev)


# ================= MAIN LOOP =================

def main() -> int:
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    init_log()

    BIN_DST.mkdir(parents=True, exist_ok=True)
    os.chdir(BIN_DST)

    print("[CI] SVN build watcher started", flush=True)

    while running:
        head = get_head()
        local = load_checkpoint()

        if head < 0:
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

                time.sleep(1)

            print(flush=True)
            continue

        print(f"[CI] update detected {local} -> {head}", flush=True)

        for rev in range(local + 1, head + 1):
            if not running:
                break

            build_revision(rev)

        time.sleep(2)

    print("[CI] exit", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())