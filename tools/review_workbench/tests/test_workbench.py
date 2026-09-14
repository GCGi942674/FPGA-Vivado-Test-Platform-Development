"""Local integration tests; fake decompiler responses are explicitly test fixtures."""
import ast
import builtins
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app
import bridge_client
from core import ReviewDocument, ReviewError, build_index
from model import Workbench


class TestModel(Workbench):
    def rpc(self, operation, **fields):
        if not self.record:
            raise ReviewError('IDA disconnected')
        if operation in ('health', 'jump'):
            return self.record
        return dict(self.record, pseudocode=self.pseudocode, timestamp=1)


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.file = self.root / 'review_list'
        self.file.write_bytes(b'=changxu r18237\r\n0x10:power\r\n0x20:power\r\n=weihao r18238\r\n0x30:route 6\r\n')
        self.source = self.root / 'power.cpp'
        self.source.write_text('//0x10:power#0x99:power2 #Target:1#\nint one() {}\n//0x20:power#0xa0:power2\nint two() {}\n')
        self.model = TestModel(self.root / 'settings')
        self.model.document = ReviewDocument(self.file)
        self.model.root = str(self.root)
        self.model.module = 'power2'
        self.model.record = {'instance':'test','idb':'/test/power2.i64','input':'/test/power2.so','imagebase':0}
        self.model.records = [self.model.record]
        self.model.pseudocode = 'int one() { return 0; }'
        self.model.make_rows()
        self.model.index, _, _ = build_index(self.root)
        self.id = self.model.rows[0]['id']

    def tearDown(self):
        self.temp.cleanup()

    def test_index_reads_shared_source_once_per_phase_without_holding_lock(self):
        count = 300
        self.file.write_text('=author r1\n' + ''.join('0x{:x}:power\n'.format(i+1) for i in range(count)))
        self.source.write_text(''.join('//0x{:x}:power\nint f{}() {{}}\n'.format(i+1, i) for i in range(count)))
        self.model.document = ReviewDocument(self.file)
        self.model.make_rows()
        reads, unlocked = [], []
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == self.source:
                reads.append(path)
                acquired = threading.Event()
                def probe():
                    with self.model.lock:
                        acquired.set()
                thread = threading.Thread(target=probe, daemon=True)
                thread.start()
                unlocked.append(acquired.wait(0.5))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            self.model.reindex()
            deadline = time.monotonic() + 5
            while self.model.scanning and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(self.model.scanning)
        self.assertEqual(len(reads), 2)
        self.assertTrue(all(unlocked))
        self.assertEqual(self.model.rows[-1]['funcName'], 'f299')

    def test_version_author_and_target_address(self):
        self.assertEqual(self.model.rows[0]['author'], 'changxu')
        self.assertEqual(self.model.rows[2]['version'], 'r18238')
        result = self.model.handle('inspect', {'id':self.id})
        self.assertEqual(result['entry']['srcAddress'], '0x99')
        self.assertEqual(result['entry']['funcName'], 'one')
        self.assertEqual(result['leftState'], 'ok')

    def test_completion_persists_exact_row_and_undo(self):
        original = self.file.read_bytes()
        self.model.inspect(self.id)
        self.model.complete(self.id)
        self.assertEqual(self.file.read_bytes(), original.replace(b'0x10:power\r', b'0x10:power 6\r'))
        state = self.model.handle('undo', {})
        self.assertEqual(self.file.read_bytes(), original)
        self.assertFalse(state['canUndo'])

    def test_reject_completion_without_snapshot(self):
        with self.assertRaises(ReviewError):
            self.model.complete(self.id)

    def test_reject_source_changes(self):
        self.model.inspect(self.id)
        self.source.write_text(self.source.read_text()+'// changed\n')
        with self.assertRaisesRegex(ReviewError, 'Source changed'):
            self.model.complete(self.id)

    def test_reject_ida_changes(self):
        self.model.inspect(self.id)
        self.model.pseudocode = 'int one() { return 1; }'
        with self.assertRaisesRegex(ReviewError, 'Pseudocode changed'):
            self.model.complete(self.id)

    def test_reject_external_list_changes(self):
        self.model.inspect(self.id)
        changed = self.file.read_bytes()+b'# concurrent edit\n'
        self.file.write_bytes(changed)
        with self.assertRaisesRegex(ReviewError, 'changed on disk'):
            self.model.complete(self.id)
        self.assertEqual(self.file.read_bytes(), changed)

    def test_skip_persists_sidecar_not_marker(self):
        original = self.file.read_bytes()
        state = self.model.handle('skip', {'id':self.id})
        self.assertEqual(state['entries'][0]['status'], 'skipped')
        self.assertEqual(self.file.read_bytes(), original)
        self.assertIn(self.id, self.model.settings()['skipped'])

    def test_duplicates_require_choice(self):
        (self.root / 'other.cpp').write_text(self.source.read_text())
        self.model.index, _, _ = build_index(self.root)
        self.assertEqual(self.model.inspect(self.id)['rightState'], 'multiple')
        self.assertEqual(self.model.inspect(self.id, 1)['leftState'], 'ok')

    def test_disconnect_keeps_source_and_refuses_done(self):
        self.model.record = None
        result = self.model.inspect(self.id)
        self.assertEqual(result['rightState'], 'ok')
        self.assertEqual(result['leftState'], 'failed')
        self.assertIn('int one()', result['entry']['sourceCode'])
        with self.assertRaises(ReviewError):
            self.model.complete(self.id)

    def test_reindex_cancels_cached_mapping(self):
        self.model.inspect(self.id)
        self.model.reindex()
        self.assertFalse(self.model.snapshots)
        deadline = time.monotonic()+5
        while self.model.scanning and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertFalse(self.model.scanning)
        self.assertEqual(self.model.rows[0]['funcName'], 'one')


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.model = app.make_demo(self.temp.name)
        self.server = app.create_server(self.model)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:{}'.format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, operation, body=None, headers=None):
        auth = {'Content-Type':'application/json','X-Review-Token':self.server.token}
        auth.update(headers or {})
        return urlopen(Request(self.url+'/api/'+operation,data=json.dumps(body or {}).encode(),headers=auth),timeout=5)

    def test_real_http_workflow(self):
        with self.request('state') as response:
            state = json.load(response)['data']
        identifier = state['entries'][0]['id']
        with self.request('inspect', {'id':identifier}) as response:
            self.assertEqual(json.load(response)['data']['entry']['srcAddress'], '0x499700')
        with self.request('complete', {'id':identifier}) as response:
            self.assertEqual(json.load(response)['data']['entries'][0]['status'], 'done')
        self.assertIn('0x383370:power 6', self.model.document.path.read_text())

    def test_missing_token_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.request('state', headers={'X-Review-Token':''})
        self.assertEqual(error.exception.code,403)

    def test_foreign_origin_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.request('state', headers={'Origin':'https://example.com'})
        self.assertEqual(error.exception.code,403)

    def test_host_rebinding_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.request('state', headers={'Host':'attacker.invalid'})
        self.assertEqual(error.exception.code,403)

    def test_demo_cannot_load_real_project(self):
        with self.assertRaises(HTTPError) as error:
            self.request('setup', {'reviewFile':'/etc/passwd','sourceRoot':'/'})
        self.assertEqual(error.exception.code,400)


