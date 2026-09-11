# PJTest Test Results: All cases / History / Compare

This native Qt5 client follows the supplied Figma layout. It uses real scheduler
records through HTTP; the React mock data and its trend algorithm are not deployed.

## Update files

Copy these server files to the corresponding paths under `/home/user3/PJTest/server`:

- `scheduler_core/main.py`
- `regression_core/http.py`
- `regression_core/results.py` (new)

Keep the existing `regression_core/service.py`, `identity.py` and other server
modules. Restart scheduler using its normal management method after updating.
No migration of `task_queue.db` is required.

The new service creates `results_view.db` beside `task_queue.db`. Its directory
must be writable by the scheduler user and should be on a local disk. Optionally
set a different local path before starting scheduler (csh/tcsh):

```csh
setenv PJTEST_RESULTS_CACHE_PATH /absolute/local/path/results_view.db
```

The original nightly TXT reports and old `/api/regression/` endpoints remain
separate. Their export lock does not control this cache.

Copy only `PJTest/regression_gui.py` to a shared readable directory.
The full GUI is embedded in this file; `results_ui.py` is no longer required
for deployment. Python and PyQt5 must still be installed on the client machine.
The client queries the scheduler API, not a shared SQLite file.

For the shared script directory, users of csh/tcsh can define:

```csh
alias regui 'python3 /home/xiaonan/Share/scripts/toolUnified/regression_gui.py'
```

Run `regui` to open a separate read-only window. The command can be distributed
through the site's existing shared shell configuration. This does not install
the alias into other users' active shells automatically.

```csh
python3 regression_gui.py --url http://192.168.10.11:8888
```

The existing Qt environment launcher is retained. If needed:

```csh
python3 regression_gui.py --url http://192.168.10.11:8888 --qt-root /usr/local/lib64/python3.6/site-packages/PyQt5/Qt5
```

## Check after restart

```csh
curl -q --noproxy '*' -sS --max-time 30 'http://192.168.10.11:8888/api/results/status'
curl -q --noproxy '*' -sS --max-time 30 'http://192.168.10.11:8888/api/results/matrix?limit=1'
```

First use starts a shared background cache build. `ready=false` means there is
no committed snapshot yet. `syncing`, `processed_tasks`, and `error` describe
progress. Subsequent updates run every 60 seconds. The GUI polls every 10 seconds.
Source reads are task-scoped; finished tasks are skipped when their metadata is
unchanged. Manual edits that do not update parent-task metadata are not detected.
Failed refreshes roll back and leave the prior snapshot available.

## Data meaning

- All cases includes daily and other tasks, including long-standing passes and
  failures. Only cases present in recorded tasks are known: it cannot list a
  planned case which has never been submitted.
- A row groups normalized workspace root and case path. Its stages remain
  separate. Different functional configurations in a stage remain separate and
  display `N configs`; details show their values rather than silently merging.
- Matrix latest results use task submission order, then example sequence. Each
  cell displays its own version/date; the row is not one synchronized batch.
- Date filters use execution start date when available, otherwise example/task
  creation date. Date comparison uses exact dates, without borrowing older data.
- Compare matches the same path, workspace, stage and configuration, then selects
  the latest task record on each selected date/version. Multiple runs are retained
  in History; comparison is between the selected final records, not proof of an
  exact regression boundary. Missing sides remain `No result`.
- Timeout, canceled, waiting and running remain distinct from ordinary failure.
  Compare uses `Other` for two present results outside the Pass/Fail combinations.
- Trends describe recorded final Pass/Fail sequences per stage/configuration.
  One transition gives New fail or Fixed; repeated transitions give Pass + Fail.
  The row shows the highest-priority stage trend (active/error/failing before
  passing), not a count of all failing stages. `All pass` describes observed
  terminal results and does not prove all planned stages completed.
- History's Recent runs uses letters (P/F/R/W/T/C/U), latest first; hover for full
  dates, versions and status. This is a short context preview across all dates.
- Duration is shown only when both start/end times can be read. Missing values
  are `-`. Failure reasons come from stored fields; missing reasons are not invented.

## Controls

Click a stage cell for its reason and recent history. Full history opens the
History page with that exact case, stage and source selected. Clear removes this
selection. More details shows IDs, configuration, retry count and paths as
selectable text. No remote filesystem is opened.

Column headers support matrix sorting; History supports Case, Stage, Date,
Version and Result; Compare supports Case, Stage and Result. Querying and sorting
happen on the server before paging. Save TXT exports all matches (up to 100000
rows), not just the current page. Matrix exports preserve configuration variants;
comparison exports include both selected records.

All clients are read-only. Up to eight API queries are admitted concurrently;
busy requests return 503. Each source/cache query has a bounded SQLite VM time
budget, but an OS-level blocked file read cannot be interrupted by that budget.
The cache writer uses a single transaction and readers retain a committed snapshot.
Client requests are asynchronous and obsolete responses are ignored. Exports are
saved locally using Qt atomic file saving. Close asks Yes/No, default No.

Status requests have two separate admission slots and read a prebuilt metadata
record; they no longer scan the runs table for filter choices at every poll.
Existing caches publish this metadata on the next successful background refresh.
Failed page loads retry after the next successful status poll even if the cache
generation has not changed. SQLite 503 errors include the query action and cause
in scheduler logs as well as the JSON error response. These changes require
updating regression_core/http.py, regression_core/results.py and results_ui.py,
then restarting scheduler and the client; do not delete the cache.

## Verification

Local checks include fixtures for every page, multiple sources/stages, conflicting
configurations, missing results, export scope, rollback and generation changes.
Qt checks drive real controls against a local HTTP server. Production data and
CentOS 7 / Python 3.6 / SQLite 3.7.17 still require server-side acceptance after
deployment. The local grammar check is not a substitute for that acceptance.
