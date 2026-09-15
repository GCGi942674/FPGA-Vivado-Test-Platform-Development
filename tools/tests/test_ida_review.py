"""Offline model and Qt interaction tests; no real IDA integration claimed."""
import ast
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'ida_review.py'
spec = importlib.util.spec_from_file_location('ida_review', str(SCRIPT))
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def document(self, data):
        path = self.root / 'review_list'
        path.write_bytes(data)
        return review.ReviewDocument(path)

    def test_python36_syntax(self):
        ast.parse(SCRIPT.read_text(encoding='utf-8'), feature_version=(3, 6))

    def test_sections_and_done_and_unknown_markers(self):
        data = b'================changxu r18237\n0x383370:power\n0x383420:power 6\n================weihao r18237\n0xdf8c40:implflow2 3\n...\n'
        entries = review.parse_entries(data)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].section, 'changxu r18237')
        self.assertEqual(entries[1].marker, '6')
        self.assertEqual(entries[2].section, 'weihao r18237')
        self.assertEqual(entries[2].marker, '3')

    def test_mark_preserves_bom_crlf_tabs_no_final_newline_and_undo(self):
        data = b'\xef\xbb\xbf================changxu r18237\r\n\t0x383370:power \t\r\n0x383420:power'
        document = self.document(data)
        document.mark_done(document.entries[0])
        self.assertEqual(document.path.read_bytes(), data.replace(b':power \t', b':power 6 \t'))
        self.assertEqual(len(list((self.root / 'review_list.review-backups').glob('*.bak'))), 1)
        document.undo()
        self.assertEqual(document.path.read_bytes(), data)
        document.mark_done(document.entries[1])
        self.assertTrue(document.path.read_bytes().endswith(b'0x383420:power 6'))

    def test_duplicate_entries_modify_only_selected_row(self):
        data = b'=one\n0x383370:power\n=two\n0x383370:power\n'
        document = self.document(data)
        document.mark_done(document.entries[1])
        self.assertEqual(document.path.read_bytes(), b'=one\n0x383370:power\n=two\n0x383370:power 6\n')

    def test_external_edit_conflicts_and_unlocks(self):
        document = self.document(b'0x383370:power\n')
        changed = document.data + b'# somebody else edited this\n'
        document.path.write_bytes(changed)
        with self.assertRaisesRegex(review.ReviewError, 'changed on disk'):
            document.mark_done(document.entries[0])
        self.assertEqual(document.path.read_bytes(), changed)
        self.assertFalse(Path(str(document.path) + '.review.lock').exists())

    def test_lock_is_not_stolen(self):
        document = self.document(b'0x383370:power\n')
        lock = Path(str(document.path) + '.review.lock')
        lock.write_text('other process')
        with self.assertRaisesRegex(review.ReviewError, 'locked'):
            document.mark_done(document.entries[0])
        self.assertEqual(lock.read_text(), 'other process')

    def test_undo_refuses_new_changes(self):
        document = self.document(b'0x383370:power\n')
        document.mark_done(document.entries[0])
        changed = document.data + b'0x499700:power2\n'
        document.path.write_bytes(changed)
        with self.assertRaises(review.ReviewError):
            document.undo()
        self.assertEqual(document.path.read_bytes(), changed)

    def test_unknown_marker_is_preserved(self):
        document = self.document(b'0x383370:power 4\n')
        with self.assertRaisesRegex(review.ReviewError, 'unknown marker'):
            document.mark_done(document.entries[0])
        self.assertEqual(document.path.read_bytes(), b'0x383370:power 4\n')

    def test_mappings_are_per_function_not_fixed_offsets(self):
        text = '//0x383370:power#0x499700:power2 #Target:6509#\nint f() {}\n//0x383380:power#0x600000:power2\nint g() {}\n'
        hits = review.source_hits(text, 'power.cpp')
        self.assertEqual(review.target_address(hits[0], 'power2'), 0x499700)
        self.assertEqual(review.target_address(hits[1], 'power2'), 0x600000)
        with self.assertRaises(review.ReviewError):
            review.target_address(hits[0], 'route2')

    def test_ambiguous_mapping_is_refused(self):
        hit = review.source_hits('//0x383370:power#0x499700:power2#0x499800:power2', 'x.cpp')[0]
        with self.assertRaises(review.ReviewError):
            review.target_address(hit, 'power2')

    def test_index_keeps_duplicate_candidates_and_exact_module_keys(self):
        (self.root / 'one.cpp').write_text('//0x383370:power#0x499700:power2\nint f() {}')
        (self.root / 'two.cpp').write_text('//0x383370:power#0x499701:power2\nint f() {}')
        (self.root / 'other.cpp').write_text('//0x383370:power2#0x499700:power\nint f() {}')
        (self.root / '.git').mkdir()
        (self.root / '.git/hidden.cpp').write_text('//0x383370:power')
        index, warnings, count = review.build_index(self.root)
        self.assertEqual(count, 3)
        self.assertEqual(len(index[(0x383370, 'power')]), 2)
        self.assertEqual(len(index[(0x383370, 'power2')]), 1)
        self.assertFalse(warnings)

    def test_failed_atomic_replace_preserves_original(self):
        document = self.document(b'0x383370:power\n')
        with patch.object(review.os, 'replace', side_effect=OSError('failure')):
            with self.assertRaises(OSError):
                document.mark_done(document.entries[0])
        self.assertEqual(document.path.read_bytes(), b'0x383370:power\n')
        self.assertFalse(Path(str(document.path) + '.review.lock').exists())

    def test_ida_backend_requires_function_start_and_correct_image(self):
        state = {'path': '/demo/power2.so', 'base': 0x400000}
        opened = []
        modules = {
            'ida_nalt': SimpleNamespace(get_input_file_path=lambda: state['path'], get_imagebase=lambda: state['base']),
            'ida_funcs': SimpleNamespace(get_func=lambda ea: SimpleNamespace(start_ea=0x499700)),
            'ida_hexrays': SimpleNamespace(init_hexrays_plugin=lambda: True,
                                          open_pseudocode=lambda ea, flags: opened.append(ea) or SimpleNamespace(ct='widget')),
            'ida_kernwin': SimpleNamespace(get_widget_title=lambda widget: 'Pseudocode-A',
                                          set_dock_pos=lambda *args: True, DP_RIGHT=4),
        }
        with patch.dict(sys.modules, modules):
            backend = review.IdaBackend()
            backend.open(0x499700)
            self.assertEqual(opened, [0x499700])
            with self.assertRaisesRegex(review.ReviewError, 'function start'):
                backend.open(0x499701)
            state['base'] += 0x1000
            with self.assertRaisesRegex(review.ReviewError, 'image base changed'):
                backend.validate()


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PyQt5 import QtCore, QtGui, QtWidgets
except ImportError:
    QtWidgets = None


