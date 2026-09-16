import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib' / 'python'))
from summarize import build_reports, build_timeout_lines


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.list = self.root / 'cases.txt'
        self.meta = dict(case_list=str(self.list), workspace_root=str(self.root),
                         host_name='test', svn_version='1', flow_config='flow_config',
                         enabled_modules='route_design', bg_max='8', time_limit='5400')

    def record(self, name, status, start=100, end=110):
        return dict(RUN_TCL=str(self.root / name / 'run.tcl'),
                    CASE_DIR=str(self.root / name), STATUS=status,
                    REASON=status + '_REASON', START_TS=str(start), END_TS=str(end),
                    RUNTIME_SEC=str(end-start))

    def test_single_case_excludes_old_results_from_every_output(self):
        self.list.write_text('selected/run.tcl\n', encoding='utf-8')
        records = [self.record('selected', 'FAIL')]
        records += [self.record('old%d' % i, 'PASS', 1, 90) for i in range(84)]
        records += [self.record('old_timeout', 'TIMEOUT', 1, 900)]
        summary, failed, stat, text, data = build_reports(records, self.meta)
        self.assertEqual((data['total'], data['runnable_cases'], data['failed_cases']), (1, 1, 1))
        self.assertEqual(data['elapsed_time_sec'], 10)
        self.assertEqual(data['average_runtime_sec'], 10)
        self.assertEqual(data['reason_counter'], {'FAIL_REASON': 1})
        self.assertEqual(summary, '')
        self.assertNotIn('old', failed + stat + text)
        self.assertEqual(build_timeout_lines(data['records'], str(self.root)), '')

    def test_empty_list_does_not_fall_back_to_history(self):
        self.list.write_text('', encoding='utf-8')
        data = build_reports([self.record('old', 'PASS')], self.meta)[-1]
        self.assertEqual(data['total'], 0)
        self.assertEqual(data['records'], [])

    def test_mixed_results_missing_case_and_duplicate_paths(self):
        self.list.write_text('pass/run.tcl\n./pass/run.tcl\nfail/run.tcl\ntimeout/run.tcl\nmissing/run.tcl\n', encoding='utf-8')
        records = [self.record(name, status) for name, status in
                   [('pass', 'PASS'), ('fail', 'FAIL'), ('timeout', 'TIMEOUT')]]
        data = build_reports(records, self.meta)[-1]
        self.assertEqual(data['total'], 4)
        self.assertEqual(data['skipped_cases'], 1)
        self.assertEqual(data['reason_counter'], {'FAIL_REASON': 1, 'TIMEOUT_REASON': 1})
        self.assertEqual(build_timeout_lines(data['records'], str(self.root)), '/timeout/run.tcl\n')

    def test_missing_explicit_list_fails_instead_of_counting_history(self):
        with self.assertRaises(FileNotFoundError):
            build_reports([self.record('old', 'PASS')], self.meta)

    def test_no_list_preserves_standalone_mode(self):
        self.meta['case_list'] = ''
        data = build_reports([self.record('a', 'PASS')], self.meta)[-1]
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['reason_counter'], {})


if __name__ == '__main__':
    unittest.main()
