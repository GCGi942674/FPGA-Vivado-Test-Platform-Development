# Architecture

External files under `test2/`:

- `run.sh`: only entrypoint for execution
- `flow_config`: toggle module switches without entering internal config
- `clean.sh`: cleanup last run artifacts

Internal framework is under `test2/vivado_runner/`.

`PROJECT_ROOT` locates the framework code. `WORKSPACE_ROOT` locates the testcase
working copy and comes from `GALAXCORE_WORKSPACE_ROOT`, or the framework's parent
directory when the variable is not set. The workspace must be an existing
directory and is resolved to its real absolute path before runtime paths are
created.

Each workspace has an automatic `WORKSPACE_ID`: a readable parent-directory name
followed by the first 16 hexadecimal characters of the SHA-256 hash of its real
absolute path. An optional `VIVADO_RUNNER_NAMESPACE` selects a different runtime
directory name without changing the automatic workspace identity.

Runtime artifacts are organized under the selected namespace:

```text
runtime/
    workspaces/
        <workspace_id-or-explicit-namespace>/
            logs/                 runner log
            status/               case result.env and captured run.log files
            tmp/                  case list, PID map, worker outputs and counters
            reports/
                latest/           current summaries
                archive/          timestamped report copies
            cache/                workspace-local helper cache directory
            exports/              default report copy destination
    status/
        <namespace>/              compatibility copies of result.env files
```

The legacy `runtime/status` search root remains a stable physical directory.
Each namespace publishes its own result.env snapshots there using atomic file
replacement and preserved timestamps. Existing recursive result readers can
find them without switching a shared symlink. Pre-upgrade results are preserved
under `runtime/legacy/` before the new layout is initialized.

Manual runs lock by the automatic workspace identity, so different working
copies can run concurrently and the same physical workspace remains exclusive.
The existing PJTest slot lock and `RUN_SH_LOCK_HELD` protocol remain supported.
Changing an explicit runtime namespace does not enable concurrent execution in
one workspace.

Testcase artifacts such as `run`, `output.*`, `mis_*` and `.run_*` remain in their
original testcase directories. The default `flow_config` source and CLI case
discovery rules are unchanged. `--clean-runtime` cleans runtime state only for
the selected namespace.

See [Workspace runtime isolation](workspace_runtime.md) for configuration,
concurrent-run examples and compatibility details, and [Report layout](report_format.md)
for report paths.
