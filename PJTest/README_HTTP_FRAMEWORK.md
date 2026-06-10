# PJTest HTTP 分布式测试系统

PJTest 是 GalaxCore 的 HTTP 分布式测试框架。当前采用：

```text
pudong 中心调度 + worker 主动拉取 example + SQLite 保存状态 + Share 按任务生成最终报告
```

核心原则：

```text
pudong 负责状态、调度、汇总、报告
worker 负责执行，不直接访问数据库
每个 worker slot 是一个独立 GalaxCore SVN working copy
```

---

## 1. 固定路径

| 项目 | 路径 |
|---|---|
| 项目目录 | `/home/user3/PJTest` |
| 数据库 | `/home/user3/PJTest/data/task_queue.db` |
| GalaxCore zip 目录 | `/home/xshare/zw_cache/distributed_test_system/GalaxCore_bin/zip` |
| Share 根目录 | `/home/xshare/zw_cache/distributed_test_system` |
| Share 报告根目录 | `/home/xshare/zw_cache/distributed_test_system/reports` |
| worker 日志目录 | `/home/user3/PJTest/logs` |
| worker slot 根目录 | `/home/user3/PJTest/worker_slots` |
| slot SVN 地址 | `http://192.168.10.10/svn/galaxcore/galaxcore` |

Share 根目录现在只建议保留：

```text
/home/xshare/zw_cache/distributed_test_system/
├── GalaxCore_bin/
│   └── zip/
└── reports/
```

根目录不再放这些文件：

```text
stat_summary
status_summary
list_pass_to_run
list_fail_to_run
timeout_list
execution_report.json
execution_report.txt
tasks_summary.json
tasks_summary.txt
```

---

## 2. 框架职责

### pudong / scheduler.py

`scheduler.py` 是唯一的状态中心，负责：

```text
1. 接收 worker 注册和心跳
2. 从数据库选择 pending example
3. 每次只分配一个 example 给 worker slot
4. 根据最新 GalaxCore zip 解析版本
5. 接收 worker 执行结果
6. 更新 tasks / task_examples / task_attempts
7. 汇总大任务状态
8. 自动刷新 reports/r<revision>/<task_id>/ 下的最终报告
```

scheduler 不负责执行测试，不 SSH 到 worker。

### taskctl.py

`taskctl.py` 是 pudong 上的任务管理工具，负责：

```text
1. 添加大任务并扫描 run.tcl
2. 查询 task / example / attempt / worker 状态
3. cancel / delete 任务
4. 手动重新生成 task 报告
```

注意：报告自动刷新由 scheduler 完成；`taskctl.py report` 只是用于手动补生成或重新生成。

### worker.py

`worker.py` 是执行器，负责：

```text
1. 默认启动 8 个 slot
2. 每个 slot 首次启动时 svn co 一份完整 galaxcore 到自己的 slot 目录
3. 已有 slot 默认直接复用，不重复 checkout
4. 空闲时主动向 scheduler pull 一个 example
5. 按 scheduler 下发的 zip_path 覆盖 slot 内 GalaxCore
6. 按任务 flow_config 修改 slot 内 test2/flow_config
7. 执行前先调用 slot/test2/clean.sh 清理残留
8. 执行 slot/test2/run.sh <run_tcl_path>
9. 读取 vivado_runner/runtime/status/.../result.env 判断真实 PASS/FAIL/TIMEOUT
10. 回传 success / failed / timeout / log 信息
11. 执行后再次调用 clean.sh 清理
```

worker 不直接访问 SQLite。

---

## 3. 数据库结构设计

数据库拆成：

```text
tasks
task_examples
task_attempts
workers
task_events
```

这样可以把“用户提交的大任务”、“真正分配给 worker 的小例子”、“worker 实际执行记录”分开保存。

### 3.1 tasks：大任务表

`tasks` 表表示用户提交的一次大任务，例如：

```bash
./taskctl.py add place .
```

