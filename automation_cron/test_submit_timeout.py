"""Submit timeout and success-gate regression checks."""
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import build_Galaxcore as build


class SubmitTimeoutTests(unittest.TestCase):
    def test_normal_capture(self):
        ok, code, output = build.run_cmd_capture(
            [sys.executable, '-c', "print('done')"], timeout=10)
        self.assertTrue(ok)
        self.assertEqual(code, 0)
        self.assertIn('done', output)

    def test_hanging_command_returns_timeout(self):
        ok, code, output = build.run_cmd_capture(
            [sys.executable, '-c', 'import time; time.sleep(60)'], timeout=0.1)
        self.assertFalse(ok)
        self.assertEqual(code, 124)
        self.assertIn('timed out', output)

    def test_linux_timeout_kills_private_group(self):
        process = Mock(pid=12345)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired('submit', 10), ('partial output', None)]
        with patch.object(build.os, 'name', 'posix'), \
                patch.object(build.os, 'killpg', create=True) as killpg, \
                patch.object(build.signal, 'SIGKILL', 9, create=True), \
                patch.object(build.subprocess, 'Popen', return_value=process) as popen:
            result = build.run_cmd_capture(['submit'], timeout=10)
        self.assertEqual(result[1], 124)
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        killpg.assert_called_once_with(12345, 9)

    def test_timeout_overrides_success_marker(self):
        with patch.object(build, 'SUBMIT_TEST_DIR', Mock()), \
                patch.object(build, 'run_cmd_capture', return_value=(
                    False, 124, build.SUBMIT_SUCCESS_MARKER)) as capture:
            ok, reason, _ = build.run_submit_test()
        self.assertFalse(ok)
        self.assertEqual(reason, 'submit_timeout')
        self.assertEqual(capture.call_args.kwargs['timeout'], build.SUBMIT_TIMEOUT_SECONDS)


if __name__ == '__main__':
    unittest.main()
