#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
worker_run_test.py

按 night_build_galaxcore.sh 的运行方式重写的 worker 测试脚本：
1. 仍然从共享目录查找最新 GalaxCore_xxxxx.zip
2. 按 zip 文件名中的 revision 对齐本地 SVN
3. 检查本地 GalaxCore 是否正在被占用
4. 解压 zip 并替换本地 bin/Linux_64/GalaxCore
5. 修改 test2/flow_config，确保 enable_copy 1
6. 用 tcsh/csh 模拟手动终端环境：source ~/.cshrc + 设置 LD_LIBRARY_PATH
7. 进入 test2 执行 ./run.sh <TEST_RUN_DIR>
8. 记录和 night_build 类似的 step 日志、summary、耗时、返回码
"""

import fcntl
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from typing import Callable, Optional, Tuple

# ==============================
# User configuration
# ==============================

PROJECT_NAME = "galaxcore"
WORKSPACE_DIR = os.path.expanduser("~/workspace/galaxcore")
TEST_DIR = os.path.join(WORKSPACE_DIR, "test2")
FLOW_CONFIG_FILE = os.path.join(TEST_DIR, "flow_config")

SHARE_ZIP_DIR = "/home/xiaonan/Share/zw_cache/GalaxCore_bin/zip"
BIN_DIR = os.path.join(WORKSPACE_DIR, "bin/Linux_64")
TARGET_BINARY = os.path.join(BIN_DIR, "GalaxCore")

# Runtime library required by GalaxCore, same as night_build_galaxcore.sh
PROTOBUF_LIB_DIR = "/home/fpga/lib/protobuf-3.9.0/lib"

# worker 默认模拟 night_build 的 ./run.sh .，如需只跑某个目录：
# export GALAXCORE_TEST_TARGET=kintexuplus/
TEST_RUN_DIR = os.environ.get("GALAXCORE_TEST_TARGET", ".")

# night_build 里面 run.sh 的 testcase 失败不让整个 flow 失败，这里默认保持一致。
# 如果你希望 worker 根据 run.sh 返回码决定任务失败：export GALAXCORE_IGNORE_RUN_RC=0
IGNORE_RUN_SH_RC = os.environ.get("GALAXCORE_IGNORE_RUN_RC", "1") != "0"

# worker 不建议默认重试 3 次并睡 30 分钟，否则调度中心可能长时间等不到结果。
# 如需模拟 night_build 的 3 次重试：export GALAXCORE_WORKER_MAX_ATTEMPTS=3
MAX_ATTEMPTS = int(os.environ.get("GALAXCORE_WORKER_MAX_ATTEMPTS", "1"))
RETRY_INTERVAL = int(os.environ.get("GALAXCORE_WORKER_RETRY_INTERVAL", "1800"))

# 日志位置改成和 night_build 接近：~/logs/galaxcore/YYYY-MM-DD/
LOG_ROOT = os.path.expanduser("~/logs/galaxcore")
DATE_TAG = datetime.now().strftime("%Y-%m-%d")
RUN_TAG = datetime.now().strftime("%H%M%S")
LOG_DIR = os.path.join(LOG_ROOT, DATE_TAG)
LOG_FILE = os.path.join(LOG_DIR, f"worker_run_{RUN_TAG}.log")
STEP_SUMMARY_FILE = os.path.join(LOG_DIR, f"worker_steps_{RUN_TAG}.tsv")
SUMMARY_FILE = os.path.join(LOG_ROOT, "worker_summary.tsv")

LOCK_FILE = "/tmp/galaxcore_worker.lock"
TMP_DIR = "/tmp/galaxcore_worker"

# 只清理 zip 解压临时目录，不默认清理 testcase 输出。
CLEAN_TMP_DIR = True
POST_RUN_CLEANUP = False

START_TS = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
START_EPOCH = int(time.time())
CURRENT_STEP = ""
FAILED_STEP = ""
FAILED_REASON = ""
CURRENT_ATTEMPT = 0


# ==============================
# Helper functions
# ==============================

def ensure_log_dir() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(LOG_ROOT, exist_ok=True)


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    ensure_log_dir()
    line = f"[{now_ts()}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def append_step_summary(step_name: str, start_ts: str, end_ts: str, duration: int, rc: int) -> None:
    ensure_log_dir()
    if not os.path.exists(STEP_SUMMARY_FILE):
        with open(STEP_SUMMARY_FILE, "w", encoding="utf-8") as f:
            f.write("step_name\tstart_time\tend_time\tduration_sec\texit_code\tstatus\n")

    status = "SUCCESS" if rc == 0 else "FAILED"
    with open(STEP_SUMMARY_FILE, "a", encoding="utf-8") as f:
        f.write(f"{step_name}\t{start_ts}\t{end_ts}\t{duration}\t{rc}\t{status}\n")


def append_daily_summary(status: str) -> None:
    ensure_log_dir()
    end_ts = now_ts()
    duration = int(time.time()) - START_EPOCH

    if not os.path.exists(SUMMARY_FILE):
        with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
            f.write("date\tstart_time\tend_time\tduration_sec\tstatus\tattempt\tfailed_step\tlog_file\thost\n")

    with open(SUMMARY_FILE, "a", encoding="utf-8") as f:
        f.write(
            f"{DATE_TAG}\t{START_TS}\t{end_ts}\t{duration}\t{status}\t"
            f"{CURRENT_ATTEMPT}\t{FAILED_STEP}\t{LOG_FILE}\t{socket.gethostname()}\n"
        )


def fail(step_name: str, reason: str) -> int:
    global FAILED_STEP, FAILED_REASON
    FAILED_STEP = step_name
    FAILED_REASON = reason
    log(f"[ERROR] [{step_name}] {reason}")
    return 1


def find_csh() -> Optional[str]:
    # night_build 用 tcsh；如果机器只有 csh，也允许 fallback。
    return shutil.which("tcsh") or shutil.which("csh")


def csh_runtime_prefix() -> str:
    """Return csh code that simulates the user's terminal environment."""
    # 注意：csh 里如果 LD_LIBRARY_PATH 不存在，直接引用会报 Undefined variable。
    return f"""
if ( -f ~/.cshrc ) source ~/.cshrc
if ( $?LD_LIBRARY_PATH ) then
    setenv LD_LIBRARY_PATH {PROTOBUF_LIB_DIR}:$LD_LIBRARY_PATH
else
    setenv LD_LIBRARY_PATH {PROTOBUF_LIB_DIR}
endif
"""


