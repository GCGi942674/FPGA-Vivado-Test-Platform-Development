# PJTest HTTP 分布式测试系统

> Updated: 2026-06-10  
> 适用版本：`scheduler.py` / `taskctl.py` / `worker.py`

PJTest 是 GalaxCore 的 HTTP 分布式测试框架。当前采用：

```text
pudong 中心调度 + worker 主动拉取 example + SQLite 保存状态 + Share 按任务生成报告
```

核心原则：

```text
pudong 负责状态、调度、汇总、报告
worker 负责执行，不直接访问数据库
每个 worker slot 是一个独立 GalaxCore SVN working copy
worker 跑完后先保存 pending report，再上报 scheduler
```

---

## 1. 固定路径

| 项目 | 路径 |
|---|---|
| 项目目录 | `/home/user3/PJTest` |
| taskctl | `/home/user3/PJTest/taskctl.py` |
| scheduler | `/home/user3/PJTest/scheduler.py` |
| worker | `/home/user3/PJTest/worker/worker.py` |
| 数据库 | `/home/user3/PJTest/data/task_queue.db` |
| 数据库初始化脚本 | `/home/user3/PJTest/DB/init_db.py` |
| 模板目录 | `/home/user3/PJTest/templates` |
| GalaxCore zip 目录 | `/home/xshare/zw_cache/distributed_test_system/GalaxCore_bin/zip` |
| Share 根目录 | `/home/xshare/zw_cache/distributed_test_system` |
| Share 报告根目录 | `/home/xshare/zw_cache/distributed_test_system/reports` |
| scheduler 日志目录 | `/home/user3/PJTest/logs/scheduler` |
| worker 日志目录 | `/home/user3/PJTest/logs` |
| pending report 缓存目录 | `/home/user3/PJTest/pending_reports` |
| worker slot 根目录 | `/home/user3/PJTest/worker_slots` |
| slot SVN 地址 | `http://192.168.10.10/svn/galaxcore/galaxcore` |

Share 根目录建议只保留：

```text
/home/xshare/zw_cache/distributed_test_system/
├── GalaxCore_bin/
│   └── zip/
└── reports/
```

根目录不再放这些旧文件：

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

## 2. 推荐目录结构

当前 `/home/user3/PJTest` 推荐结构：

```text
/home/user3/PJTest/
├── taskctl.py
├── scheduler.py
├── templates/
│   ├── default.json
│   ├── place.json
│   ├── route.json
│   └── checksum.json
├── data/
│   └── task_queue.db
├── db/
│   └── init_db.py
├── worker/
│   └── worker.py
├── logs/
│   ├── scheduler/
│   └── 202606xx/
├── pending_reports/
└── worker_slots/
```

`worker_slots` 会非常大。长期建议迁到大盘或单独 runtime 目录，例如：

```bash
export PJTEST_WORKER_SLOT_ROOT=/home/user3/PJTest_runtime/worker_slots
```

---

## 3. 框架职责

### 3.1 pudong / scheduler.py

`scheduler.py` 是唯一的状态中心，负责：

```text
1. 接收 worker 注册和心跳
2. 从数据库选择 pending example
3. 每次只分配一个 example 给 worker slot
4. 根据最新 GalaxCore zip 解析版本
5. 接收 worker 执行结果
6. 更新 tasks / task_examples / task_attempts
7. 汇总大任务状态
8. 自动刷新 reports/r<revision>/<task_id>/ 下的报告
9. 对 SQLite database locked 进行 retry
10. 对 stale worker 做保守标记，不默认误杀 running example
```

scheduler 不负责执行测试，不 SSH 到 worker。

### 3.2 taskctl.py

`taskctl.py` 是 pudong 上的任务管理工具，负责：

```text
1. 添加大任务并扫描 run.tcl
2. 查询 task / example / attempt / worker 状态
3. cancel / delete 任务
4. 查看 stale running
5. diagnose 诊断 DB/worker 一致性
6. repair-example 手动修复单个 example
7. 手动重新生成 task 报告
```

报告自动刷新由 scheduler 完成；`taskctl.py report` 用于手动补生成或重新生成。

