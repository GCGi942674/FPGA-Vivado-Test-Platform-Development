#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import signal
import shutil
import subprocess
from datetime import datetime

# ================= CONFIG =================

WORK_DIR = "/home/user3/workspace/galaxcore"

BIN_SRC = "/home/user3/workspace/galaxcore/bin/Linux_64/GalaxCore"

BIN_DST = "/home/xiaonan/Share/zw_cache/Galaxcore_bin"

SVN_URL = "http://192.168.10.10/svn/galaxcore/galaxcore"

HISTORY_LOG = os.path.join(BIN_DST, "build_history.log")
FAIL_LOG = os.path.join(BIN_DST, "mk_fail")
CHECKPOINT_FILE = os.path.join(BIN_DST, "last_revision.txt")

MAX_KEEP = 150

# ================= GLOBAL =================

running = True
mk_proc = None
build_no = 0

# ================= TIME =================


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ================= SIGNAL =================


def signal_handler(sig, frame):
    global running
    global mk_proc

    running = False

    print("\n[CI] stopping safely...")

    if mk_proc and mk_proc.poll() is None:
        try:
            print(f"[CI] killing mk process group pid={mk_proc.pid}")

            os.killpg(os.getpgid(mk_proc.pid), signal.SIGINT)
            time.sleep(1)

            if mk_proc.poll() is None:
                os.killpg(os.getpgid(mk_proc.pid), signal.SIGKILL)

        except Exception as e:
            print(f"[CI] kill mk failed: {e}")


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ================= SVN =================


def get_head_revision():
    try:
        out = subprocess.check_output(
            ["svn", "info", SVN_URL, "--show-item", "revision"],
            text=True
        ).strip()

        return int(out)

    except Exception:
        return -1


def get_local_revision():
    try:
        out = subprocess.check_output(
            ["svn", "info", "--show-item", "revision"],
            cwd=WORK_DIR,
            text=True
        ).strip()

        return int(out)

    except Exception:
        return 0


# ================= CHECKPOINT =================


def load_checkpoint():
    if not os.path.exists(CHECKPOINT_FILE):
        return get_local_revision()

    try:
        with open(CHECKPOINT_FILE, "r") as f:
            return int(f.read().strip())

    except Exception:
        return get_local_revision()


def save_checkpoint(rev):
    with open(CHECKPOINT_FILE, "w") as f:
        f.write(str(rev))


# ================= LOG =================


def init_log():
    with open(HISTORY_LOG, "a") as f:
        f.write("============================================================\n")
        f.write(f"Starting monitor time: {now()}\n")
        f.write(f"Starting version: r{load_checkpoint()}\n")
        f.write("============================================================\n")
        f.write("No.\tRevision\tAuthor\tTime\t\t\tResult\n")
        f.write("------------------------------------------------------------\n")


def log_history(rev, result):
    global build_no

    build_no += 1

    with open(HISTORY_LOG, "a") as f:
        f.write(
            f"{build_no}\t"
            f"r{rev}\t"
            f"unknown\t"
            f"{now()}\t"
            f"{result}\n"
        )


def log_fail(rev):
    with open(FAIL_LOG, "a") as f:
        f.write("==================================================\n")
        f.write(f"[{now()}] FAIL r{rev}\n")
        f.write("==================================================\n")


# ================= SVN CLEAN =================


def svn_clean():
    print("[CI] svn clean start")

    subprocess.call(
        "svn revert -R . > /dev/null 2>&1",
        cwd=WORK_DIR,
        shell=True
    )

    subprocess.call(
        "svn cleanup > /dev/null 2>&1",
        cwd=WORK_DIR,
        shell=True
    )

    print("[CI] svn clean done")


# ================= CHECKOUT =================


def checkout_revision(rev):
    print(f"[CI] svn update r{rev}")

    ret = subprocess.call(
        f"svn update -r {rev} > /dev/null 2>&1",
        cwd=WORK_DIR,
        shell=True
    )

    if ret == 0:
        print("[CI] svn update OK")
        return True

    print("[CI] svn update FAIL")
    return False


# ================= BUILD =================


def build():
    global mk_proc

    print("[CI] mk starting...")

    devnull = open(os.devnull, "w")

    mk_proc = subprocess.Popen(
        'csh -c "mk"',
        cwd=WORK_DIR,
        shell=True,
        stdout=devnull,
        stderr=devnull,
        preexec_fn=os.setsid
    )

    ret = mk_proc.wait()

    print(f"[CI] mk finished exit={ret}")

    return ret == 0


# ================= CLEAN =================


def make_clean():
    print("[CI] make clean start")

    subprocess.call(
        "make clean > /dev/null 2>&1",
        cwd=WORK_DIR,
        shell=True
    )

    print("[CI] make clean done")


# ================= COPY BIN =================


def copy_binary(rev):
    dst = os.path.join(BIN_DST, f"Galaxcore_{rev}")

    if not os.path.exists(BIN_SRC):
        return False

    try:
        shutil.copy2(BIN_SRC, dst)
        return True

    except Exception:
        return False


# ================= KEEP LIMIT =================


def cleanup_old_bins():
    files = []

    for name in os.listdir(BIN_DST):
        if name.startswith("Galaxcore_"):
            path = os.path.join(BIN_DST, name)

            if os.path.isfile(path):
                files.append(path)

    files.sort(key=lambda x: os.path.getmtime(x))

    while len(files) > MAX_KEEP:
        old = files.pop(0)

        try:
            print(f"[CI] remove old bin {os.path.basename(old)}")
            os.remove(old)

        except Exception:
            pass


# ================= BUILD FLOW =================


def build_revision(rev):
    print("\n==============================")
    print(f"[CI] build r{rev}")
    print("==============================")

    svn_clean()

    if not checkout_revision(rev):
        return

    # first build
    if build():
        if copy_binary(rev):
            print(f"[CI] binary saved: Galaxcore_{rev}")

        log_history(rev, "SUCCESS")
        save_checkpoint(rev)

        cleanup_old_bins()
        return

    # retry after clean
    make_clean()

    if build():
        if copy_binary(rev):
            print(f"[CI] binary saved: Galaxcore_{rev}")

        log_history(rev, "SUCCESS_AFTER_CLEAN")
        save_checkpoint(rev)

        cleanup_old_bins()
        return

    log_history(rev, "FAIL")
    log_fail(rev)

    save_checkpoint(rev)


# ================= MAIN =================


def main():
    os.chdir(BIN_DST)

    init_log()

    print("[CI] SVN build watcher started")

    while running:
        head = get_head_revision()
        local = load_checkpoint()

        if head < 0:
            time.sleep(10)
            continue

        if head <= local:
            print(f"[CI] idle | local={local} head={head}")

            time.sleep(10)
            continue

        print(f"[CI] update detected {local} -> {head}")

        for rev in range(local + 1, head + 1):
            if not running:
                break

            build_revision(rev)

        time.sleep(2)

    print("[CI] exit")


if __name__ == "__main__":
    main()