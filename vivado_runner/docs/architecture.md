# Architecture

External files under `test2/`:
- `run.sh`: only entrypoint for execution
- `flow_config`: toggle module switches without entering internal config
- `clean.sh`: cleanup last run artifacts

Internal framework is under `test2/vivado_runner/`.
Runtime artifacts are organized into:
- `runtime/logs/`: runner and per-case logs
- `runtime/status/`: structured case result env files
- `runtime/reports/latest/`: most recent summaries
- `runtime/reports/archive/`: timestamped archives for local lookup
- `runtime/cache/`: discovery cache and helper files