@unittest.skipIf(QtWidgets is None, 'PyQt5 is unavailable')
class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.panel_class = review.make_panel_class(QtCore, QtGui, QtWidgets)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'review_list').write_text('=author r1\n0x10:power\n0x20:power\n')
        (self.root / 'source.cpp').write_text('//0x10:power#0x99:power2\nint f() {}\n//0x20:power#0xa0:power2\nint g() {}\n')

        class Backend:
            identity = 'fake power2.so'
            preview = False
            def __init__(self):
                self.addresses = []
            def open(self, ea):
                self.addresses.append(ea)

        self.backend = Backend()
        self.panel = self.panel_class(self.backend)
        self.panel.load_list(self.root / 'review_list')
        self.panel.module.setText('power2')
        self.panel.index, _, _ = review.build_index(self.root)

    def tearDown(self):
        self.panel.shutdown()
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_complete_uses_pinned_row_after_ida_navigation(self):
        self.panel.next_entry()
        self.assertEqual(self.backend.addresses[-1], 0x99)
        self.backend.open(0x12345)
        self.panel.complete()
        self.assertIn('0x10:power 6', (self.root / 'review_list').read_text())
        self.assertEqual(self.panel.current.address, 0x20)
        self.assertEqual(self.backend.addresses[-1], 0xa0)

    def test_skip_does_not_write_and_can_reset(self):
        original = (self.root / 'review_list').read_bytes()
        self.panel.next_entry()
        self.panel.skip()
        self.assertEqual(self.panel.current.address, 0x20)
        self.assertEqual((self.root / 'review_list').read_bytes(), original)
        self.panel.reset_skips()
        self.panel.next_entry()
        self.assertEqual(self.panel.current.address, 0x10)

    def test_rebinding_disables_completion(self):
        self.panel.next_entry()
        self.assertTrue(self.panel.done.isEnabled())
        self.panel.module.setText('route2')
        self.assertFalse(self.panel.done.isEnabled())
        with self.assertRaises(review.ReviewError):
            self.panel.complete()

    def test_source_edit_invalidates_review_and_refreshes_text(self):
        self.panel.next_entry()
        source = self.root / 'source.cpp'
        source.write_text(source.read_text() + '// changed\n')
        self.panel.check_source()
        self.assertIn('// changed', self.panel.code.toPlainText())
        self.assertFalse(self.panel.done.isEnabled())
        with self.assertRaises(review.ReviewError):
            self.panel.complete()

    def test_duplicate_source_is_not_automatically_selected(self):
        (self.root / 'duplicate.cpp').write_text('//0x10:power#0x98:power2\nint f() {}')
        self.panel.index, _, _ = review.build_index(self.root)
        self.panel.next_entry()
        self.assertEqual(self.backend.addresses, [])
        self.assertFalse(self.panel.done.isEnabled())
        self.assertEqual(self.panel.candidates.count(), 3)

    def test_clearing_candidate_disables_completion(self):
        self.panel.next_entry()
        self.panel.candidates.setCurrentIndex(0)
        self.assertFalse(self.panel.done.isEnabled())
        with self.assertRaises(review.ReviewError):
            self.panel.complete()

    def test_preview_cannot_browse_real_files(self):
        self.backend.preview = True
        with self.assertRaisesRegex(review.ReviewError, 'disposable'):
            self.panel.choose_list()
        with self.assertRaisesRegex(review.ReviewError, 'disposable'):
            self.panel.choose_root()


if __name__ == '__main__':
    unittest.main()