def run_process(args, step_log_prefix: Optional[str] = None) -> int:
    """Run a command and stream stdout/stderr into LOG_FILE."""
    display = " ".join(args) if isinstance(args, (list, tuple)) else str(args)
    log(f"[CMD] {display}")

    try:
        p = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        log(f"[ERROR] command not found: {exc}")
        return 127

    assert p.stdout is not None
    for line in p.stdout:
        line = line.rstrip("\n")
        if step_log_prefix:
            log(f"{step_log_prefix}{line}")
        else:
            log(line)

    rc = p.wait()
    log(f"[CMD_EXIT] rc={rc}")
    return rc


def run_bash(script: str) -> int:
    return run_process(["/bin/bash", "-lc", script])


def run_csh(script: str) -> int:
    csh_bin = find_csh()
    if not csh_bin:
        log("[ERROR] neither tcsh nor csh found in PATH")
        return 127
    # -f：不自动加载 cshrc；脚本里手动 source ~/.cshrc，和 night_build 的 tcsh -fc 保持一致。
    return run_process([csh_bin, "-f", "-c", script])


def run_step(step_name: str, func: Callable[[], int]) -> int:
    global CURRENT_STEP
    CURRENT_STEP = step_name

    step_start_epoch = int(time.time())
    step_start_ts = now_ts()

    log("============================================================")
    log(f"[STEP START] {step_name}")
    log("============================================================")

    rc = func()

    step_end_ts = now_ts()
    duration = int(time.time()) - step_start_epoch
    append_step_summary(step_name, step_start_ts, step_end_ts, duration, rc)

    if rc != 0:
        log(f"[STEP FAIL ] {step_name} (cost {duration}s)")
        CURRENT_STEP = ""
        fail(step_name, f"exit code {rc}")
        return rc

    log(f"[STEP DONE ] {step_name} (cost {duration}s)")
    CURRENT_STEP = ""
    return 0