这个命令不会让一台 worker 直接跑完整个 `.`，而是生成一个大任务，并拆成多个 example。

`tasks` 主要保存：

```text
task_id
template_name
revision / revision_policy
target_dir
priority
max_retry
max_time
total_examples
created_at / started_at / finished_at
status
message
```

### 3.2 task_examples：小例子表

`task_examples` 表表示大任务拆出来的每一个可执行 example。

例如：

```text
kintexuplus/xcku3p-ffvd900-1-i/asym_ram_sdp_8k_16/run.tcl
kintexuplus/xcku3p-ffvd900-1-i/asym_ram_tdp_16k_32/run.tcl
```

worker 每次只拿一个 example。

`task_examples` 保存 example 当前最终状态：

```text
pending / running / success / failed / timeout / canceled
assigned_worker
current_attempt_id
retry_count / max_retry
exit_code
failed_reason
log_file
message
```

### 3.3 task_attempts：执行记录表

`task_attempts` 表表示某个 example 的一次实际执行记录。

当前支持 retry：

```text
max_retry = 0  -> 每个 example 只跑一次
max_retry = 1  -> 第一次失败后最多再跑一次，会产生第二个 attempt
```

也就是说：

```text
example_id 一样，attempt_id 不一样 = 同一个 example 的多次尝试
```

`task_examples` 保存当前最终状态，`task_attempts` 保存每一次执行历史。

### 3.4 workers：工作机状态表

`workers` 表保存 worker 注册、心跳和当前运行状态：

```text
worker_name
hostname
status
current_task_id
current_example_id
current_attempt_id
last_seen_at
started_at
updated_at
message
```

### 3.5 task_events：事件日志表

`task_events` 表保存调度和状态变化事件，例如：

```text
task_created
example_claimed
example_success
example_failed
example_timeout
example_requeued
task_status_changed
task_deleted
task_canceled
```

---

## 4. 调度策略

worker slot 每次 pull 时，scheduler 只分配一个 pending example。

选择逻辑等价于：

```sql
SELECT example
FROM task_examples
JOIN tasks
WHERE example.status = 'pending'
  AND task.status IN ('pending', 'running')
  AND (task.target_worker = 'any' OR task.target_worker = 当前worker)
ORDER BY task.priority DESC, task.created_at ASC, example.seq ASC
LIMIT 1;
```

含义：

```text
1. 只分配 pending example
2. 大任务 pending / running 都可以继续分发
3. priority 高的任务优先
4. priority 一样时，先创建的任务优先
5. 同一个大任务内按 seq 顺序分发
6. target_worker = any 时任意 worker 可拿
7. 指定 target_worker 时只有对应 worker 可拿
```

如果第一个大任务只剩最后一个 example 正在跑，其它空闲 worker 不会等待它结束，而是会继续拿后面任务的 pending example。

---

## 5. 初始化数据库

首次部署或需要清空重建时，在 pudong 执行：

```bash
cd /home/user3/PJTest
./init_db.py --reset
```

`--reset` 会清空所有 task / example / attempt / worker / event 数据。

保留旧数据时执行：

```bash
cd /home/user3/PJTest
./init_db.py
```

建议 reset 前先备份：

```bash
cd /home/user3/PJTest
mkdir -p backup
sqlite3 data/task_queue.db ".backup 'backup/task_queue_$(date +%F_%H%M%S).db'"
```

---

## 6. 启动 scheduler

在 pudong 上启动：

```bash
cd /home/user3/PJTest
./scheduler.py --host 0.0.0.0 --port 8888
```

如果要显式指定 zip 目录：

```csh
cd /home/user3/PJTest
setenv GALAXCORE_ZIP_DIR /home/xshare/zw_cache/distributed_test_system/GalaxCore_bin/zip
./scheduler.py --host 0.0.0.0 --port 8888
```

检查 scheduler：

```bash
curl http://192.168.10.11:8888/api/health
```

正常返回类似：

```json
{"ok": true, "service": "scheduler"}
```

---

## 7. 启动 worker

