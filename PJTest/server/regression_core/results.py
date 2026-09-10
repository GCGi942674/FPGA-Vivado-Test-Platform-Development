#!/usr/bin/env python3
"""Read-only result browser with a separate incremental SQLite cache."""
import csv
import hashlib
import io
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

from .identity import build_test_identity, normalize_path
from .service import ViewError, encode

STAGES = ['place_design', 'route_design', 'route_design_from_place',
          'report_timing_summary', 'report_utilization']
RESULTS = {'success': 'Pass', 'failed': 'Fail', 'timeout': 'Timeout',
           'running': 'Running', 'pending': 'Waiting', 'canceled': 'Canceled'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, fingerprint TEXT);
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, task_id TEXT, case_id TEXT, test_key TEXT, path TEXT,
 name TEXT, stage TEXT, version TEXT, day TEXT, source TEXT, result TEXT,
 rank INTEGER, seq INTEGER, data TEXT);
CREATE INDEX IF NOT EXISTS runs_task ON runs(task_id);
CREATE INDEX IF NOT EXISTS runs_case ON runs(case_id,stage,rank,seq);
CREATE INDEX IF NOT EXISTS runs_key ON runs(test_key,rank,seq);
CREATE INDEX IF NOT EXISTS runs_day ON runs(day,version,source);
CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY, data TEXT);
'''


class ResultsService:
    def __init__(self, source_path, cache_path=None):
        self.source_path = Path(source_path)
        self.cache_path = Path(cache_path or self.source_path.with_name('results_view.db'))
        if self.cache_path.resolve() == self.source_path.resolve():
            raise ValueError('Cache must differ from source database')
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.syncing, self.error, self.processed = False, '', 0
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(str(self.cache_path), timeout=2)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self.loop, name='results-view', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(5)

    def loop(self):
        while not self.stop.is_set():
            try:
                self.refresh()
                self.error = ''
            except Exception as exc:
                self.error = str(exc)
            self.stop.wait(60)

    def refresh(self):
        if not self.lock.acquire(False):
            return
        self.syncing, self.processed = True, 0
        src = None
        try:
            src = sqlite3.connect(self.source_path.resolve().as_uri() + '?mode=ro',
                                  uri=True, timeout=2, isolation_level=None)
            src.row_factory = sqlite3.Row
            tasks = src.execute('SELECT id,task_id,status,updated_at,finished_at,total_examples '
                                'FROM tasks ORDER BY id').fetchall()
            with self.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                old = self.metadata(c)
                seen, changed = set(), False
                for task in tasks:
                    if self.stop.is_set():
                        raise RuntimeError('Update stopped')
                    task_id = task['task_id']
                    seen.add(task_id)
                    fingerprint = encode(dict(task))
                    previous = c.execute('SELECT fingerprint FROM tasks WHERE task_id=?', (task_id,)).fetchone()
                    if previous and previous[0] == fingerprint and task['status'] in ('success', 'failed', 'canceled'):
                        continue
                    deadline = time.monotonic() + 20
                    src.set_progress_handler(lambda: int(self.stop.is_set() or time.monotonic() > deadline), 10000)
                    src.execute('BEGIN')
                    try:
                        t = dict(src.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
                        rows = src.execute('SELECT example_id,seq,run_tcl_path,status,revision,created_at,'
                                           'started_at,finished_at,assigned_worker,failed_reason,infra_reason,'
                                           'exit_code,log_file FROM task_examples WHERE task_id=?', (task_id,)).fetchall()
                        attempts = dict(src.execute('SELECT example_id,COUNT(*) FROM task_attempts '
                            'WHERE example_id IN (SELECT example_id FROM task_examples WHERE task_id=?) '
                            'GROUP BY example_id', (task_id,)).fetchall())
                    finally:
                        src.rollback()
                        src.set_progress_handler(None, 0)
                    c.execute('DELETE FROM runs WHERE task_id=?', (task_id,))
                    for row in rows:
                        r = dict(row)
                        key, config, path, _ = build_test_identity(t['work_root'], r['run_tcl_path'],
                                                                 t['template_name'], t['flow_config_json'])
                        case_id = hashlib.sha256((normalize_path(t['work_root']) + '\n' + path).encode('utf-8')).hexdigest()[:24]
                        name = PurePosixPath(path).parent.name if PurePosixPath(path).name == 'run.tcl' else PurePosixPath(path).stem
                        date = r['started_at'] or r['created_at'] or t['created_at'] or ''
                        result = RESULTS.get(r['status'], 'Unknown')
                        duration = None
                        if r['started_at'] and r['finished_at']:
                            try:
                                duration = max(0, int((datetime.strptime(r['finished_at'][:19], '%Y-%m-%d %H:%M:%S') -
                                                      datetime.strptime(r['started_at'][:19], '%Y-%m-%d %H:%M:%S')).total_seconds()))
                            except ValueError:
                                pass
                        r.update(case_id=case_id, test_key=key, config=config, path=path, name=name,
                                 stage=t['template_name'], version=str(r['revision'] or t['revision'] or ''),
                                 day=date[:10], date=date, source='daily' if t['suite'] == 'daily_regression' else 'other',
                                 result=result, task_id=task_id, duration=duration)
                        r['retries'] = max(0, attempts.get(r['example_id'],0)-1)
                        r['failed_reason'] = str(r['failed_reason'] or '')[:4096]
                        c.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                  (r['example_id'], task_id, case_id, key, path, name, r['stage'], r['version'],
                                   r['day'], r['source'], result, t['id'], r['seq'], encode(r)))
                    c.execute('INSERT OR REPLACE INTO tasks VALUES(?,?)', (task_id, fingerprint))
                    changed = True
                    self.processed += 1
                for row in c.execute('SELECT task_id FROM tasks').fetchall():
                    if row[0] not in seen:
                        c.execute('DELETE FROM runs WHERE task_id=?', (row[0],))
                        c.execute('DELETE FROM tasks WHERE task_id=?', (row[0],))
                        changed = True
                meta = dict(ready=True, generation=old.get('generation', 0) + int(changed or not old.get('ready')),
                            updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                # Build filter catalogs once per refresh, not on every status poll.
                meta.update(stages=list(dict.fromkeys(STAGES + [r[0] for r in c.execute('SELECT DISTINCT stage FROM runs ORDER BY stage')])),
                            versions=[r[0] for r in c.execute('SELECT DISTINCT version FROM runs ORDER BY CAST(version AS INTEGER) DESC,version DESC')],
                            dates=[r[0] for r in c.execute("SELECT DISTINCT day FROM runs WHERE day<>'' ORDER BY day DESC")])
                if 'stages' not in old and meta['generation']==old.get('generation',0):
                    meta['generation'] += 1
                c.execute('INSERT OR REPLACE INTO meta VALUES(1,?)', (encode(meta),))
                c.commit()
        finally:
            if src:
                src.close()
            self.syncing = False
            self.lock.release()

    @staticmethod
    def metadata(c):
        row = c.execute('SELECT data FROM meta WHERE id=1').fetchone()
        return json.loads(row[0]) if row else dict(ready=False, generation=0)

    @contextmanager
    def snapshot(self, query):
        with self.connect() as c:
            c.execute('BEGIN')
            meta = self.metadata(c)
            if query.get('generation') and int(query['generation']) != meta['generation']:
                raise ViewError('Data changed. Update again.', 409)
            deadline = time.monotonic() + 20
            c.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            yield c, meta

    def status(self):
        with self.snapshot({}) as (c, meta):
            if 'stages' not in meta:
                meta.update(ready=False,stages=STAGES,versions=[],dates=[])
        meta.update(syncing=self.syncing, error=self.error, processed_tasks=self.processed)
        return meta

    @staticmethod
    def conditions(q, alias='r', result=True):
        clauses, args = [], []
        for field, column in [('case_id','case_id'), ('stage','stage'), ('version','version'), ('source','source')]:
            if q.get(field):
                clauses.append(alias + '.' + column + '=?')
                args.append(q[field])
        if result and q.get('result'):
            clauses.append(alias + '.result=?')
            args.append(q['result'])
        for field, op in [('from','>='), ('to','<=')]:
            if q.get(field):
                clauses.append(alias + '.day' + op + '?')
                args.append(q[field])
        if q.get('q'):
            clauses.append("(" + alias + ".path LIKE ? ESCAPE '\\' OR " + alias + ".name LIKE ? ESCAPE '\\')")
            word = '%' + q['q'].replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'
            args.extend([word, word])
        return ' AND '.join(clauses) or '1=1', args

    @staticmethod
    def page(items, q, meta):
        offset = max(0, int(q.get('offset', 0)))
        limit = min(200, max(1, int(q.get('limit', 50))))
        return dict(items=items[offset:offset+limit], total=len(items), generation=meta['generation'])

    def latest(self, c, q):
        where, args = self.conditions(q, result=False)
        other, other_args = self.conditions(q, 'n', result=False)
        sql = ('SELECT r.data FROM runs r WHERE ' + where +
               ' AND NOT EXISTS (SELECT 1 FROM runs n WHERE n.test_key=r.test_key AND (' + other +
               ') AND (n.rank>r.rank OR (n.rank=r.rank AND n.seq>r.seq))) ORDER BY r.name,r.path,r.stage')
        return [json.loads(r[0]) for r in c.execute(sql, args + other_args)]

    def history(self, q):
        with self.snapshot(q) as (c, meta):
            where, args = self.conditions(q)
            total = c.execute('SELECT COUNT(*) FROM runs r WHERE ' + where, args).fetchone()[0]
            limit = 100001 if q.get('_export') else min(200, max(1, int(q.get('limit', 50))))
            offset = 0 if q.get('_export') else max(0, int(q.get('offset', 0)))
            sort = {'name':'r.name','stage':'r.stage','version':'CAST(r.version AS INTEGER)',
                    'result':'r.result','date':'r.rank'}.get(q.get('sort'), 'r.rank')
            direction = 'ASC' if q.get('order')=='asc' else 'DESC'
            rows = c.execute('SELECT data FROM runs r WHERE ' + where +
                             ' ORDER BY '+sort+' '+direction+',seq DESC LIMIT ? OFFSET ?', args + [limit, offset])
            items = [json.loads(r[0]) for r in rows]
            if not q.get('_export'):
                recent = {}
                for item in items:
                    if item['test_key'] not in recent:
                        recent[item['test_key']] = [dict(r) for r in c.execute(
                            'SELECT day,version,result FROM runs WHERE test_key=? ORDER BY rank DESC,seq DESC LIMIT 6',
                            (item['test_key'],))]
                    item['recent'] = recent[item['test_key']]
            return dict(items=items, total=total, generation=meta['generation'])

    def matrix(self, q):
        with self.snapshot(q) as (c, meta):
            scope = {k:v for k,v in q.items() if k not in ('stage','result')}
            groups = {}
            for r in self.latest(c, scope):
                item = groups.setdefault(r['case_id'], dict(case_id=r['case_id'], name=r['name'], path=r['path'], stages={}))
                item['stages'].setdefault(r['stage'], []).append(r)
            # Trends are computed per comparable stage/config, never by mixing stages.
            history_where, history_args = self.conditions(scope, result=False)
            past = {}
            for r in c.execute('SELECT r.test_key,r.result FROM runs r WHERE ' + history_where +
                               ' ORDER BY r.rank,r.seq', history_args):
                if r['result'] in ('Pass','Fail'):
                    past.setdefault(r['test_key'], []).append(r['result'])
            def trend(run):
                if run['result'] not in ('Pass','Fail'):
                    return run['result']
                values = past.get(run['test_key'], [])
                changes = sum(a != b for a,b in zip(values,values[1:]))
                if changes > 1:
                    return 'Pass + Fail'
                if changes == 1:
                    return 'New fail' if values[-1]=='Fail' else 'Fixed'
                return 'All pass' if values and values[-1]=='Pass' else 'All fail'
            priority = ['Running','Waiting','Timeout','Unknown','Pass + Fail','New fail','All fail','Fixed','Canceled','All pass']
            for item in groups.values():
                trends = [trend(r) for runs in item['stages'].values() for r in runs]
                item['trend'] = next((v for v in priority if v in trends), 'No result')
            items = list(groups.values())
            if q.get('trend'):
                items = [r for r in items if r['trend']==q['trend']]
            if q.get('result'):
                stages = [q['stage']] if q.get('stage') else list(dict.fromkeys(STAGES +
                    [r[0] for r in c.execute('SELECT DISTINCT stage FROM runs')]))
                def match(item):
                    for stage in stages:
                        runs = item['stages'].get(stage, [])
                        if q['result'] == 'No result' and not runs:
                            return True
                        if any(r['result'] == q['result'] for r in runs):
                            return True
                    return False
                items = [item for item in items if match(item)]
            sort = q.get('sort','name')
            def sort_key(item):
                primary = item['name'].lower() if sort=='name' else item['trend'] if sort=='trend' else ','.join(
                    sorted(r['result'] for r in item['stages'].get(sort,[])))
                return primary, item['path'], item['case_id']
            items.sort(key=sort_key, reverse=q.get('order') == 'desc')
            counts = {}
            for item in items:
                for values in item['stages'].values():
                    for r in values:
                        counts[r['result']] = counts.get(r['result'],0)+1
            result = dict(items=items, total=len(items), generation=meta['generation']) if q.get('_export') else self.page(items,q,meta)
            result['counts'] = counts
            return result

    def compare(self, q):
        field = 'version' if q.get('mode') == 'version' else 'day'
        if not q.get('left') or not q.get('right'):
            raise ViewError('Select both dates or versions.', 400)
        with self.snapshot(q) as (c, meta):
            sides = []
            for side in ('left','right'):
                scope = {k:v for k,v in q.items() if k not in ('result','version','from','to')}
                if field == 'day':
                    scope.update({'from':q[side], 'to':q[side]})
                else:
                    scope['version'] = q[side]
                sides.append({r['test_key']:r for r in self.latest(c, scope)})
            items = []
            universe_scope = {k:v for k,v in q.items() if k not in ('result','version','from','to')}
            universe = {r['test_key']:r for r in self.latest(c, universe_scope)}
            by_case = {}
            for r in universe.values():
                by_case.setdefault(r['case_id'], {})[r['stage']] = r
            for case_id, known in by_case.items():
                for stage in ([q['stage']] if q.get('stage') else STAGES):
                    if stage not in known:
                        sample = dict(next(iter(known.values())), stage=stage, config='')
                        universe[case_id+':missing:'+stage] = sample
            for key in set(universe) | set(sides[0]) | set(sides[1]):
                left, right = sides[0].get(key), sides[1].get(key)
                pair = (left['result'] if left else 'No result', right['result'] if right else 'No result')
                state = {('Pass','Fail'):'New fail', ('Fail','Pass'):'Fixed', ('Fail','Fail'):'Still fail',
                         ('Pass','Pass'):'Still pass'}.get(pair, 'No result' if not left or not right else 'Other')
                r = right or left or universe[key]
                items.append(dict(name=r['name'],path=r['path'],stage=r['stage'],config=r['config'],
                                  case_id=r['case_id'],left=left,right=right,result=state))
            counts = {state:sum(r['result']==state for r in items) for state in
                      ['New fail','Fixed','Still fail','Still pass','No result','Other']}
            items = [r for r in items if not q.get('result') or r['result']==q['result']]
            sort = q.get('sort') if q.get('sort') in ('name','stage','result') else 'name'
            items.sort(key=lambda r:(r[sort],r['path'],r['stage'],r['config']),reverse=q.get('order')=='desc')
            result = dict(items=items,total=len(items),generation=meta['generation']) if q.get('_export') else self.page(items,q,meta)
            result['counts'] = counts
            return result

    def export(self, q):
        q = dict(q, _export=True)
        kind = q.get('view', 'matrix')
        if kind not in ('matrix','history','compare'):
            raise ViewError('Unknown export view')
        data = getattr(self, kind)(q)
        if data['total'] > 100000:
            raise ViewError('Too many rows. Select fewer cases.', 413)
        stream = io.StringIO()
        stream.write('PJTest %s\nRows: %s\n' % (kind,data['total']))
        writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
        writer.writerow(['Case','Path','Stage','Version','Date','Result','Fail reason','Config'])
        for item in data['items']:
            if kind == 'matrix':
                runs = [r for values in item['stages'].values() for r in values]
            elif kind == 'compare':
                writer.writerow([item['name'],item['path'],item['stage'],q['left']+' -> '+q['right'],'',item['result'],'',item['config']])
                runs = [r for r in (item['left'],item['right']) if r]
            else:
                runs = [item]
            for r in runs:
                writer.writerow([r['name'],r['path'],r['stage'],r['version'],r['date'],r['result'],r['failed_reason'],r['config']])
        return stream.getvalue()
