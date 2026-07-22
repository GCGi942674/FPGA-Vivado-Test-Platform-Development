# PJTest Repository Layout

PJTest uses one SVN repository with two self-contained roles:

```text
PJTest/
|-- server/
|-- worker/
|-- PROJECT_LAYOUT.md
|-- svn-ignore.txt
`-- svn-global-ignore.txt
```

The scheduler host checks out the complete trunk and therefore manages both
roles. A worker host checks out only `trunk/worker`; worker code does not import
anything from `server/` and does not access the scheduler SQLite database.

## Server Role

Stable entry points:

- `server/scheduler.py`: HTTP scheduler service.
- `server/taskctl.py`: task administration CLI.
- `server/clock_task.py`: scheduled daily task entry point.
- `server/find_first_fail.py`: revision-scan compatibility entry point.

Implementation packages:

- `server/scheduler_core/`: scheduler implementation. Keep the wrapper stable
  while splitting `main.py` into HTTP, repository, reconciliation, and report
  modules later.
- `server/task_core/`: task and report implementation. Keep `taskctl.py` stable
  while splitting `cli.py` into command modules later.
- `server/database/`, `server/config/`, and `server/templates/` are server-only.

## Worker Role

Stable entry points:

- `worker/worker.py`: worker service.
- `worker/start.sh` and `worker/stop.sh`: background lifecycle scripts.
- `worker/worker_core/`: worker implementation, ready to split into scheduler
  client, slot, artifact, runner, result, and report modules later.

The worker keeps GalaxCore slot checkouts under `worker/worker_slots` and keeps
its PID and lock files under `worker/tmp`. Both directories are created at
runtime and are not part of the PJTest SVN repository.

## SVN Checkout

Scheduler host, complete repository:

```bash
svn checkout svn://192.168.10.11/PJTest/trunk /home/user3/PJTest
```

Worker host, worker role only:

```bash
mkdir -p /home/user3/PJTest
svn checkout svn://192.168.10.11/PJTest/trunk/worker /home/user3/PJTest/worker
```

Runtime directories stay outside version control. Scheduler data and shared
worker output use `data`, `logs`, and `pending_reports` at the project root.
Worker-local state uses `worker/tmp` and `worker/worker_slots`.

Apply the supplied properties at the repository working-copy root before the
first commit:

```bash
svn propset svn:ignore -F svn-ignore.txt .
svn propset svn:global-ignores -F svn-global-ignore.txt .
svn propset svn:ignore -F worker/svn-ignore.txt worker
```

No role requires a shared Python package or a `PYTHONPATH` override.

## Startup

Server:

```bash
cd /home/user3/PJTest/server
python3 scheduler.py --check
python3 taskctl.py check
python3 scheduler.py
```

Worker:

```bash
cd /home/user3/PJTest/worker
python3 worker.py --check
bash start.sh
```
