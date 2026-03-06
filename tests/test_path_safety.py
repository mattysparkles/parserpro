import os
import unittest
from pathlib import Path
from unittest.mock import patch

from config import _add_dir_to_path_windows


class PathSafetyTests(unittest.TestCase):
    def test_add_dir_to_path_windows_keeps_existing_entries(self):
        original_path = r"C:\Python312;C:\Windows\System32"
        with patch.dict(os.environ, {"PATH": original_path}, clear=False):
            result = _add_dir_to_path_windows(Path(r"C:\Tools\Hydra"))

            self.assertTrue(result["session_updated"])
            self.assertFalse(result["persisted"])
            updated = os.environ["PATH"]
            self.assertIn(r"C:\Python312", updated)
            self.assertIn(r"C:\Windows\System32", updated)
            self.assertIn(r"C:\Tools\Hydra", updated)

    def test_add_dir_to_path_windows_noop_when_entry_exists_case_insensitive(self):
        original_path = r"C:\Python312;C:\Tools\Hydra"
        with patch.dict(os.environ, {"PATH": original_path}, clear=False):
            result = _add_dir_to_path_windows(Path(r"c:\tools\hydra"))

            self.assertFalse(result["session_updated"])
            self.assertFalse(result["persisted"])
            self.assertEqual(os.environ["PATH"], original_path)


if __name__ == "__main__":
    unittest.main()