### 3.3 worker.py

`worker.py` 是执行器，负责：

```text
1. 默认启动 8 个 slot
2. 每个 slot 首次启动时 svn co 一份完整 galaxcore 到自己的 slot 目录
3. 已有 slot 默认直接复用，不重复 checkout
4. 启动和每次 pull 前先 flush pending_reports
5. 空闲时主动向 scheduler pull 一个 example
6. 按 scheduler 下发的 zip_path 覆盖 slot 内 GalaxCore
7. 按任务 flow_config 修改 slot 内 test2/flow_config
8. 执行前先调用 slot/test2/clean.sh 清理残留
9. 执行 slot/test2/run.sh <run_tcl_path>
10. 读取 vivado_runner/runtime/status/.../result.env 判断真实 PASS/FAIL/TIMEOUT
11. 先把 report payload 保存到 pending_reports
12. 回传 success / failed / timeout / log 信息
13. 上报成功后删除 pending report
14. 上报失败时保持 reporting 状态并重试，不继续接新任务
15. 执行后再次调用 clean.sh 清理
```

worker 不直接访问 SQLite。

---

## 4. 数据库结构设计

数据库拆成：

```text
tasks
task_examples
task_attempts
workers
task_events
```

这样可以把“用户提交的大任务”、“真正分配给 worker 的小例子”、“worker 实际执行记录”分开保存。

### 4.1 tasks：大任务表

`tasks` 表表示用户提交的一次大任务，例如：

```bash
./taskctl.py add place .
```

这个命令不会让一台 worker 直接跑完整个 `.`，而是生成一个大任务，并拆成多个 example。

主要字段：

```text
task_id
task_name
template_name
revision / revision_policy / resolved_zip_path
target_dir
priority
max_retry
max_time
total_examples
created_at / started_at / finished_at
status
message
```

### 4.2 task_examples：小例子表

`task_examples` 表表示大任务拆出来的每一个可执行 example。

例如：

```text
kintexuplus/xcku3p-ffvd900-1-i/asym_ram_sdp_8k_16/run.tcl
kintexuplus/xcku3p-ffvd900-1-i/asym_ram_tdp_16k_32/run.tcl
```

worker 每次只拿一个 example。

example 状态：

```text
pending / running / success / failed / timeout / canceled
```

主要字段：

```text
example_id
task_id
seq
target_arg
run_tcl_path
assigned_worker
current_attempt_id
retry_count / max_retry
exit_code
failed_reason
log_file
run_log_dir
message
```

### 4.3 task_attempts：执行记录表

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

### 4.4 workers：工作机状态表

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

worker 状态常见值：

```text
idle
running
reporting
error
stopping
offline
```

### 4.5 task_events：事件日志表

`task_events` 表保存调度和状态变化事件，例如：

```text
task_created
example_claimed
example_success
example_failed
example_timeout
example_requeued
task_status_changed
task_canceling
task_canceled
worker_stale
manual_repair
task_deleted
```

---

## 5. 调度策略

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
2. 大任务 pending / running 可以继续分发
3. canceling / canceled / success / failed 的任务不会继续分发
4. priority 高的任务优先
5. priority 一样时，先创建的任务优先
6. 同一个大任务内按 seq 顺序分发
7. target_worker = any 时任意 worker 可拿
8. 指定 target_worker 时只有对应 worker 可拿
```

如果第一个大任务只剩最后几个 example 正在跑，其它空闲 worker 不会等待它结束，而是会继续拿后面任务的 pending example。这是吞吐优先策略。

如果后面要严格阶段顺序，例如：

```text
place 完成后才能 checksum
checksum 完成后才能 route
```

还需要继续增加 task dependency 或 strict queue 策略。

---

## 6. 初始化数据库

首次部署或需要清空重建时，在 pudong 执行：

```bash
cd /home/user3/PJTest
./db/init_db.py --reset
```

`--reset` 会清空所有 task / example / attempt / worker / event 数据。

保留旧数据时执行：

```bash
cd /home/user3/PJTest
./db/init_db.py
```

如果你的实际文件还在项目根目录，也可以用：

```bash
./db/init_db.py --reset
```

reset 前建议备份：

```bash
cd /home/user3/PJTest
mkdir -p backup
sqlite3 data/task_queue.db ".backup 'backup/task_queue_$(date +%F_%H%M%S).db'"
```

---

## 7. 启动 scheduler

### 7.1 前台启动

在 pudong 上启动：

```bash
cd /home/user3/PJTest
./scheduler.py --host 0.0.0.0 --port 8888 --worker-timeout 600
```

如果要显式指定 zip 目录：

```bash
cd /home/user3/PJTest
export GALAXCORE_ZIP_DIR=/home/xshare/zw_cache/distributed_test_system/GalaxCore_bin/zip
./scheduler.py --host 0.0.0.0 --port 8888 --worker-timeout 600
```

检查 scheduler：

```bash
curl http://192.168.10.11:8888/api/health
```

正常返回类似：

```json
{"ok": true, "service": "scheduler"}
```

### 7.2 tmux 后台启动

推荐用 tmux：

```bash
cd /home/user3/PJTest

