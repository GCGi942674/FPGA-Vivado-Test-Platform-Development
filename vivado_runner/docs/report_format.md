# Report layout

Latest reports are written to
`runtime/workspaces/<workspace_id-or-explicit-namespace>/reports/latest/`:

- `runTime_Summary`
- `list_fail_to_run`
- `stat_summary`
- `execution_report.txt`
- `execution_report.json`

Each run is also archived under
`runtime/workspaces/<workspace_id-or-explicit-namespace>/reports/archive/<timestamp>/`.
Both latest reports and archives belong to the selected workspace runtime.

The runner log and execution report metadata identify the real workspace path,
automatic workspace ID, selected runtime namespace and runtime directory. JSON
metadata uses `workspace_root`, `workspace_id`, `runtime_namespace` and
`runtime_dir` for those values.

Per-case `result.env` and captured `run.log` files live under the namespace's
`status/` directory. Compatibility copies of result.env are published under
`runtime/status/<namespace>/` for existing recursive readers. Their timestamps
are preserved so readers can reject stale results. Use the full workspace
runtime for logs. Pre-upgrade status results are preserved in
`runtime/legacy/status.<timestamp>.<pid>/`.

`timeout_list` remains under `WORKSPACE_ROOT`. It is not copied to the public
report destination.

With `--copy`, the default export destination is the namespace's `exports/`
directory. `--report-dst <dir>` continues to select an external destination. Its
revision grouping is retained, with namespace and a unique suffix added:

```text
<report-dst>/<svn_revision>/<host>_<YYYYMMDD_HHMMSS>_<namespace>_<unique>/
```

`last_result_path.txt` and `last_detail_path.txt` remain in the namespace's
`reports/latest/` directory and record the selected exported report paths.

See [Workspace runtime isolation](workspace_runtime.md) for concurrent-run
commands and namespace rules.
