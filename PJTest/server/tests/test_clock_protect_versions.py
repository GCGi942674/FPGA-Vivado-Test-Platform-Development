#!/usr/bin/env python3
"""Clock-runner zip-retention marker tests."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from task_core import clock


class ClockProtectVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "GalaxCore"
        self.zip_dir = self.root / "zip"
        self.zip_dir.mkdir(parents=True)
        self.zip_path = self.zip_dir / "GalaxCore_18300.zip"
        self.zip_path.write_text("zip placeholder", encoding="utf-8")

    def test_protect_file_defaults_next_to_zip_dir(self):
        protect_file, added = clock.save_protected_version("18300", self.zip_path)
        protect_file, added_again = clock.save_protected_version(
            "18300",
            self.zip_path,
        )

        self.assertTrue(added)
        self.assertFalse(added_again)
        self.assertEqual(protect_file, self.root / "protect_versions")
        self.assertEqual(protect_file.read_text(encoding="utf-8"), "18300\n")
        self.assertTrue((protect_file.stat().st_mode & 0o004) != 0)

    def test_new_revision_replaces_all_old_versions(self):
        protect_file = self.root / "protect_versions"
        protect_file.write_text("r18301\n18300\n18301\nbad\n", encoding="utf-8")

        clock.save_protected_version("18299", self.zip_path)

        self.assertEqual(protect_file.read_text(encoding="utf-8"), "18299\n")

    def test_protect_file_path_can_be_overridden(self):
        custom_path = Path(self.temp.name) / "shared" / "keep_versions"

        with patch.dict(
            os.environ,
            {"PJTEST_PROTECT_VERSION_FILE": str(custom_path)},
        ):
            protect_file, _ = clock.save_protected_version("18300", self.zip_path)

        self.assertEqual(protect_file, custom_path)
        self.assertEqual(custom_path.read_text(encoding="utf-8"), "18300\n")

    def test_existing_shared_directory_permissions_are_not_changed(self):
        real_chmod = os.chmod
        changed_paths = []

        def record_chmod(path, mode):
            changed_paths.append(Path(path))
            real_chmod(path, mode)

        with patch.object(clock.os, "chmod", side_effect=record_chmod):
            protect_file, _ = clock.save_protected_version(
                "18300",
                self.zip_path,
            )

        self.assertNotIn(self.root, changed_paths)
        self.assertIn(protect_file, changed_paths)


if __name__ == "__main__":
    unittest.main()