tmux new-session -d -s pjtest_scheduler \
'cd /home/user3/PJTest && ./scheduler.py --host 0.0.0.0 --port 8888 --worker-timeout 600'
```

查看：

```bash
tmux attach -t pjtest_scheduler
```

退出但不停止：

```text
Ctrl+b 然后按 d
```

停止：

```bash
tmux kill-session -t pjtest_scheduler
```

---

## 8. 启动 worker

worker 默认启动 8 个 slot。

### 8.1 通用参数

常用参数：

```bash
./worker/worker.py \
  --scheduler http://192.168.10.11:8888 \
  --worker-name tiger \
  --jobs 8 \
  --shell csh \
  --shutdown-timeout 0
```

`--shutdown-timeout 0` 表示收到停止信号后一直等当前 example 跑完并上报，不主动杀测试。

### 8.2 tiger

```csh
cd /home/user3/PJTest
setenv SCHEDULER_URL http://192.168.10.11:8888
setenv WORKER_NAME tiger
setenv SHARE_ZIP_DIR /home/xshare/zw_cache/distributed_test_system/GalaxCore_bin/zip
setenv GALAXCORE_IGNORE_RUN_RC 0
setenv PJTEST_REPORT_MAX_RETRIES 0
mkdir -p /home/user3/PJTest/tmp
./worker/worker.py --jobs 8 --shell csh --shutdown-timeout 0
```

### 8.3 kangqiao

```csh
cd /home/user3/PJTest
setenv SCHEDULER_URL http://192.168.10.11:8888
setenv WORKER_NAME kangqiao
setenv SHARE_ZIP_DIR /home/xshare/zw_cache/distributed_test_system/GalaxCore_bin/zip
setenv GALAXCORE_IGNORE_RUN_RC 0
setenv PJTEST_REPORT_MAX_RETRIES 0
mkdir -p /home/user3/PJTest/tmp
./worker/worker.py --jobs 8 --shell csh --shutdown-timeout 0
```

### 8.4 yangpu

```csh
cd /home/user3/PJTest
setenv SCHEDULER_URL http://192.168.10.11:8888
setenv WORKER_NAME yangpu
setenv SHARE_ZIP_DIR /home/xshare/zw_cache/distributed_test_system/GalaxCore_bin/zip
setenv GALAXCORE_IGNORE_RUN_RC 0
setenv PJTEST_REPORT_MAX_RETRIES 0
mkdir -p /home/user3/PJTest/tmp
./worker/worker.py --jobs 8 --shell csh --shutdown-timeout 0
```

### 8.5 pudong 作为补充 worker

pudong 可以跑 worker，但不建议直接开满 8 个 slot。建议从 1 到 2 个开始：

```bash
cd /home/user3/PJTest
./worker/worker.py --worker-name pudong --jobs 1 --shell csh --shutdown-timeout 0
```

或者降低优先级：

```bash
cd /home/user3/PJTest
ionice -c2 -n7 nice -n 10 ./worker/worker.py --worker-name pudong --jobs 2 --shell csh --shutdown-timeout 0
```

### 8.6 tmux 后台启动 worker

pudong 本机：

```bash
cd /home/user3/PJTest