# ==============================
# Worker task functions
# ==============================

def extract_revision(filename: str) -> int:
    m = re.search(r"^GalaxCore_(\d+)\.zip$", filename)
    return int(m.group(1)) if m else -1


def find_latest_zip() -> Tuple[Optional[int], Optional[str]]:
    if not os.path.isdir(SHARE_ZIP_DIR):
        log(f"[ERROR] share zip directory not found: {SHARE_ZIP_DIR}")
        return None, None

    zips = []
    for filename in os.listdir(SHARE_ZIP_DIR):
        rev = extract_revision(filename)
        if rev > 0:
            zips.append((rev, filename))

    if not zips:
        log(f"[ERROR] no GalaxCore_xxxxx.zip found in {SHARE_ZIP_DIR}")
        return None, None

    zips.sort(key=lambda x: x[0], reverse=True)
    rev, filename = zips[0]
    log(f"[INFO] Latest zip found: {filename}")
    log(f"[INFO] Parsed SVN revision: {rev}")
    return rev, os.path.join(SHARE_ZIP_DIR, filename)


def basic_checks() -> int:
    if not os.path.isdir(WORKSPACE_DIR):
        return fail("basic checks", f"Workspace directory not found: {WORKSPACE_DIR}")
    if not os.path.isdir(TEST_DIR):
        return fail("basic checks", f"Test directory not found: {TEST_DIR}")
    if not find_csh():
        return fail("basic checks", "tcsh/csh not found in PATH")
    if not shutil.which("svn"):
        return fail("basic checks", "svn not found in PATH")
    if not shutil.which("unzip"):
        return fail("basic checks", "unzip not found in PATH")

    log(f"[INFO] host: {socket.gethostname()}")
    log(f"[INFO] csh path: {find_csh()}")
    log(f"[INFO] workspace: {WORKSPACE_DIR}")
    log(f"[INFO] test dir: {TEST_DIR}")
    log(f"[INFO] protobuf lib dir: {PROTOBUF_LIB_DIR}")
    log(f"[INFO] test target: {TEST_RUN_DIR}")
    log(f"[INFO] ignore run.sh rc: {int(IGNORE_RUN_SH_RC)}")
    log(f"[INFO] log file: {LOG_FILE}")
    return 0


def svn_update_to_revision(rev: int) -> int:
    script = f"""
{csh_runtime_prefix()}
cd "{WORKSPACE_DIR}"
svn update -r {rev}
"""
    return run_csh(script)


