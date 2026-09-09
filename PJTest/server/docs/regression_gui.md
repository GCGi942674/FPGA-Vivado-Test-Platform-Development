# Qt5 回归工作台（只读）

客户端提供分类、模块和最新结果版本筛选、用例搜索、分页历史、日志/重试摘录，以及全部匹配结果的 TXT 导出。没有修改、取消任务或重跑入口。

## 服务端更新

本次代码尚需同步到服务器并使用原有管理方式重启 scheduler；本地验证不代表远端超时已经解决。保留原数据库和配置。需要同步的文件（相对 PJTest）：

- `server/scheduler_core/main.py`
- `server/scheduler_core/task_queries.py`
- `server/regression_core/analyzer.py`
- `server/regression_core/service.py`
- `server/regression_core/http.py`

实际服务目录为 `/home/user3/PJTest/server`。之前的 LIMIT 优先于过滤的尝试及临时诊断日志已被替换：现在先筛选并选出任务页，再只统计该页任务的用例。不能整体回退 main.py，否则新增查询接口也会被撤销。

主数据库不需要迁移。首次访问回归接口时，服务会创建独立缓存 `/home/user3/PJTest/data/regression_view.db` 及 SQLite 的 WAL/SHM 文件。缓存目录必须允许 scheduler 用户写入，并位于本机磁盘。可在启动服务前用 csh 配置其他缓存路径：

```csh
setenv PJTEST_REGRESSION_CACHE_PATH /absolute/local/path/regression_view.db
```

更新服务后验证：

```csh
curl -q --noproxy '*' --max-time 30 'http://192.168.10.11:8888/api/tasks?limit=1'
curl -q --noproxy '*' --max-time 30 'http://192.168.10.11:8888/api/regression/status'
```

首次构建缓存可能需要较长时间，状态接口和界面会显示同步状态；请等待 ready 后查询。若 tasks 仍超时，应继续检查运行中的服务代码和实际查询，不应仅增加客户端超时。

## 客户端启动

把 `PJTest/regression_gui.py` 复制到每位使用者可读的目录。无需访问 user3 的目录，也无需注册为 worker。在脚本所在目录运行：

```csh
python3 regression_gui.py --url http://192.168.10.11:8888
```

Linux 启动器会寻找 PyQt5 自带的 Qt，并在子进程中配置库路径，避免系统 Qt 5.8 抢先加载。必要时显式指定已验证的目录：

```csh
python3 regression_gui.py --url http://192.168.10.11:8888 --qt-root /usr/local/lib64/python3.6/site-packages/PyQt5/Qt5
```

需要 Python 3.6+、PyQt5、有效的 DISPLAY 和 xcb 环境。启动器不使用 `python -I`，不修改用户 shell 配置。导出在客户端选择目录，以 UTF-8 BOM TXT 保存全部筛选结果（最多十万条），不是仅当前页；请各自保存到自己的目录。

## 多人访问和统计口径

- 客户端只发 GET 请求。服务共用一个后台缓存构建器，每 60 秒增量同步，界面每 10 秒检查状态。完成且元数据未变化的任务不会重复扫描。
- 原任务数据库以只读连接访问；内部缓存写入使用事务，读者在更新过程中仍看到上一个已提交快照。更新失败保留旧快照并显示错误。
- 查询并发上限为 8，繁忙时返回 503；稍后刷新即可。客户端网络请求在后台运行。快照发生变化时重新加载，避免分页混合不同快照。
- 不存在多人编辑覆盖问题，因为本版没有编辑功能。TXT 导出使用原子保存，但多人指定同一共享文件名仍可能后写覆盖，应使用各自的导出路径。
- 只统计明确标记 `suite=daily_regression` 的数据。日期按任务提交日期归组，不是计划批次；“已提交任务完成”不能证明没有漏交模块。
- 同版本冲突保留为不稳定，超时等不能直接确认为功能回退。新增/恢复使用最近两个提交日期的可比较结果。主表版本筛选指最新综合结果版本，历史可逐页查看。
- 日志只展示数据库已有摘录：最终日志最多 16000 字符，最近五次尝试各最多 4000 字符，不读取任意服务器文件。
- 增量检测依赖父任务元数据更新；直接手改数据库中的用例而不更新父任务元数据，可能无法自动触发缓存刷新。

## 验证范围

本地包含任务分页查询、回归分类、并发快照、失败回滚、HTTP 只读接口和真实 Qt 控件/导出集成检查。测试使用本地模拟数据库，不是生产回归数据。

远端已验证独立 Qt 5.15.2 能显示窗口；完整新版服务和界面仍需在 CentOS 7 / Python 3.6 / SQLite 3.7.17 上联调验收。本地测试使用较新 Python/SQLite，语法兼容检查不能替代该验收。