tmux new-session -d -s pjtest_worker_pudong \
'cd /home/user3/PJTest && ./worker/worker.py --scheduler http://192.168.10.11:8888 --worker-name pudong --jobs 2 --shell csh --shutdown-timeout 0'
```

其他机器示例，tiger：

```bash
cd /home/user3/PJTest

tmux new-session -d -s pjtest_worker_tiger \
'cd /home/user3/PJTest && ./worker/worker.py --scheduler http://192.168.10.11:8888 --worker-name tiger --jobs 8 --shell csh --shutdown-timeout 0'
```

查看：

```bash
tmux ls
tmux attach -t pjtest_worker_tiger
```

停止：

```bash
tmux kill-session -t pjtest_worker_tiger
```

---

## 9. Worker slot 目录和 SVN checkout

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
./worker/worker.py --update-test2 --worker-name tiger --jobs 8
```

如果需要重拉当前机器所有 slot：

```bash
cd /home/user3/PJTest
./worker/worker.py --recheckout-slots --worker-name tiger --jobs 8
```

只跑 1 个 slot 调试：

```bash
./worker/worker.py --jobs 1 --once --dump-json --verbose-console
```

---

## 10. Worker 安全退出

按一次 `Ctrl+C` 不是立即强杀测试，而是请求 worker 进程安全退出。

当前逻辑：

```text
Ctrl+C
    ↓
worker 停止 pull 新任务
    ↓
正在运行的 example 继续收尾
    ↓
跑完后先保存 pending report
    ↓
上报 scheduler 成功后退出
```

默认：

```bash
./worker/worker.py --shutdown-timeout 30
```

如果想等待更久：

```bash
./worker/worker.py --shutdown-timeout 600
```

如果想一直等当前 example 跑完：

```bash
./worker/worker.py --shutdown-timeout 0
```

不要使用 `kill -9`。如果必须杀进程，至少要检查 `pending_reports` 和 DB 中的 stale running。

---

## 11. Pending report 机制

为了解决 `REPORT_FAIL database is locked` 后结果丢失的问题，worker 使用本地 pending report 缓存。

默认目录：

```text
/home/user3/PJTest/pending_reports/<worker_slot>/
```

每个结果一个 JSON：

```text
/home/user3/PJTest/pending_reports/yangpu_slot6/ex_xxx_att_yyy.json
```

流程：

```text
1. worker 执行完 example
2. 生成 report payload
3. 先写 pending_reports JSON
4. 尝试 POST /api/task/report
5. 成功后删除 JSON
6. 失败则保持 reporting 状态并重试
7. worker 启动后会先 flush 自己 slot 的 pending_reports
```

相关环境变量：

```bash
export PJTEST_PENDING_REPORT_ROOT=/home/user3/PJTest/pending_reports
export PJTEST_REPORT_RETRY_INTERVAL=10
export PJTEST_REPORT_MAX_RETRIES=0
```

csh：

```csh
setenv PJTEST_PENDING_REPORT_ROOT /home/user3/PJTest/pending_reports
setenv PJTEST_REPORT_RETRY_INTERVAL 10
setenv PJTEST_REPORT_MAX_RETRIES 0
```

`PJTEST_REPORT_MAX_RETRIES=0` 表示一直重试。

查看是否有未补报结果：

```bash
find /home/user3/PJTest/pending_reports -name '*.json' -type f -print
```

如果这里长期有文件，说明某些结果还没有成功回传 scheduler。

---

## 12. Worker 执行时注入的环境变量

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

统计的是 example 是否真正 PASS，而不是 `run.sh` 外壳是否执行结束。

---

## 13. 添加任务

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

选择最大的 `GalaxCore_xxxxx.zip` 版本，并把 task 的 `revision` 锁定成实际版本号。

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

## 14. 查看任务状态

查看大任务：

```bash
./taskctl.py list
```

`list` 中：

```text
RUNNING = workers 表中当前真实匹配的 running/reporting example 数
STALE   = task_examples 中还是 running，但 worker 当前已经不对应它的残留 example 数
```

