# PJTest regression export

`regression-export` is the phase-one regression index. It reads completed
`daily_regression` task results from SQLite and atomically regenerates one
compact read-only summary per template. Manual and other task suites are
excluded.

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

The generated filenames use a common prefix and the template name:

```text
Regression_Summary_place_design.txt
Regression_Summary_report_timing_summary.txt
Regression_Summary_route_design.txt
Regression_Summary_route_design_from_place.txt
```

The exporter also writes one combined nightly regression file:

```text
Regression.txt
```

Each file contains only `CASE_PATH`, `S_VERSION`, and `F_VERSION`. The template
is encoded in the filename. Detailed case tables, internal identity hashes,
state, severity, and a combined summary are deliberately omitted. All files are
derived views; SQLite remains the source of truth and the files must not be
edited as task input.

`Regression.txt` compares the latest two task submission dates present in
completed `daily_regression` results. It contains only cases that were `PASS`
on the previous nightly run and `FAIL` on the current nightly run:

```text
CASE_PATH  TEMPLATE  S_VERSION  F_VERSION
```

A case missing from either night is not comparable. `TIMEOUT`, `FLAKY`,
`INCONCLUSIVE`, and infrastructure-only outcomes are not reported as confirmed
nightly regressions. Both nights must contain exactly one numeric revision for
that case; the two revisions may be equal. With fewer than two nightly dates,
the file contains only its header.

The daily task submitter marks every task configured in `task.yaml` with
`suite=daily_regression`. When one of those tasks becomes terminal, the
scheduler asynchronously regenerates all module files. The final nightly task to
finish therefore publishes the complete nightly view and `Regression.txt`.

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
`fsync`s them, and publishes each file with `os.replace`. After publishing the
current set, it removes legacy combined files and stale module summaries.