worker 默认启动 8 个 slot。

### tiger

```csh
cd /home/user3/PJTest
setenv SCHEDULER_URL http://192.168.10.11:8888
setenv WORKER_NAME tiger
setenv SHARE_ZIP_DIR /home/xshare/zw_cache/distributed_test_system/GalaxCore_bin/zip
setenv GALAXCORE_IGNORE_RUN_RC 0
mkdir -p /home/user3/PJTest/tmp
./worker.py
```

### kangqiao

```csh
cd /home/user3/PJTest
setenv SCHEDULER_URL http://192.168.10.11:8888
setenv WORKER_NAME kangqiao
setenv SHARE_ZIP_DIR /home/xshare/zw_cache/distributed_test_system/GalaxCore_bin/zip
setenv GALAXCORE_IGNORE_RUN_RC 0
mkdir -p /home/user3/PJTest/tmp
./worker.py
```

### yangpu

```csh
cd /home/user3/PJTest
setenv SCHEDULER_URL http://192.168.10.11:8888
setenv WORKER_NAME yangpu
setenv SHARE_ZIP_DIR /home/xshare/zw_cache/distributed_test_system/GalaxCore_bin/zip
setenv GALAXCORE_IGNORE_RUN_RC 0
mkdir -p /home/user3/PJTest/tmp
./worker.py
```

### pudong 作为补充 worker

pudong 可以跑 worker，但不建议直接开满 8 个 slot。建议从 1 到 2 个开始：

```bash
cd /home/user3/PJTest
./worker.py --worker-name pudong --jobs 1
```

或者降低优先级：

```bash
cd /home/user3/PJTest
ionice -c2 -n7 nice -n 10 ./worker.py --worker-name pudong --jobs 2
```

---

## 8. Worker slot 目录和 SVN checkout

每个 slot 是一个完整 GalaxCore SVN working copy。

第一次启动时，如果 slot 不存在，会自动执行：

```bash
svn co http://192.168.10.10/svn/galaxcore/galaxcore \
  /home/user3/PJTest/worker_slots/<worker>_slotN/galaxcore
```

例如 tiger：

```text
/home/user3/PJTest/worker_slots/tiger_slot1/galaxcore
/home/user3/PJTest/worker_slots/tiger_slot2/galaxcore
...
/home/user3/PJTest/worker_slots/tiger_slot8/galaxcore
```

已有 slot 默认直接复用，不再 checkout。

如果修改了 `galaxcore/test2` 下面的 `run.sh / clean.sh / vivado_runner / case`，启动前更新 test2：

```bash
cd /home/user3/PJTest
./worker.py --update-test2
```

如果需要重拉当前机器所有 slot：

```bash
cd /home/user3/PJTest
./worker.py --recheckout-slots
```

只跑 1 个 slot 调试：

```bash
./worker.py --jobs 1 --once --dump-json
```

---

## 9. Worker 安全退出

按一次 `Ctrl+C` 不是关机，只是请求 worker 进程安全退出。

当前逻辑：

```text
Ctrl+C
    ↓
worker 停止 pull 新任务
    ↓
正在运行的 example 最多等待 30 秒
    ↓
30 秒内跑完则正常 report 后退出
    ↓
超过 30 秒则结束当前子进程组并退出
```

默认：

```bash
./worker.py --shutdown-timeout 30
```

如果想等待更久：

```bash
./worker.py --shutdown-timeout 600
```

如果想一直等当前 example 跑完：

```bash
./worker.py --shutdown-timeout 0
```

不要使用 `kill -9`，否则 scheduler 只能靠 heartbeat timeout 之后再处理 running example。

---

## 10. Worker 执行时注入的环境变量

worker 执行 `run.sh` 前会进入 csh/tcsh 环境，并注入：