如果 `STALE > 0`，说明 DB 里有疑似残留 running，需要用 `stale` 或 `diagnose` 继续查。

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
./taskctl.py workers --status reporting
```

检查环境：

```bash
./taskctl.py check --scheduler http://192.168.10.11:8888
```

---

## 15. 按状态筛选 example

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

## 16. Stale running / diagnose / repair

### 16.1 查看 stale running

查看所有任务的 stale running：

```bash
./taskctl.py stale
```

查看某个任务：

```bash
./taskctl.py stale task_xxxxxxxxxxxx
```

详细查看：

```bash
./taskctl.py stale task_xxxxxxxxxxxx -v
```

`stale` 的含义：

```text
example.status = running
但是 workers 表中没有对应 worker 正在跑这个 example/attempt
```

常见原因：

```text
1. worker 曾经 REPORT_FAIL，旧版本没有补报
2. worker 进程被 kill -9
3. 机器重启
4. scheduler heartbeat 期间 database locked，worker 被标记 offline
```

### 16.2 诊断一致性

诊断全部任务：

```bash
./taskctl.py diagnose
```

诊断某个任务：

```bash
./taskctl.py diagnose task_xxxxxxxxxxxx
```

重点看：

```text
Active workers
DB running examples
Stale running examples
Orphan running attempts
```

### 16.3 手动修复单个 example

把 example 修成 timeout：

```bash
./taskctl.py repair-example ex_xxxxxxxxxxxx --status timeout --exit-code 124
```

把 example 修成 failed：

```bash
./taskctl.py repair-example ex_xxxxxxxxxxxx --status failed --exit-code 134
```

把 example 修成 success：

```bash
./taskctl.py repair-example ex_xxxxxxxxxxxx --status success --exit-code 0
```

如果 example 已经是终态但你确认要覆盖：

```bash
./taskctl.py repair-example ex_xxxxxxxxxxxx --status timeout --exit-code 124 --force
```

建议修复前后各查一次：

```bash
./taskctl.py attempts ex_xxxxxxxxxxxx -v
./taskctl.py examples task_xxxxxxxxxxxx --status running | grep ex_xxxxxxxxxxxx
./taskctl.py timeout task_xxxxxxxxxxxx | grep ex_xxxxxxxxxxxx
```

---

## 17. 报告文件

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
├── status_summary
├── list_pass_to_run
├── list_fail_to_run
└── timeout_list
```

当前保留 5 个报告文件：

```text
stat_summary
status_summary
list_pass_to_run
list_fail_to_run
timeout_list
```

### 17.1 stat_summary

总体统计文件，用来快速看总数和进度。

包含：

```text
generated_at
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
created_at
updated_at
started_at
finished_at
message
```

### 17.2 status_summary

完整状态表，用来复盘每个 example。

包含：

```text
SEQ
EXAMPLE_ID
STATUS
WORKER
RETRY
EXIT
TARGET_ARG
LOG_FILE
MESSAGE
```

### 17.3 list_pass_to_run

只列成功的 example。

### 17.4 list_fail_to_run

只列 failed 的 example，不包含 timeout。

### 17.5 timeout_list

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

## 18. 取消和删除任务

### 18.1 安全取消

安全取消任务：

```bash
./taskctl.py cancel task_xxxxxxxxxxxx
```

默认行为：

```text
pending examples -> canceled
running examples -> 不动，等待 worker 自然跑完并上报
task status -> canceling
scheduler 不再继续分发该 task 的 pending example
```

适合你想停止一个大任务继续扩散，但不想破坏已经在跑的 example。

### 18.2 强制取消

强制取消 running 任务：

```bash
./taskctl.py cancel task_xxxxxxxxxxxx --force
```

行为：

```text
pending + running examples -> canceled
running attempts -> canceled
task status -> canceled
```

注意：`--force` 只是改 DB，不一定能杀掉 worker 机器上已经启动的 run.sh。worker 进程仍可能继续跑到结束，所以除非确认要废弃这批结果，否则优先用普通 cancel。

