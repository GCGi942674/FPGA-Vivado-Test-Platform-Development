#!/usr/bin/env python3

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "lib" / "python" / "bounded_run_log.py"


class BoundedRunLogTest(unittest.TestCase):
    def test_suppression_does_not_append_after_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--output",
                    "run",
                    "--limit-bytes",
                    "1048576",
                    "--limit-marker",
                    ".run_log_limit",
                    "--suppress-pin-redefinition",
                ],
                input=(
                    b"Routing Is Done.\n"
                    b"PinRedefinition is not implemented. HdnTError.cc\n"
                    b"Runtime: 73\n"
                ),
                cwd=workdir,
                check=False,
            )

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(
                (workdir / "run").read_bytes(),
                b"Routing Is Done.\nRuntime: 73\n",
            )
            self.assertFalse((workdir / ".run_log_limit").exists())


if __name__ == "__main__":
    unittest.main()
