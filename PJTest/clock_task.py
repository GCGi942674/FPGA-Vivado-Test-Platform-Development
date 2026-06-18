#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PJTest daily batch runner.

Reads tasks.yaml and creates tasks using taskctl apply.
Ensures only one batch runs at a time by checking for pending/running examples.
Records batch start/end timestamps and duration.
"""

import argparse
import json
import os
import sys
import subprocess
import time
from datetime import datetime
from pathlib import Path

# -------- 配置（可通过环境变量覆盖）---------
DEFAULT_DB_PATH = "/home/user3/PJTest/data/task_queue.db"
DEFAULT_YAML_PATH = "/home/user3/PJTest/tasks.yaml"
DEFAULT_TASKCTL_PATH = "/home/user3/PJTest/taskctl.py"
DEFAULT_BATCH_STATUS_FILE = "/home/user3/PJTest/batch_status.json"
DEFAULT_LOG_FILE = "/home/user3/PJTest/logs/daily_runner.log"

# 环境变量支持
DB_PATH = Path(os.environ.get("PJTEST_DB_PATH", DEFAULT_DB_PATH))
YAML_PATH = Path(os.environ.get("PJTEST_YAML_PATH", DEFAULT_YAML_PATH))
TASKCTL_PATH = Path(os.environ.get("PJTEST_TASKCTL_PATH", DEFAULT_TASKCTL_PATH))
BATCH_STATUS_FILE = Path(os.environ.get("PJTEST_BATCH_STATUS_FILE", DEFAULT_BATCH_STATUS_FILE))
LOG_FILE = Path(os.environ.get("PJTEST_DAILY_LOG_FILE", DEFAULT_LOG_FILE))

# -------- 工具函数 --------
def log_message(msg):
    """记录带时间戳的日志到文件和控制台"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")

def get_db_connection():
    """返回 SQLite 连接"""
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    return conn

def has_pending_or_running_examples():
    """检查数据库中是否有 pending 或 running 的示例"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) AS cnt
        FROM task_examples
        WHERE status IN ('pending', 'running')
    """)
    row = cur.fetchone()
    conn.close()
    return row["cnt"] > 0

def load_batch_status():
    """读取批次状态文件，若不存在返回 None"""
    if not BATCH_STATUS_FILE.is_file():
        return None
    with BATCH_STATUS_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)

def write_batch_status(status):
    """写入批次状态文件"""
    BATCH_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with BATCH_STATUS_FILE.open("w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)

def finish_current_batch():
    """检查当前批次是否已完成（数据库中无 pending/running），若是则记录结束时间并计算耗时"""
    status = load_batch_status()
    if not status:
        return False  # 没有活跃批次
    if status.get("batch_end") is not None:
        return False  # 已经结束

    # 检查是否还有未完成任务
    if has_pending_or_running_examples():
        return False  # 未完成

    # 任务已完成，记录结束时间
    end_time = datetime.now()
    start_time = datetime.strptime(status["batch_start"], "%Y-%m-%d %H:%M:%S")
    duration = (end_time - start_time).total_seconds()
    status["batch_end"] = end_time.strftime("%Y-%m-%d %H:%M:%S")
    status["duration_seconds"] = duration
    write_batch_status(status)
    log_message(f"Batch finished. Duration: {duration:.2f} seconds")
    return True

def run_taskctl_apply(yaml_path):
    """调用 taskctl apply 创建任务"""
    cmd = [str(TASKCTL_PATH), "apply", str(yaml_path)]
    log_message(f"Running: {' '.join(cmd)}")
    try:
        # 实时输出到日志
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            log_message(f"  {line.rstrip()}")
        rc = proc.wait()
        if rc != 0:
            log_message(f"taskctl apply exited with code {rc}")
            return False
        return True
    except Exception as e:
        log_message(f"Failed to run taskctl apply: {e}")
        return False

def start_new_batch():
    """开始新批次：调用 taskctl apply 并记录开始状态"""
    if not YAML_PATH.is_file():
        log_message(f"YAML file not found: {YAML_PATH}")
        return False

    # 记录批次开始
    start_time = datetime.now()
    status = {
        "batch_start": start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "batch_end": None,
        "duration_seconds": None,
        "yaml_file": str(YAML_PATH),
    }
    write_batch_status(status)
    log_message(f"Started new batch at {status['batch_start']}")

    # 执行 taskctl apply
    success = run_taskctl_apply(YAML_PATH)

    if not success:
        # 如果失败，删除批次状态或标记为失败？这里选择标记结束但记录失败
        status["batch_end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status["duration_seconds"] = 0
        status["error"] = "taskctl apply failed"
        write_batch_status(status)
        log_message("Batch creation failed, status updated.")
        return False

    log_message("Batch creation completed successfully.")
    return True

# -------- 主逻辑 --------
def main():
    parser = argparse.ArgumentParser(description="PJTest daily batch runner")
    parser.add_argument("--check-only", action="store_true", help="Only check if a batch is running, don't start new")
    args = parser.parse_args()

    log_message("=== Daily runner started ===")

    # 1. 先尝试结束当前批次（如果有且已完成）
    finished = finish_current_batch()
    if finished:
        log_message("Previous batch was finished and recorded.")

    # 2. 如果只检查，则退出
    if args.check_only:
        log_message("Check-only mode, exiting.")
        return 0

    # 3. 检查是否有未完成的任务（包括正在运行的）
    if has_pending_or_running_examples():
        log_message("There are pending/running examples. Skipping new batch creation.")
        return 0

    # 4. 检查是否有活跃批次状态且未结束（理论上不该发生，因为上面 finish 会结束）
    status = load_batch_status()
    if status and status.get("batch_end") is None:
        # 这种情况可能是数据库已无任务但状态未更新，我们强制结束
        log_message("Batch status indicates active but no pending/running examples. Forcing finish.")
        status["batch_end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status["duration_seconds"] = 0
        status["error"] = "forced finish due to inconsistency"
        write_batch_status(status)

    # 5. 开始新批次
    start_new_batch()

    log_message("=== Daily runner finished ===")

if __name__ == "__main__":
    main()