### 18.3 删除任务

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

## 19. HTTP 通信流程

```text
worker slot
  启动后 flush pending_reports
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
  保存 pending report JSON
    ↓
worker slot
  POST /api/task/report
    ↓
scheduler
  更新 example / attempt / task 状态
  刷新 reports/r<revision>/<task_id>/ 下的报告
    ↓
worker slot
  report 成功后删除 pending report JSON
```

---

## 20. 数据库锁和高并发注意事项

当前 scheduler 已经做了：

```text
1. SQLite busy_timeout 提高
2. database is locked 自动 retry
3. WAL 只在 scheduler 启动时初始化
4. worker report 失败后不接新任务，而是保持 retry
5. reconcile 默认不因为 heartbeat 超时就 requeue/fail running example
```

仍然要注意：SQLite 是单写者模型，高并发下不可能完全没有锁竞争。

建议启动 scheduler 时：

```bash
export PJTEST_SQLITE_BUSY_TIMEOUT_MS=60000
export PJTEST_DB_LOCK_RETRIES=8
export PJTEST_DB_LOCK_RETRY_BASE_SEC=0.25
export PJTEST_REQUEUE_STALE_RUNNING=0
```

`PJTEST_REQUEUE_STALE_RUNNING=0` 是保守模式。它只把 worker 标记 offline，不自动重跑/失败它持有的 running example。这样可以避免因为暂时 heartbeat 写 DB 失败而误伤真实正在跑的测试。

如果确认 worker 机器死了，使用：

```bash
./taskctl.py stale
./taskctl.py diagnose
./taskctl.py repair-example ex_xxx --status timeout --exit-code 124
```

---

## 21. 查看 setenv 环境变量

### 21.1 查看当前终端变量

```csh
printenv SCHEDULER_URL
printenv WORKER_NAME
printenv SHARE_ZIP_DIR
printenv GALAXCORE_IGNORE_RUN_RC
printenv PJTEST_REPORT_MAX_RETRIES
```

一次性筛选：

```csh
printenv | egrep 'SCHEDULER_URL|WORKER_NAME|SHARE_ZIP_DIR|GALAXCORE_IGNORE_RUN_RC|PJTEST_REPORT'
```

或者：

```csh
env | egrep 'SCHEDULER_URL|WORKER_NAME|SHARE_ZIP_DIR|GALAXCORE_IGNORE_RUN_RC|PJTEST_REPORT'
```

### 21.2 判断变量是否存在

csh 里如果变量不存在，直接 `echo $VAR` 可能报 `Undefined variable`。推荐：

```csh
if ($?SCHEDULER_URL) then
    echo $SCHEDULER_URL
else
    echo "SCHEDULER_URL not set"
endif
```

### 21.3 查看已启动 worker 的环境变量

先找 PID：

```bash
ps -ef | grep '[w]orker.py'
```

假设 PID 是 `12345`：

```bash
tr '\0' '\n' < /proc/12345/environ | egrep 'SCHEDULER_URL|WORKER_NAME|SHARE_ZIP_DIR|GALAXCORE_IGNORE_RUN_RC|PJTEST_REPORT'
```

注意：worker 已经启动后，再执行 `setenv` 不会影响正在运行的 worker。必须重启 worker。

---

## 22. 常见问题

### 22.1 `./worker.py` 被当成 shell 脚本执行

如果看到：

```text
import: command not found
syntax error near unexpected token
```

检查第一行：

```bash
head -n 3 /home/user3/PJTest/worker/worker.py
```

第一行必须是：

```bash
#!/usr/bin/env python3
```

前面不能有空行。

然后确认权限：

```bash
chmod +x /home/user3/PJTest/worker/worker.py
```

### 22.2 worker 几秒钟就 success

这通常说明 `run.sh` 没真正跑起来，或者返回码被忽略。

确认：

```csh
setenv GALAXCORE_IGNORE_RUN_RC 0
```

然后用调试模式：

```bash
./worker/worker.py --jobs 1 --once --dump-json --verbose-console
```

重点看：

```text
cmd
log_file
target_arg
run_tcl_path
result.env
```

