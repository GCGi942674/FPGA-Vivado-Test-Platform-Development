# Execution logs in Regression

Update and restart the scheduler and workers, then distribute the updated
`PJTest/regression_gui.py`. The scheduler creates the log table at startup.
The GUI remains a single-file launcher:

```sh
python3 regression_gui.py --help
```

Select a record and click **More details**. The Execution logs window offers
Run log and Flow config tabs, stage selection, search, Top, Bottom, and Save.
Run log opens at the end. Requests use the scheduler's existing HTTP connection.

Failed and timed-out reports include the worker's preserved evidence files.
Pending reports retain these contents for retry. Successful reports omit logs.
Historical records without uploads show an explicit unavailable message.
The existing evidence size limit still applies: large files contain head/tail
text and an explicit truncation marker. Missing files also show a message.

Storage is keyed by example ID; subsequent uploads replace the saved text.
Logs are queried on demand rather than included in matrix refresh responses.
No live execution streaming or historical backfill is included.
