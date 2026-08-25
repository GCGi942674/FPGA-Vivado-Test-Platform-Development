# PJTest regression export

`regression-export` is the phase-one regression index. It reads completed
`daily_regression` task results from SQLite and atomically regenerates two
read-only files. Manual and other task suites are excluded.

## Usage

```bash
cd /home/user3/PJTest
./taskctl.py regression-export
```

The default output directory is:

```text
<report_root>/Summary/
```

An alternate output directory can be selected for inspection:

```bash
./taskctl.py regression-export --out /tmp/pjtest-regression
```

The generated files are:

```text
regression_cases.tsv
regression_summary.txt
```

`regression_cases.tsv` contains case/template, regression boundaries, latest
result, and update time. `regression_summary.txt` keeps only case/template and
the compact `sREV/fREV` display. Internal identity hashes, state, and severity
are deliberately omitted. Both files are derived views; SQLite remains the
source of truth and the files must not be edited as task input.

The daily task submitter marks every task configured in `task.yaml` with
`suite=daily_regression`. When one of those tasks becomes terminal, the
scheduler asynchronously regenerates both files. The final nightly task to
finish therefore publishes the complete nightly view.

## Test identity

One comparable test is identified from these normalized values:

```text
work_root
run_tcl_path
template_name
flow_config_json
```

Paths use `/`, JSON keys are sorted, and execution-only values such as task IDs,
attempt IDs, timestamps, worker names, and log paths are excluded from the
configuration hash.

## Included results

The exporter includes examples only when:

```text
task.status is success or failed
example.status is success, failed, or timeout
the resolved example/task revision is numeric
```

Canceled and unfinished work does not affect a regression boundary. A terminal
example carrying `infra_reason` is treated as infrastructure noise and does not
move PASS/FAIL boundaries.

Repeated results for the same `TEST_KEY + revision` are normalized before
analysis:

```text
all success                    -> PASS
all failed                     -> FAIL
all timeout                    -> TIMEOUT
PASS mixed with FAIL/TIMEOUT   -> FLAKY
FAIL mixed with TIMEOUT        -> INCONCLUSIVE
infra-only                     -> INFRA_ERROR
```

When a terminal example has terminal `task_attempts`, those attempts are used
so a failed attempt followed by a successful retry is visible as `FLAKY`. If an
example has no terminal attempts (for example, after a manual repair), its
final `task_examples.status` is used instead.

## Current phase-one states

```text
STABLE         latest valid history has no regression
OPEN           known PASS boundary followed by FAIL
NO_BASELINE    FAIL exists but no earlier known PASS exists
FIXED          the latest regression episode later returned to PASS
FLAKY          one revision has conflicting outcomes
INCONCLUSIVE   latest outcome cannot move a PASS/FAIL boundary
UNKNOWN        no classifiable outcome
```

Phase one stores only the latest regression episode in the generated view. Full
attempt history remains in SQLite. Historical event tables, dynamic bisection,
failure-signature grouping, and visualization are deliberately deferred.

## Safe generation

Only the central export command writes the files. It uses an exclusive export
lock, creates temporary files in the destination directory, flushes and
`fsync`s them, and publishes each file with `os.replace`.