### 22.3 统计全 success，但实际有失败

worker 必须解析 `result.env` 后再 report。正确逻辑是：

```text
STATUS=PASS     -> success
STATUS=FAIL     -> failed
STATUS=TIMEOUT  -> timeout
```

旧 worker 如果只按 `run.sh` 返回码判断，可能会把实际失败误报为 success。升级 worker 后重新跑任务即可。

### 22.4 同一个 example 跑了两遍

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

### 22.5 Share 根目录旧报告文件还存在

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

### 22.6 `database is locked`

偶发 heartbeat locked 可以观察，不一定影响测试。

危险的是：

```text
REPORT_FAIL database is locked
```

新 worker 会进入 `REPORT_RETRY`，不会继续接新任务。查看 worker 日志：

```bash
grep -R "REPORT_RETRY\|REPORT_OK_AFTER_RETRY\|REPORT_FAIL" /home/user3/PJTest/logs 2>/dev/null
```

如果一直失败：

```bash
./taskctl.py workers
find /home/user3/PJTest/pending_reports -name '*.json' -type f -print
```

### 22.7 `STALE` 不为 0

先查：

```bash
./taskctl.py stale -v
./taskctl.py diagnose
```

如果确认该 example 实际 timeout：

```bash
./taskctl.py repair-example ex_xxx --status timeout --exit-code 124
```

如果确认失败：

```bash
./taskctl.py repair-example ex_xxx --status failed --exit-code 134
```

---

## 23. 推荐日常流程

### 23.1 启动 pudong scheduler

```bash
cd /home/user3/PJTest

tmux new-session -d -s pjtest_scheduler \
'cd /home/user3/PJTest && ./scheduler.py --host 0.0.0.0 --port 8888 --worker-timeout 600'
```

### 23.2 启动 worker

```bash
cd /home/user3/PJTest

tmux new-session -d -s pjtest_worker_tiger \
'cd /home/user3/PJTest && ./worker/worker.py --scheduler http://192.168.10.11:8888 --worker-name tiger --jobs 8 --shell csh --shutdown-timeout 0'
```

### 23.3 添加任务

```bash
cd /home/user3/PJTest
./taskctl.py add place . --max-retry 0
```

### 23.4 查看进度

```bash
./taskctl.py list
./taskctl.py stat
./taskctl.py fail
./taskctl.py timeout
./taskctl.py stale
./taskctl.py diagnose
```

### 23.5 重新生成报告

```bash
./taskctl.py report
```

### 23.6 停止 worker

进入 tmux：

```bash
tmux attach -t pjtest_worker_tiger
```

按一次：

```text
Ctrl+C
```

worker 会停止 pull 新任务，并等待当前 example 跑完、保存 pending report、上报成功后退出。

---

## 24. 更新/替换脚本后的验证

如果使用 `fullverified` 版本替换：

```bash
cp taskctl_fullverified_20260610.py /home/user3/PJTest/taskctl.py
cp scheduler_fullverified_20260610.py /home/user3/PJTest/scheduler.py
cp worker_fullverified_20260610.py /home/user3/PJTest/worker/worker.py

chmod +x /home/user3/PJTest/taskctl.py
chmod +x /home/user3/PJTest/scheduler.py
chmod +x /home/user3/PJTest/worker/worker.py
```

验证关键功能是否存在：

```bash
grep -n "def cmd_stale\|def cmd_diagnose\|def cmd_repair_example\|STALE\|canceling" /home/user3/PJTest/taskctl.py
grep -n "status_summary\|build_status_summary" /home/user3/PJTest/scheduler.py
grep -n "PENDING_REPORT_ROOT\|save_pending_report\|flush_pending_reports" /home/user3/PJTest/worker/worker.py
```

检查语法：

```bash
python3 -m py_compile /home/user3/PJTest/taskctl.py /home/user3/PJTest/scheduler.py /home/user3/PJTest/worker/worker.py
```

检查运行状态：

```bash
./taskctl.py check --scheduler http://192.168.10.11:8888
./taskctl.py workers
./taskctl.py list
./taskctl.py diagnose
```