```csh
setenv GALAXCORE_RUN_MODE distributed
setenv DTS_RUN_MODE distributed
setenv GALAXCORE_BUILD_REVISION <revision>
setenv GALAXCORE_REVISION <revision>
setenv GALAXCORE_BUILD_ZIP <zip_path>
setenv GALAXCORE_WORKER_NAME <slot_worker_name>
setenv GALAXCORE_TASK_ID <task_id>
setenv GALAXCORE_EXAMPLE_ID <example_id>
setenv GALAXCORE_ATTEMPT_ID <attempt_id>
setenv GALAXCORE_FLOW_CONFIG <slot_test2_flow_config_path>
setenv DTS_WORKER <slot_worker_name>
setenv DTS_SLOT_WORKER <slot_worker_name>
setenv DTS_TASK_ID <task_id>
setenv DTS_EXAMPLE_ID <example_id>
setenv DTS_ATTEMPT_ID <attempt_id>
setenv DTS_FLOW_CONFIG <slot_test2_flow_config_path>
```

worker 根据 `vivado_runner/runtime/status/.../result.env` 判断真实结果：

```text
STATUS=PASS     -> scheduler status=success
STATUS=FAIL     -> scheduler status=failed
STATUS=TIMEOUT  -> scheduler status=timeout
```

所以统计的是 example 是否真正 PASS，而不是 `run.sh` 外壳是否执行结束。

---

## 11. 添加任务

添加 place 任务，跑 `work_root` 下所有 example：

```bash
cd /home/user3/PJTest
./taskctl.py add place .
```

指定固定版本：

```bash
./taskctl.py add place . --revision 14879
```

不指定 `--revision` 时，任务使用 latest 策略。scheduler 在 worker pull example 时扫描：

```text
/home/xshare/zw_cache/distributed_test_system/GalaxCore_bin/zip
```

选择最大的 `GalaxCore_xxxxx.zip` 版本。

不重试：

```bash
./taskctl.py add place . --max-retry 0
```

失败后重试一次：

```bash
./taskctl.py add place . --max-retry 1
```

如果要跑绝对路径列表文件：

```bash
./taskctl.py add place /absolute/path/to/run_tcl_list.txt
```

列表文件内容必须是一行一个绝对 `run.tcl` 路径，并且必须在 `work_root` 下面。

---

## 12. 查看任务状态

查看大任务：

```bash
./taskctl.py list
```

查看某个大任务的所有 example：

```bash
./taskctl.py examples task_xxxxxxxxxxxx
```

详细查看：

```bash
./taskctl.py examples task_xxxxxxxxxxxx -v
```

查看某个 example 的 attempt：

```bash
./taskctl.py attempts ex_xxxxxxxxxxxx
```

详细查看 attempt：

```bash
./taskctl.py attempts ex_xxxxxxxxxxxx -v
```

查看 worker：

```bash
./taskctl.py workers
./taskctl.py workers --status running
```

检查环境：

```bash
./taskctl.py check --scheduler http://192.168.10.11:8888
```

---

## 13. 按状态筛选 example

查看最新任务统计：

```bash
./taskctl.py stat
```

查看指定任务统计：

```bash
./taskctl.py stat task_xxxxxxxxxxxx
```

列出通过的 example：

```bash
./taskctl.py pass task_xxxxxxxxxxxx
```

列出失败的 example：

```bash
./taskctl.py fail task_xxxxxxxxxxxx
```

列出 timeout 的 example：

```bash
./taskctl.py timeout task_xxxxxxxxxxxx
```

列出正在运行的 example：

```bash
./taskctl.py running task_xxxxxxxxxxxx
```

列出还没运行的 example：

```bash
./taskctl.py pending task_xxxxxxxxxxxx
```

只输出路径：

```bash
./taskctl.py fail task_xxxxxxxxxxxx --names-only > fail.list
./taskctl.py timeout task_xxxxxxxxxxxx --names-only > timeout.list
./taskctl.py pass task_xxxxxxxxxxxx --names-only > pass.list
```

查看失败原因和日志路径：

```bash
./taskctl.py fail task_xxxxxxxxxxxx -v
```

---

## 14. 报告文件