def check_galaxcore_binary_not_in_use() -> int:
    if not os.path.exists(TARGET_BINARY):
        log(f"[WARN] target binary does not exist yet: {TARGET_BINARY}")
        return 0

    if not shutil.which("fuser"):
        return fail("check binary in use", "fuser not found in PATH")

    log(f"[INFO] Checking whether target GalaxCore binary is in use: {TARGET_BINARY}")
    p = subprocess.run(
        ["fuser", TARGET_BINARY],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    stdout = p.stdout.strip()
    stderr = p.stderr.strip()

    # fuser rc=0：至少有进程占用该文件。
    if p.returncode == 0:
        if stdout:
            log(f"fuser stdout: {stdout}")
        if stderr:
            log(f"fuser stderr: {stderr}")
        return fail("check binary in use", f"target GalaxCore binary is in use: {TARGET_BINARY}")

    # fuser rc=1：通常表示没有进程占用。
    if p.returncode == 1:
        if stderr:
            log(f"[WARN] fuser warning ignored: {stderr}")
        log("[INFO] Target GalaxCore binary is not in use")
        return 0

    if stdout:
        log(f"fuser stdout: {stdout}")
    if stderr:
        log(f"fuser stderr: {stderr}")
    return fail("check binary in use", f"fuser returned unexpected rc={p.returncode}")


def find_extracted_galaxcore_binary(tmp_dir: str) -> Optional[str]:
    direct_path = os.path.join(tmp_dir, "GalaxCore")
    if os.path.isfile(direct_path):
        return direct_path

    for root, _, files in os.walk(tmp_dir):
        if "GalaxCore" in files:
            return os.path.join(root, "GalaxCore")

    return None


def extract_zip(zip_path: str) -> int:
    if os.path.exists(TMP_DIR):
        shutil.rmtree(TMP_DIR)
    os.makedirs(TMP_DIR, exist_ok=True)

    log(f"[INFO] Extracting {zip_path} -> {TMP_DIR}")
    return run_bash(f"unzip -o {shell_quote(zip_path)} -d {shell_quote(TMP_DIR)}")


def shell_quote(s: str) -> str:
    # Python 3.6 on old servers may not always have shlex.join, so keep a small local quote helper.
    import shlex
    return shlex.quote(s)


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
    log(f"[INFO] Binary replaced: {extracted_binary} -> {TARGET_BINARY}")
    return 0


def update_flow_config() -> int:
    if not os.path.exists(FLOW_CONFIG_FILE):
        return fail("update flow_config", f"flow_config file not found: {FLOW_CONFIG_FILE}")

    with open(FLOW_CONFIG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    found = False
    new_lines = []
    for line in lines:
        # 替换非注释行里的 enable_copy，兼容 enable_copy 1 / enable_copy = 1 这两种旧写法。
        if re.match(r"^\s*enable_copy\b", line):
            new_lines.append("enable_copy 1\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append("\nenable_copy 1\n")
        log(f"[WARN] enable_copy line not found, appended 'enable_copy 1' to {FLOW_CONFIG_FILE}")
    else:
        log(f"[INFO] Updated enable_copy to 1 in {FLOW_CONFIG_FILE}")

    with open(FLOW_CONFIG_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    log("[INFO] Current enable_copy lines:")
    with open(FLOW_CONFIG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        for idx, line in enumerate(f, start=1):
            if "enable_copy" in line:
                log(f"{idx}:{line.rstrip()}")

    return 0


def dump_csh_runtime_env() -> int:
    env_dump = f"/tmp/galaxcore_worker_env_{socket.gethostname()}_{os.getpid()}.txt"
    script = f"""
{csh_runtime_prefix()}
cd "{TEST_DIR}"
echo '[DEBUG] pwd:' `pwd`
echo '[DEBUG] csh command loaded successfully'
echo '[DEBUG] PATH:' $PATH
echo '[DEBUG] LD_LIBRARY_PATH:' $LD_LIBRARY_PATH
echo '[DEBUG] env dump: {env_dump}'
env | sort > "{env_dump}"
"""
    return run_csh(script)


def check_galaxcore_libs() -> int:
    script = f"""
{csh_runtime_prefix()}
cd "{TEST_DIR}"
echo '[DEBUG] pwd:' `pwd`
echo '[DEBUG] GalaxCore path:'
ls -l "{TARGET_BINARY}"
echo '[DEBUG] ldd missing libs:'
ldd "{TARGET_BINARY}" | grep 'not found' || true
"""
    return run_csh(script)


def run_tests() -> int:
    ignore_flag = "1" if IGNORE_RUN_SH_RC else "0"
    script = f"""
{csh_runtime_prefix()}
cd "{TEST_DIR}"
set test_arg = "{TEST_RUN_DIR}"
echo '[DEBUG] pwd:' `pwd`
echo "[DEBUG] TEST_RUN_DIR: $test_arg"
echo '[DEBUG] LD_LIBRARY_PATH:' $LD_LIBRARY_PATH
./run.sh "$test_arg"
set run_rc = $status
echo "[INFO] ./run.sh $test_arg finished with exit code $run_rc"
if ( "{ignore_flag}" == "1" ) then
    echo "[INFO] Ignore testcase failures and continue worker flow, same as night_build"
    exit 0
else
    exit $run_rc
endif
"""
    return run_csh(script)


def post_run_cleanup() -> int:
    if not POST_RUN_CLEANUP:
        log("[INFO] POST_RUN_CLEANUP=0, keep test output files for inspection")
        return 0

    script = f"""
set -e
cd {shell_quote(TEST_DIR)}
./clean.sh 2>/dev/null || true
[ -d kintexuplus ] && rm -rf kintexuplus/*
[ -d virtexuplus ] && rm -rf virtexuplus/*
"""
    return run_bash(script)


def cleanup_tmp() -> None:
    if CLEAN_TMP_DIR:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        log(f"[INFO] Temporary directory cleaned: {TMP_DIR}")


# ==============================
# Main worker logic
# ==============================

def run_attempt() -> int:
    global FAILED_STEP, FAILED_REASON, CURRENT_STEP
    FAILED_STEP = ""
    FAILED_REASON = ""
    CURRENT_STEP = ""

    log("============================================================")
    log("Worker job started")
    log(f"Project    : {PROJECT_NAME}")
    log(f"Workspace  : {WORKSPACE_DIR}")
    log(f"Test dir   : {TEST_DIR}")
    log(f"Attempt    : {CURRENT_ATTEMPT}/{MAX_ATTEMPTS}")
    log(f"Log file   : {LOG_FILE}")
    log("============================================================")

    rc = run_step("basic checks", basic_checks)
    if rc != 0:
        return rc

    rev_zip = {"rev": None, "zip_path": None}

    def step_find_latest_zip() -> int:
        rev, zip_path = find_latest_zip()
        if rev is None or zip_path is None:
            return 1
        rev_zip["rev"] = rev
        rev_zip["zip_path"] = zip_path
        return 0

    rc = run_step("find latest zip", step_find_latest_zip)
    if rc != 0:
        return rc

    rc = run_step("svn update", lambda: svn_update_to_revision(int(rev_zip["rev"])))
    if rc != 0:
        return rc

    rc = run_step("check binary in use", check_galaxcore_binary_not_in_use)
    if rc != 0:
        return rc

    rc = run_step("extract latest zip", lambda: extract_zip(str(rev_zip["zip_path"])))
    if rc != 0:
        return rc

    rc = run_step("replace binary", replace_binary_from_tmp)
    if rc != 0:
        return rc

    rc = run_step("update flow_config", update_flow_config)
    if rc != 0:
        return rc

    rc = run_step("dump csh runtime env", dump_csh_runtime_env)
    if rc != 0:
        return rc

    rc = run_step("check GalaxCore libs", check_galaxcore_libs)
    if rc != 0:
        return rc

    rc = run_step(f"./run.sh {TEST_RUN_DIR}", run_tests)
    if rc != 0:
        return rc

    rc = run_step("post run cleanup", post_run_cleanup)
    if rc != 0:
        return rc

    log("Worker attempt finished successfully")
    return 0


def main() -> int:
    global CURRENT_ATTEMPT, FAILED_STEP, FAILED_REASON
    ensure_log_dir()

    lock_fd = open(LOCK_FILE, "w")
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("[WARN] Another worker job is still running. Exit without starting a new one.")
            append_daily_summary("SKIPPED_LOCKED")
            return 0

        final_rc = 1
        for attempt in range(1, MAX_ATTEMPTS + 1):
            CURRENT_ATTEMPT = attempt
            final_rc = run_attempt()

            if final_rc == 0:
                append_daily_summary("SUCCESS")
                return 0

            if attempt < MAX_ATTEMPTS:
                log(f"[INFO] Attempt {attempt} failed at step [{FAILED_STEP}]")
                log(f"[INFO] Reason: {FAILED_REASON}")
                log(f"[INFO] Sleep {RETRY_INTERVAL}s, then restart whole flow from latest zip")
                time.sleep(RETRY_INTERVAL)

        append_daily_summary("FAILED")
        return final_rc

    except Exception as exc:  # 保底，避免异常时没有日志。
        if not FAILED_STEP:
            FAILED_STEP = CURRENT_STEP or "unexpected error"
        if not FAILED_REASON:
            FAILED_REASON = str(exc)
        log(f"[ERROR] Unexpected exception at step [{FAILED_STEP}]: {exc}")
        append_daily_summary("FAILED")
        return 1

    finally:
        cleanup_tmp()
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            lock_fd.close()


if __name__ == "__main__":
    sys.exit(main())
