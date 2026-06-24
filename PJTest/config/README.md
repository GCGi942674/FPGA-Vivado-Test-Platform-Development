# PJTest 配置说明

所有 `scheduler.py` 与 `taskctl.py` 共用的可调参数都集中在：

```text
config/pjtest.ini
```

修改 INI 后：

- `scheduler.py` 需要重启后生效；
- `taskctl.py` 每次执行都会重新读取配置；
- 原有环境变量仍然具有最高优先级，适合临时覆盖配置。

## 配置分区

- `[paths]`：数据库、模板、工作目录、报告、日志、GalaxCore zip 路径。
- `[scheduler]`：监听地址、端口、scheduler URL、reconcile 周期、worker 超时、stale 自动恢复。
- `[database]`：SQLite 连接超时、busy timeout、锁重试。
- `[reports]`：报告保留天数、归档维护周期、Summary/Old 名称、attempt 报告数量。
- `[http]`：管理请求和健康检查超时、API 默认/最大返回数量。
- `[task_defaults]`：任务默认优先级、重试次数、超时和命令显示数量。
- `[scan]`：扫描 `run.tcl` 时忽略的目录。
- `[flow_config]`：新任务默认合并的 flow_config。

## 环境变量覆盖

仍支持原来的环境变量，例如 csh：

```csh
setenv PJTEST_DB_PATH /tmp/task_queue.db
setenv PJTEST_SHARE_REPORT_DIR /tmp/Reports
setenv PJTEST_REPORT_RETENTION_DAYS 7
setenv PJTEST_REQUEUE_STALE_RUNNING 1
```

也可以切换整份配置文件：

```csh
setenv PJTEST_CONFIG_FILE /path/to/custom_pjtest.ini
```

环境变量优先级高于 `config/pjtest.ini`。

## 注意

`[scheduler] port` 与 `[scheduler] url` 应保持一致。当前配置为：

```ini
port = 8888
url = http://192.168.10.11:8888
```
