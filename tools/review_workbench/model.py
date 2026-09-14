"""Filesystem/project operations for the independent review workbench."""
from __future__ import print_function
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
import bridge_client
from core import ReviewDocument, ReviewError, build_index, source_hits, target_address


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Workbench:
    def __init__(self, state_dir=None):
        self.lock = threading.RLock()
        self.state_dir = Path(state_dir or Path.home() / '.config' / 'ida-review-workbench')
        self.settings_path = self.state_dir / 'settings.json'
        self.document = None
        self.root = ''
        self.rows, self.index, self.records = [], {}, []
        self.record, self.module = None, ''
        self.skipped, self.selected_candidates = set(), {}
        self.snapshots = {}
        self.scanning = False
        self.scan_error = ''
        self.scan_generation = 0
        self.active = ''
        self.last_done = ''
        self.demo = False
        self.browse = None
        self.close = None

    def remember(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        data = {'reviewFile': str(self.document.path) if self.document else '', 'sourceRoot': self.root,
                'targetModule': self.module, 'active': self.active,
                'fingerprint': digest(self.document.data) if self.document else '', 'skipped': sorted(self.skipped)}
        fd, tmp = tempfile.mkstemp(dir=str(self.state_dir), prefix='.settings-')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(data, stream)
            os.replace(tmp, str(self.settings_path))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def settings(self):
        try:
            data = json.loads(self.settings_path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def row(self, identifier):
        for row in self.rows:
            if row['id'] == identifier:
                return row
        raise ReviewError('Review row no longer exists. Reload the project.')

    def entry(self, identifier):
        row = self.row(identifier)
        return next(entry for entry in self.document.entries if entry.line == row['_line'])

    def make_rows(self):
        rows = []
        names = {row['id']: row['funcName'] for row in self.rows}
        for entry in self.document.entries:
            parts = entry.section.rsplit(None, 1)
            author, version = (parts[0], parts[1]) if len(parts) == 2 and re.fullmatch(r'r\d+', parts[1]) else (entry.section, 'Unversioned')
            identifier = '{}:{}:{}'.format(entry.line, entry.module, entry.address)
            state = 'done' if entry.marker == '6' else ('skipped' if identifier in self.skipped or entry.marker else 'pending')
            rows.append({'id': identifier, '_line': entry.line, 'author': author, 'version': version,
                         'address': hex(entry.address), 'module': entry.module, 'funcName': names.get(identifier, hex(entry.address) + ':' + entry.module),
                         'status': state, 'marker': entry.marker, 'srcAddress': '', 'srcModule': '', 'targetId': '',
                         'srcFile': '', 'srcLine': 1, 'sourceCode': '', 'pseudocode': '', 'srcCandidates': []})
        self.rows = rows

    def setup(self, request):
        if self.demo:
            raise ReviewError('Demo is isolated from real projects. Restart without --demo to select real files.')
        path = Path(str(request.get('reviewFile', ''))).expanduser()
        root = Path(str(request.get('sourceRoot', ''))).expanduser()
        if not path.is_file() or not root.is_dir():
            raise ReviewError('Select an existing review_list file and source directory.')
        document = ReviewDocument(path)
        if not document.entries:
            raise ReviewError('No address:module rows were found in the selected review file.')
        previous = self.settings()
        self.document, self.root = document, str(root.resolve())
        self.rows = []
        self.selected_candidates.clear()
        self.module = str(request.get('targetModule', '')).strip()
        self.skipped = set(previous.get('skipped', [])) if previous.get('reviewFile') == str(document.path) and previous.get('fingerprint') == digest(document.data) else set()
        self.make_rows()
        self.active = previous.get('active', '') if any(row['id'] == previous.get('active') for row in self.rows) else self.rows[0]['id']
        self.record = None
        self.bind(str(request.get('instance', '')), self.module)
        self.reindex()
        self.remember()
        return self.state(check_bridge=False)

    def connect(self):
        self.records = bridge_client.discover() if not self.demo else self.records
        if self.record and not any(item['instance'] == self.record['instance'] for item in self.records):
            self.record = None
            self.snapshots.clear()
        return [{'instance': item['instance'], 'idb': item['idb'], 'input': item['input']} for item in self.records]

    def bind(self, identifier, module):
        self.module = module
        self.record = next((record for record in self.records if record['instance'] == identifier), None)
        self.snapshots.clear()

    def reindex(self):
        if not self.document:
            raise ReviewError('Open a project first.')
        self.scan_generation += 1
        generation = self.scan_generation
        self.snapshots.clear()
        self.index = {}
        self.selected_candidates.clear()
        self.scanning, self.scan_error = True, ''
        def scan():
            try:
                index, warnings, count = build_index(self.root, lambda: generation != self.scan_generation)
                # Read each matched file once, outside the request lock.
                with self.lock:
                    rows = [dict(row) for row in self.rows]
                grouped, names = {}, {}
                for row in rows:
                    hits = index.get((int(row['address'], 16), row['module']), [])
                    if len(hits) == 1:
                        grouped.setdefault(hits[0].path, []).append((row, hits[0].line))
                for path, matches in grouped.items():
                    if generation != self.scan_generation:
                        return
                    try:
                        lines = Path(path).read_text(encoding='utf-8-sig', errors='replace').splitlines()
                        for row, line in matches:
                            names[row['id']] = self.function_name(lines, line, row['funcName'])
                    except OSError:
                        pass
                with self.lock:
                    if generation != self.scan_generation:
                        return
                    self.index = index
                    self.scan_error = '{} source files indexed; {} unreadable.'.format(count, len(warnings))
                    for row in self.rows:
                        row['funcName'] = names.get(row['id'], row['funcName'])
                    self.scanning = False
            except Exception as exc:
                with self.lock:
                    if generation == self.scan_generation:
                        self.scanning, self.scan_error = False, str(exc)
        thread = threading.Thread(target=scan, name='review source index')
        thread.daemon = True
        thread.start()

    @staticmethod
    def function_name(text, line, default):
        lines = text if isinstance(text, list) else text.splitlines()
        signature = ' '.join(lines[line + 1:line + 9])
        match = re.search(r'([\w:~]+)\s*\(', signature)
        return match.group(1) if match else default

    def state(self, check_bridge=True):
        connected = self.record is not None
        if connected and check_bridge:
            try:
                current = self.rpc('health', timeout=0.8)
                connected = all(current.get(key) == self.record.get(key) for key in ('instance', 'idb', 'input', 'imagebase'))
            except Exception:
                connected = False
        return {'entries': [{key: value for key, value in row.items() if not key.startswith('_')} for row in self.rows],
                'active': self.active, 'loaded': self.document is not None, 'scanning': self.scanning, 'scanMessage': self.scan_error,
                'idaStatus': 'connected' if connected else 'disconnected', 'idaDb': self.record['idb'] if self.record else '',
                'instances': [{'instance': item['instance'], 'idb': item['idb'], 'input': item['input']} for item in self.records],
                'settings': dict(self.settings(), targetModule=self.module), 'canUndo': bool(self.document and self.document.undo_state), 'demo': self.demo}

    def rpc(self, operation, **fields):
        if self.record is None:
            raise ReviewError('Connect to an IDA instance and choose its target module in Settings.')
        return bridge_client.rpc(self.record, operation, **fields)

    def inspect(self, identifier, candidate=None, decompile=True):
        row = dict(self.row(identifier))
        self.active = identifier
        self.snapshots.pop(identifier, None)
        if self.scanning:
            return {'entry': row, 'rightState': 'indexing', 'leftState': 'failed', 'error': 'Source index is still building.'}
        hits = self.index.get((int(row['address'], 16), row['module']), [])
        row['srcCandidates'] = ['{}:{}'.format(hit.path, hit.line + 1) for hit in hits]
        if not hits:
            return {'entry': row, 'rightState': 'not-found', 'leftState': 'failed', 'error': 'No exact address/module annotation found.'}
        choice = candidate if candidate is not None else self.selected_candidates.get(identifier)
        if len(hits) > 1 and choice is None:
            return {'entry': row, 'rightState': 'multiple', 'leftState': 'failed', 'error': 'Select an exact source candidate.'}
        choice = int(choice or 0)
        if choice < 0 or choice >= len(hits):
            raise ReviewError('Source candidate is no longer valid. Rebuild the index.')
        self.selected_candidates[identifier] = choice
        hit = hits[choice]
        data = Path(hit.path).read_bytes()
        text = data.decode('utf-8-sig', errors='replace')
        annotations = source_hits(text, hit.path)
        if hit not in annotations:
            raise ReviewError('Source mapping changed. Rebuild the index before continuing.')
        lines = text.splitlines()
        following = [item.line for item in annotations if item.line > hit.line]
        stop = min(following) if following else len(lines)
        row.update(srcFile=hit.path, srcLine=hit.line + 1, sourceCode='\n'.join(lines[hit.line:stop]),
                   funcName=self.function_name(text, hit.line, row['funcName']))
        target = re.search(r'#Target:([^#]+)#', lines[hit.line])
        row['targetId'] = target.group(1) if target else ''
        try:
            ea = target_address(hit, self.module)
            row.update(srcAddress=hex(ea), srcModule=self.module)
            if not decompile:
                return {'entry': row, 'rightState': 'ok', 'leftState': 'failed'}
            result = self.rpc('pseudocode', address=hex(ea))
            row['pseudocode'] = result['pseudocode']
            self.snapshots[identifier] = {'path': hit.path, 'sourceHash': digest(data), 'address': hex(ea),
                                           'pseudocodeHash': digest(result['pseudocode'].encode('utf-8')),
                                           'instance': self.record['instance'], 'module': self.module}
            self.row(identifier)['funcName'] = row['funcName']
            self.remember()
            return {'entry': row, 'rightState': 'ok', 'leftState': 'ok', 'timestamp': result.get('timestamp')}
        except Exception as exc:
            return {'entry': row, 'rightState': 'ok', 'leftState': 'failed', 'error': str(exc)}

    def complete(self, identifier):
        snapshot = self.snapshots.get(identifier)
        if not snapshot:
            raise ReviewError('Refresh the mapped Pseudocode successfully before completing this function.')
        if not self.record or snapshot['instance'] != self.record['instance'] or snapshot['module'] != self.module:
            raise ReviewError('IDA selection changed. Refresh and review again.')
        if digest(Path(snapshot['path']).read_bytes()) != snapshot['sourceHash']:
            self.snapshots.pop(identifier, None)
            raise ReviewError('Source changed during review. Rebuild the index and review again.')
        current = self.rpc('pseudocode', address=snapshot['address'])
        if digest(current['pseudocode'].encode('utf-8')) != snapshot['pseudocodeHash']:
            self.snapshots.pop(identifier, None)
            raise ReviewError('Pseudocode changed in IDA. Refresh and review the updated result.')
        self.document.mark_done(self.entry(identifier))
        self.skipped.discard(identifier)
        self.last_done = identifier
        self.make_rows()
        self.remember()
        return self.state(check_bridge=False)

    def handle(self, operation, request):
        with self.lock:
            if operation == 'state':
                return self.state()
            if operation == 'connect':
                self.connect()
                return self.state(check_bridge=False)
            if operation == 'setup':
                return self.setup(request)
            if operation == 'bind':
                self.bind(str(request.get('instance', '')), str(request.get('targetModule', '')).strip())
                self.remember()
                return self.state()
            if operation == 'inspect':
                return self.inspect(str(request['id']), request.get('candidate'))
            if operation == 'complete':
                return self.complete(str(request['id']))
            if operation == 'skip':
                identifier = str(request['id'])
                if self.row(identifier)['status'] != 'done':
                    self.skipped.add(identifier)
                self.make_rows()
                self.remember()
                return self.state(check_bridge=False)
            if operation == 'reset-skips':
                self.skipped.clear()
                self.make_rows()
                self.remember()
                return self.state(check_bridge=False)
            if operation == 'undo':
                if not self.document:
                    raise ReviewError('Open a project first.')
                self.document.undo()
                self.active = self.last_done
                self.make_rows()
                self.remember()
                return self.state(check_bridge=False)
            if operation == 'reindex':
                self.reindex()
                return self.state(check_bridge=False)
            if operation == 'reload':
                self.document.reload()
                self.document.undo_state = None
                self.snapshots.clear()
                self.make_rows()
                self.remember()
                return self.state(check_bridge=False)
            if operation == 'jump':
                identifier = str(request['id'])
                row = self.inspect(identifier, request.get('candidate'), decompile=False)['entry']
                if not row['srcAddress']:
                    raise ReviewError('No unique mapping to the selected IDA module.')
                self.rpc('jump', address=row['srcAddress'])
                return {'jumped': row['srcAddress']}
            if operation == 'editor':
                row = self.inspect(str(request['id']), request.get('candidate'), decompile=False)['entry']
                if not row['srcFile']:
                    raise ReviewError('No unique source file was selected.')
                executable = shutil.which('code')
                if not executable:
                    raise ReviewError('VS Code command "code" is not on PATH. Open {}:{} manually.'.format(row['srcFile'], row['srcLine']))
                subprocess.Popen([executable, '--goto', '{}:{}'.format(row['srcFile'], row['srcLine'])])
                return {'opened': row['srcFile']}
            if operation == 'browse':
                if self.demo or self.browse is None:
                    raise ReviewError('Native file picker unavailable. Enter the absolute path, or start desktop mode.')
                return {'path': self.browse(str(request.get('kind', 'file')))}
            if operation == 'close':
                if self.close:
                    self.close()
                return {'closed': True}
            raise ReviewError('Unknown action')