scheduler 会在 worker report 后自动刷新 task 报告。

也可以手动从数据库重新生成：

```bash
./taskctl.py report task_xxxxxxxxxxxx
```

默认生成到：

```text
/home/xshare/zw_cache/distributed_test_system/reports/r<revision>/<task_id>/
```

例如：

```text
/home/xshare/zw_cache/distributed_test_system/reports/r14879/task_7bfb683ae33c/
├── stat_summary
├── list_pass_to_run
├── list_fail_to_run
└── timeout_list
```

当前只保留 4 个最终报告文件：

```text
stat_summary
list_pass_to_run
list_fail_to_run
timeout_list
```

不再生成 `status_summary`，也不在 Share 根目录生成任何 summary 文件。

### stat_summary

总体统计文件，用来快速看总数和进度。

包含：

```text
task_id
template
task_name
revision
status
target_dir
total
done
success
failed
timeout
running
pending
progress
workers
created_at
updated_at
```

### list_pass_to_run

只列成功的 example。

### list_fail_to_run

只列 failed 的 example，不包含 timeout。

### timeout_list

只列 timeout 的 example。timeout 后面可能单独重跑，所以单独拆出来。

如果要临时输出到 `/tmp`：

```bash
./taskctl.py report task_xxxxxxxxxxxx --out /tmp/pjtest_reports
```

会生成：

```text
/tmp/pjtest_reports/r<revision>/<task_id>/
```

---

## 15. 取消和删除任务

安全取消任务：

```bash
./taskctl.py cancel task_xxxxxxxxxxxx
```

如果任务中还有 running example，普通 cancel 会拒绝，避免隐藏 worker 仍在运行的事实。

强制取消 running 任务：

```bash
./taskctl.py cancel task_xxxxxxxxxxxx --force
```

删除已完成或 pending 的任务：

```bash
./taskctl.py delete task_xxxxxxxxxxxx
```

强制删除 running 任务：

```bash
./taskctl.py delete task_xxxxxxxxxxxx --force
```

删除大任务后，对应的 `task_examples` 和 `task_attempts` 会一起删除。

---

## 16. HTTP 通信流程

```text
worker slot
  POST /api/task/pull
    ↓
scheduler
  锁定一个 pending example
  创建 attempt 记录
  返回 zip_path / work_root / target_arg / flow_config
    ↓
worker slot
  覆盖 GalaxCore
  修改 flow_config
  pre-clean
  执行 ./run.sh <target_arg>
  解析 result.env
    ↓
worker slot
  POST /api/task/report
    ↓
scheduler
  更新 example / attempt / task 状态
  刷新 reports/r<revision>/<task_id>/ 下的报告
```

---

## 17. 查看 setenv 环境变量

### 查看当前终端变量

```csh
printenv SCHEDULER_URL
printenv WORKER_NAME
printenv SHARE_ZIP_DIR
printenv GALAXCORE_IGNORE_RUN_RC
```

一次性筛选：

```csh
printenv | egrep 'SCHEDULER_URL|WORKER_NAME|SHARE_ZIP_DIR|GALAXCORE_IGNORE_RUN_RC'
```

或者：

```csh
env | egrep 'SCHEDULER_URL|WORKER_NAME|SHARE_ZIP_DIR|GALAXCORE_IGNORE_RUN_RC'
```

### 判断变量是否存在

csh 里如果变量不存在，直接 `echo $VAR` 可能报 `Undefined variable`。推荐：

```csh
if ($?SCHEDULER_URL) then
    echo $SCHEDULER_URL
else
    echo "SCHEDULER_URL not set"
endif
```

### 查看已启动 worker 的环境变量

先找 PID：

```bash
ps -ef | grep '[w]orker.py'
```

假设 PID 是 `12345`：

```bash
tr '\0' '\n' < /proc/12345/environ | egrep 'SCHEDULER_URL|WORKER_NAME|SHARE_ZIP_DIR|GALAXCORE_IGNORE_RUN_RC'
```

