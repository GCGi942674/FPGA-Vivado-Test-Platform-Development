# Workspace runtime isolation

One `run.sh` and one `vivado_runner` installation can execute cases in different
physical working copies concurrently. Runtime state is separated by workspace;
SVN branch names are not used as identities.

## Workspace identity and runtime namespace

Set `GALAXCORE_WORKSPACE_ROOT` to the testcase working copy. When omitted, the
default remains the directory containing `run.sh` and `vivado_runner`.

The directory must exist. The runner resolves it to a real absolute path before
choosing runtime state, so a symlink and its target identify the same physical
workspace.

The automatic `WORKSPACE_ID` combines a readable name from the workspace's
parent directory with the first 16 hexadecimal characters of the SHA-256 hash
of the real workspace path. For example, two directories both named `test2`
receive different identities when their real absolute paths differ.

The default runtime directory is:

```text
<runner-directory>/vivado_runner/runtime/workspaces/<workspace_id>/
```

An optional `VIVADO_RUNNER_NAMESPACE` replaces only the runtime directory name:

```bash
GALAXCORE_WORKSPACE_ROOT=/workspace/branch_A/test2 \
VIVADO_RUNNER_NAMESPACE=branch_A_manual \
/home/user3/PJTest/run.sh /workspace/branch_A/test2/case1/run.tcl
```

A namespace must match `[A-Za-z0-9][A-Za-z0-9_.-]*` and must not contain `..`.
It is a single directory name, not a path. Assign distinct explicit namespaces
to different workspaces. A namespace records its workspace owner; attempting to
reuse it for another physical workspace is rejected. Leave the variable unset
for automatic isolation.

The automatic workspace ID is retained even when an explicit namespace is
selected. Manual-run locking uses that automatic ID, so selecting another
namespace does not bypass the same-workspace restriction.

## Run two working copies concurrently

The following example uses the same absolute runner path and two absolute case
paths. Replace these paths with existing directories and testcase files:

```bash
GALAXCORE_WORKSPACE_ROOT=/workspace/branch_A/test2 \
/home/user3/PJTest/run.sh /workspace/branch_A/test2/case1/run.tcl &
run_a=$!

GALAXCORE_WORKSPACE_ROOT=/workspace/branch_B/test2 \
/home/user3/PJTest/run.sh /workspace/branch_B/test2/case1/run.tcl &
run_b=$!

wait "$run_a"
wait "$run_b"
```

Each run has its own logs, status files, case list, PID map, worker outputs,
reports and cache directory. Interrupt and timeout handling use the run's own
namespace state.

The same physical workspace does not support concurrent runs, including runs
with different explicit namespaces. Testcase output files still occupy their
original directories. The existing PJTest slot-lock behavior and the
`RUN_SH_LOCK_HELD` parent-lock protocol are preserved.

## Paths and behavior that remain compatible

`GALAXCORE_WORKSPACE_ROOT` does not change the caller's current directory.
Directory arguments, direct `run.tcl` arguments and list-file arguments retain
their existing CLI resolution rules. Relative entries inside a case list remain
relative to `WORKSPACE_ROOT`; absolute entries are used directly. Use absolute
runner and testcase paths for concurrent invocations from a common directory.

The default `flow_config` remains the file beside the entrypoint `run.sh`.
Selecting another workspace does not automatically select that workspace's
`flow_config`. Use `--flow-config /absolute/path/to/flow_config` when a run needs
a different configuration.

The default GalaxCore binary path remains relative to the selected workspace.
The `--galaxcore` override, flow switches, result judgments, `--bg` and
`--timeout` retain their existing meanings.

Testcase `run`, `output.*`, `mis_*` and `.run_*` artifacts remain in the testcase
directory. Different workspaces must refer to separate writable testcase
directories for those artifacts to remain isolated.

## Inspection and cleanup

The startup log records the real workspace root, automatic workspace ID,
selected namespace and runtime directory. Execution report metadata carries the
same information. Inspect these paths to identify the state belonging to each
concurrent run.

The legacy `vivado_runner/runtime/status` search root stays a physical directory.
Result snapshots are published under its `<namespace>/` subdirectories with
atomic replacement and preserved timestamps. Existing recursive readers such
as PJTest can match a result by its RUN_TCL path. A private regression runner
still discovers its single current result. Pre-upgrade results are moved to
`vivado_runner/runtime/legacy/status.<timestamp>.<pid>/` before initialization.
Use the full workspace runtime when inspecting logs and reports.

`--clean-runtime` cleans the selected namespace's runtime state, including its
local reports and cache. It does not clean other workspace namespaces or move
testcase artifacts. Acquire the normal run lock before cleaning the selected
runtime.

The workspace's `timeout_list` remains under `WORKSPACE_ROOT`. Report copying to
a shared external `--report-dst` retains the existing revision/host/timestamp
grouping, with a namespace and unique suffix to prevent same-second collisions.
See [Report layout](report_format.md) for the exact paths.
