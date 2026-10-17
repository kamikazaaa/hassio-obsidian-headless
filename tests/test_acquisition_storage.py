"""Obsidian workspace budgets apply during acquisition, before publication."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from hardened import runtime


class AcquisitionStorageTests(unittest.TestCase):
    def test_large_file_is_stopped_during_acquisition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b'x'*2000000)"
            with patch.object(runtime, 'MAX_ACQUISITION_FILE_BYTES', 1024 * 1024, create=True):
                with self.assertRaises(runtime.RuntimeFailure):
                    runtime.run_command([sys.executable, '-c', code, str(root / 'large.md')],
                                        dict(os.environ), workspace=root, timeout=2)
            self.assertLessEqual((root / 'large.md').stat().st_size, 1024 * 1024)

    def test_aggregate_budget_is_checked_even_when_child_finishes_quickly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = "import pathlib,sys; r=pathlib.Path(sys.argv[1]); [(r/str(i)).write_bytes(b'x'*1000) for i in range(4)]"
            with patch.object(runtime, 'MAX_ACQUISITION_BYTES', 2000, create=True):
                with self.assertRaises(runtime.RuntimeFailure):
                    runtime.run_command([sys.executable, '-c', code, str(root)], dict(os.environ), workspace=root)

    def test_no_acquisition_starts_when_storage_reserve_is_exhausted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file = root / 'should-not-exist'
            code = "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()"
            low_space = type('Space', (), {'f_bavail': 1, 'f_frsize': 4096})()
            with patch.object(runtime.os, 'fstatvfs', return_value=low_space):
                with self.assertRaises(runtime.RuntimeFailure):
                    runtime.run_command([sys.executable, '-c', code, str(file)], dict(os.environ), workspace=root)
            self.assertFalse(file.exists())


if __name__ == '__main__':
    unittest.main()
