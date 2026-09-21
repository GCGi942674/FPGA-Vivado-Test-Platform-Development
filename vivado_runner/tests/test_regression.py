"""Algorithm tests requiring neither SVN nor GalaxCore."""
import importlib.util
import ast
import contextlib
import io
from pathlib import Path
import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('regression', str(Path(__file__).resolve().parents[2] / 'regression.py'))
regression = importlib.util.module_from_spec(spec)
spec.loader.exec_module(regression)


class SearchTests(unittest.TestCase):
    def test_layout_does_not_depend_on_terminal_directory(self):
        script = Path(__file__).resolve().parents[2] / 'regression.py'
        with patch.object(Path, 'cwd', return_value=Path('/unrelated/terminal')):
            test2, root = regression.resolve_layout(script)
        self.assertEqual(test2, script.parent)
        self.assertEqual(root, script.parent.parent)

    def test_misplaced_script_is_rejected(self):
        with patch.object(Path, 'is_file', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'Place regression.py in test2'):
                regression.resolve_layout('/misplaced/regression.py')

    def test_every_boundary_and_stride(self):
        for size in range(2, 25):
            versions = list(range(100, 100 + size))
            for boundary in versions[1:]:
                for step in (1, 4, 100):
                    visited = []

                    def test(version, phase):
                        self.assertNotIn(version, visited)
                        visited.append(version)
                        return 'FAIL' if version >= boundary else 'PASS'

                    result = regression.locate(versions, step, test)
                    self.assertEqual(result['first_fail'], boundary)
                    self.assertEqual(result['last_pass'], boundary - 1)

    def test_sparse_cache(self):
        result = regression.locate([12, 15, 30, 31], 4,
                                   lambda version, phase: 'PASS' if version < 30 else 'FAIL')
        self.assertEqual(result['first_fail'], 30)
        self.assertTrue(result['uncached_gap'])

    def test_no_boundary(self):
        for versions in ([1], [1, 2, 3]):
            self.assertEqual(regression.locate(versions, 4, lambda v, p: 'PASS')['outcome'], 'BASE_PASS')
            self.assertEqual(regression.locate(versions, 4, lambda v, p: 'FAIL')['outcome'], 'NO_PASS_IN_CACHE')

    def test_manifest_includes_day_versions_and_skips_equivalent_revisions(self):
        text = 'success version[18472E, 18672, 18673E, 18674, 18674]\nday_version[18271, 18472, 18656, 18672]'
        self.assertEqual(regression.parse_version_manifest(text), {
            'versions': [18271, 18656, 18672, 18674],
            'day_versions': [18271, 18656, 18672]})

    def test_full_manifest_is_not_limited_to_twenty(self):
        text = 'success version[' + ', '.join(str(v) for v in range(18457, 18682)) + ']'
        manifest = regression.parse_version_manifest(text)
        self.assertEqual(manifest['versions'], list(range(18457, 18682)))
        self.assertEqual(manifest['day_versions'], [])

    def test_bad_or_abbreviated_lists_are_rejected(self):
        for text in ('bad', 'success version[1, ..., 30]', 'success version[12X]',
                     'success version[1]\nsuccess version[2]', 'success version[1E]',
                     'success version[1]\nday_version[2',
                     'success version[1]\nday_version[2]\nday_version[3]',
                     'success version[1]\nday_version[2, ..., 30]',
                     'success version[1] trailing',
                     'All available release success versions: [1, 2]'):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                regression.parse_version_manifest(text)

    def test_file_snapshot_and_current_revision_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, snapshot = Path(tmp) / 'success_version', Path(tmp) / 'snapshot'
            data = b'success version[18672, 18673E, 18674, 18680, 18681]\nday_version[18656, 18680, 18681]'
            path.write_bytes(data)
            self.assertEqual(regression.read_candidates(path, 18680, snapshot),
                             {'versions': [18656, 18672, 18674, 18680], 'day_versions': [18656]})
            self.assertEqual(snapshot.read_bytes(), data)
            self.assertEqual(regression.read_candidates(path, 18673, snapshot),
                             {'versions': [18656, 18672], 'day_versions': [18656]})
            self.assertEqual(regression.read_candidates(path, 18682, snapshot),
                             {'versions': [18656, 18672, 18674, 18680, 18681],
                              'day_versions': [18656, 18680]})
            with self.assertRaisesRegex(RuntimeError, 'No usable cached version at or before'):
                regression.read_candidates(path, 18600, snapshot)
            with self.assertRaisesRegex(RuntimeError, 'Cannot read version file'):
                regression.read_candidates(Path(tmp) / 'missing', 18680, snapshot)

    def test_latest_usable_base_can_come_from_day_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'success_version'
            path.write_text('success version[18672, 18680E, 18683]\nday_version[18679]')
            self.assertEqual(regression.read_candidates(path, 18682, Path(tmp) / 'snapshot'),
                             {'versions': [18672, 18679], 'day_versions': []})

    def test_fallback_pass_does_not_claim_original_checkout_passed(self):
        result = {'outcome': 'BASE_PASS', 'base': 18681,
                  'original_revision': 18682, 'test_base_revision': 18681}
        summary = regression.format_summary(result, 18681, 'case', 1, 'results.tsv')
        self.assertIn('Original checkout: r18682 (not tested', summary)
        self.assertIn('Base version: r18681  [PASS]', summary)
        self.assertNotIn('Current version passed', summary)

    def test_day_search_every_boundary_and_checkpoint_density(self):
        for size in range(1, 25):
            versions = list(range(100, 100 + size))
            for boundary in range(100, 101 + size):
                for days in ([], versions, versions[::4], versions[-1:], versions[:1]):
                    visited = []

                    def test(version, phase):
                        self.assertNotIn(version, visited)
                        visited.append(version)
                        return 'FAIL' if version >= boundary else 'PASS'

                    result = regression.locate_by_day(versions, days, test)
                    if boundary == 100:
                        self.assertEqual(result['outcome'], 'NO_PASS_IN_CACHE')
                        self.assertEqual(result['oldest'], versions[0])
                    elif boundary > versions[-1]:
                        self.assertEqual(result['outcome'], 'BASE_PASS')
                        self.assertEqual(visited, [versions[-1]])
                    else:
                        self.assertEqual(result['last_pass'], boundary - 1)
                        self.assertEqual(result['first_fail'], boundary)

    def test_day_search_matches_user_example(self):
        versions = list(range(18600, 18681))
        calls = []

        def test(version, phase):
            calls.append((version, phase))
            return 'PASS' if version < 18650 else 'FAIL'

        result = regression.locate_by_day(versions, [18600, 18631, 18656, 18656, 18680, 18690], test)
        self.assertEqual(calls[:3], [(18680, 'BASE'), (18656, 'DAY'), (18631, 'DAY')])
        self.assertTrue(all(phase == 'BISECT' and 18631 < v < 18656 for v, phase in calls[3:]))
        self.assertEqual(result['last_pass'], 18649)
        self.assertEqual(result['first_fail'], 18650)

    def test_day_search_fallback_older_than_all_days(self):
        calls = []

        def test(version, phase):
            calls.append((version, phase))
            return 'PASS' if version < 103 else 'FAIL'

        result = regression.locate_by_day(list(range(100, 111)), [104, 108], test)
        self.assertEqual(calls[:4], [(110, 'BASE'), (108, 'DAY'), (104, 'DAY'), (100, 'OLDEST')])
        self.assertEqual(result['first_fail'], 103)

    def test_day_only_old_history_reports_interval(self):
        result = regression.locate_by_day([18271, 18304, 18656, 18680], [18271, 18304, 18656],
                                          lambda v, p: 'PASS' if v < 18656 else 'FAIL')
        self.assertEqual(result['last_pass'], 18304)
        self.assertEqual(result['first_fail'], 18656)
        self.assertTrue(result['uncached_gap'])

    def test_errors_stop_both_search_strategies(self):
        for strategy in ('day', 'step'):
            for error in ('ERROR', 'TIMEOUT', None):
                calls = []

                def test(version, phase):
                    calls.append(version)
                    return 'FAIL' if phase == 'BASE' else error

                with self.assertRaisesRegex(RuntimeError, 'Inconclusive'):
                    if strategy == 'day':
                        regression.locate_by_day([1, 2, 3], [2], test)
                    else:
                        regression.locate([1, 2, 3], 1, test)
                self.assertEqual(calls, [3, 2])

    def test_svn_error_propagates_without_more_tests(self):
        calls = []

        def test(version, phase):
            calls.append(version)
            if phase != 'BASE':
                raise RuntimeError('SVN conflict')
            return 'FAIL'

        with self.assertRaisesRegex(RuntimeError, 'SVN conflict'):
            regression.locate_by_day([1, 2, 3], [2], test)
        self.assertEqual(calls, [3, 2])

    def test_result_cache_does_not_repeat_a_version_across_phases(self):
        calls = []
        test = regression.cached_test(lambda v, p: calls.append((v, p)) or 'PASS')
        self.assertEqual(test(42, 'DAY'), 'PASS')
        self.assertEqual(test(42, 'BISECT'), 'PASS')
        self.assertEqual(calls, [(42, 'DAY')])

    def test_cli_defaults_to_day_and_explicit_step_selects_stride(self):
        args = regression.parse_args(['case/run.tcl'])
        self.assertIsNone(args.step)
        self.assertEqual(args.versions_file, regression.DEFAULT_VERSION_FILE)
        args = regression.parse_args(['case/run.tcl', '4', '--versions-file', '/cache/manifest'])
        self.assertEqual(args.step, 4)
        self.assertEqual(args.versions_file, '/cache/manifest')
        for value in ('0', '-1'):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                regression.parse_args(['case/run.tcl', value])

    def test_python36_syntax(self):
        # The test host is newer, but deployed workstations may use Python 3.6.
        script = Path(regression.__file__)
        ast.parse(script.read_text(encoding='utf-8'), filename=str(script), feature_version=(3, 6))

    def test_run_uses_manifest_and_records_selected_strategy(self):
        for step, checkout in ((None, 105), (None, 106), (4, 105), (4, 106)):
            with self.subTest(step=step, checkout=checkout), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                test2 = root / 'test2'
                test2.mkdir()
                for filename in ('run.sh', 'flow_config'):
                    (test2 / filename).write_text('fixture')
                for directory in ('lib', 'config', 'templates'):
                    (test2 / 'vivado_runner' / directory).mkdir(parents=True)
                case = test2 / 'case' / 'run.tcl'
                case.parent.mkdir()
                case.write_text('fixture')
                manifest = root / 'success_version'
                manifest.write_text('success version[100, 101, 102, 103, 104, 105]\nday_version[100, 103]')
                args = regression.parse_args([str(case), '--versions-file', str(manifest)])
                args.step = step
                fake_fcntl = SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *args: None)
                with patch.object(regression, 'resolve_layout', return_value=(test2, root)), \
                        patch.dict(regression.sys.modules, {'fcntl': fake_fcntl}), \
                        patch.dict(regression.os.environ, {'RUN_SH_LOCK_FILE': str(root / 'runner.lock')}):
                    runner = regression.Regression(args)
                    calls = []

                    def test(version, phase):
                        if phase == 'BASE':
                            # The real test() must see the old checkout to trigger svn up.
                            self.assertEqual(runner.current, checkout)
                        calls.append((version, phase))
                        runner.current = version
                        status = 'PASS' if version < 102 else 'FAIL'
                        runner.rows.append(dict(version=version, status=status, phase=phase,
                                                seconds=0, reason='fixture'))
                        return status

                    try:
                        with patch.object(runner, 'revision', return_value=checkout), \
                                patch.object(runner, 'test', side_effect=test), \
                                patch.object(runner, 'command') as command, \
                                contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(runner.run(), 0)
                        command.assert_not_called()  # No qkmk -v for version discovery.
                        data = regression.json.loads((runner.output / 'results.json').read_text())
                        self.assertEqual(data['strategy'], 'day' if step is None else 'step')
                        self.assertEqual(data['result']['first_fail'], 102)
                        self.assertEqual(data['original_revision'], checkout)
                        self.assertEqual(data['test_base_revision'], 105)
                        self.assertEqual((runner.output / 'success_version.snapshot').read_bytes(),
                                         manifest.read_bytes())
                        plan = regression.json.loads((runner.output / 'candidate_versions.json').read_text())
                        self.assertEqual(plan['day_versions'], [100, 103])
                        self.assertEqual(calls[:2], [(105, 'BASE'), (103, 'DAY') if step is None else (101, 'BACKWARD')])
                    finally:
                        if runner.lock:
                            runner.lock.close()
                        if getattr(runner, 'runner_lock', None):
                            runner.runner_lock.close()


if __name__ == '__main__':
    unittest.main()
