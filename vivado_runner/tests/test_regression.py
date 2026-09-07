"""Algorithm tests requiring neither SVN nor GalaxCore."""
import importlib.util
from pathlib import Path
import unittest
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

    def test_cache_parser(self):
        self.assertEqual(regression.cached_versions(
            'banner\nAll available success versions:\n[42, 40, 39]\nAll available day versions:\n[12]'), [39, 40, 42])
        with self.assertRaises(RuntimeError):
            regression.cached_versions('bad output')


if __name__ == '__main__':
    unittest.main()