class BridgeTests(unittest.TestCase):
    def test_python36_syntax(self):
        for path in ROOT.glob('*.py'):
            ast.parse(path.read_text(encoding='utf-8'), filename=str(path), feature_version=(3,6))

    def test_mocked_ida_rpc_identity_and_jump(self):
        temp = tempfile.TemporaryDirectory()
        registry = Path(temp.name)
        os.chmod(str(registry),0o700)
        spec = importlib.util.spec_from_file_location('test_bridge',ROOT/'bridge.py')
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        bridge.REGISTRY = registry
        state = {'idb':'/test/power2.i64','base':0}
        jumped = []
        class Hook:
            def hook(self): pass
            def unhook(self): pass
        modules = {
            'ida_funcs':SimpleNamespace(get_func=lambda ea:SimpleNamespace(start_ea=0x99),get_func_name=lambda ea:'one'),
            'ida_hexrays':SimpleNamespace(init_hexrays_plugin=lambda:True,decompile=lambda ea:SimpleNamespace(get_pseudocode=lambda:[SimpleNamespace(line='int one() {}')]),open_pseudocode=lambda ea,flags:jumped.append(ea) or SimpleNamespace(ct=1)),
            'ida_idp':SimpleNamespace(IDB_Hooks=Hook),
            'ida_kernwin':SimpleNamespace(execute_sync=lambda fn,flags:fn(),MFF_WRITE=1,activate_widget=lambda *args:None),
            'ida_lines':SimpleNamespace(tag_remove=lambda text:text),
            'ida_nalt':SimpleNamespace(get_input_file_path=lambda:'/test/power2.so',get_imagebase=lambda:state['base']),
            'idc':SimpleNamespace(get_idb_path=lambda:state['idb']),
        }
        try:
            with patch.dict(sys.modules,modules):
                bridge.start()
                record=json.loads(next(registry.glob('*.json')).read_text())
                self.assertEqual(bridge_client.rpc(record,'pseudocode',address='0x99')['pseudocode'],'int one() {}')
                # No QApplication in this test; avoid exercising native window focus here.
                with patch.dict(sys.modules,{'PyQt5':None}):
                    bridge_client.rpc(record,'jump',address='0x99')
                self.assertEqual(jumped,[0x99])
                state['idb']='/test/other.i64'
                with self.assertRaisesRegex(RuntimeError,'database changed'):
                    bridge_client.rpc(record,'pseudocode',address='0x99')
        finally:
            owner=getattr(builtins,bridge.OWNER,None)
            if owner: owner.stop()
            temp.cleanup()


if __name__=='__main__':
    unittest.main()