注意：worker 已经启动后，再执行 `setenv` 不会影响正在运行的 worker。必须重启 worker。

---

## 18. 常见问题

### 18.1 `./worker.py` 被当成 shell 脚本执行

如果看到：

```text
import: command not found
syntax error near unexpected token
```

检查第一行：

```bash
head -n 3 /home/user3/PJTest/worker.py
```

第一行必须是：

```bash
#!/usr/bin/env python3
```

前面不能有空行。

然后确认权限：

```bash
chmod +x /home/user3/PJTest/worker.py
```

### 18.2 worker 几秒钟就 success

这通常说明 `run.sh` 没真正跑起来，或者返回码被忽略。

确认：

```csh
setenv GALAXCORE_IGNORE_RUN_RC 0
```

然后用调试模式：

```bash
./worker.py --jobs 1 --once --dump-json --verbose-console
```

重点看：

```text
cmd
log_file
target_arg
run_tcl_path
result.env
```

### 18.3 统计全 success，但实际有失败

worker 必须解析 `result.env` 后再 report。正确逻辑是：

```text
STATUS=PASS     -> success
STATUS=FAIL     -> failed
STATUS=TIMEOUT  -> timeout
```

旧 worker 如果只按 `run.sh` 返回码判断，可能会把实际失败误报为 success。升级 worker 后重新跑任务即可。

### 18.4 同一个 example 跑了两遍

如果看到同一个 `example_id` 但不同 `attempt_id`，说明 retry 生效了。

例如：

```text
example_id = ex_xxx, attempt_id = att_aaa
example_id = ex_xxx, attempt_id = att_bbb
```

设置不重试：

```bash
./taskctl.py add place . --max-retry 0
```

已经创建的任务可以在 pudong 上改 DB：

```bash
cd /home/user3/PJTest
sqlite3 data/task_queue.db "
UPDATE tasks SET max_retry = 0 WHERE task_id = 'task_xxxxxxxxxxxx';
UPDATE task_examples SET max_retry = 0 WHERE task_id = 'task_xxxxxxxxxxxx';
"
```

### 18.5 Share 根目录旧报告文件还存在

旧文件不会自动删除，例如：

```text
execution_report.txt
execution_report.json
tasks_summary.txt
tasks_summary.json
stat_summary
status_summary
list_pass_to_run
list_fail_to_run
timeout_list
```

可以先移走，不要直接删除：

```bash
cd /home/xshare/zw_cache/distributed_test_system
BAK="_old_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BAK"

mv -v execution_report.json execution_report.txt "$BAK"/ 2>/dev/null
mv -v tasks_summary.json tasks_summary.txt "$BAK"/ 2>/dev/null
mv -v stat_summary status_summary list_pass_to_run list_fail_to_run timeout_list "$BAK"/ 2>/dev/null
```

清理后根目录应主要保留：

```text
GalaxCore_bin
reports
_old_xxxxx
```

---

## 19. 推荐日常流程

### pudong scheduler

```bash
cd /home/user3/PJTest
./scheduler.py --host 0.0.0.0 --port 8888
```

### worker

```csh
cd /home/user3/PJTest
setenv SCHEDULER_URL http://192.168.10.11:8888
setenv WORKER_NAME tiger
setenv SHARE_ZIP_DIR /home/xshare/zw_cache/distributed_test_system/GalaxCore_bin/zip
setenv GALAXCORE_IGNORE_RUN_RC 0
mkdir -p /home/user3/PJTest/tmp
./worker.py
```

### 添加任务

```bash
cd /home/user3/PJTest
./taskctl.py add place . --max-retry 0
```

### 查看进度

```bash
./taskctl.py list
./taskctl.py stat
./taskctl.py fail
./taskctl.py timeout
```

### 重新生成报告

```bash
./taskctl.py report
```

### 停止 worker

在 worker 终端按一次：

```text
Ctrl+C
```

worker 会停止 pull 新任务，并最多等待 30 秒让当前 example 收尾。
