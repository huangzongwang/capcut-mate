import logging
import ntpath
import unittest
from unittest.mock import patch

from src.utils.logger import RelativePathFormatter


class RelativePathFormatterTests(unittest.TestCase):
    def format_path(self, path):
        formatter = RelativePathFormatter(
            "%(rel_path)s:%(lineno)d | %(message)s",
            project_root=r"E:\project\capcut-mate",
        )
        record = logging.LogRecord("uvicorn", logging.INFO, path, 42, "Started", (), None)
        with patch("src.utils.logger.os.path.relpath", side_effect=ntpath.relpath):
            return formatter.format(record)

    def test_same_drive_uses_relative_path(self):
        self.assertEqual(
            self.format_path(r"E:\project\capcut-mate\main.py"),
            "main.py:42 | Started",
        )

    def test_other_drive_preserves_original_path(self):
        path = r"D:\anaconda3\envs\capcut-mate\Lib\site-packages\uvicorn\server.py"
        self.assertEqual(self.format_path(path), path + ":42 | Started")


if __name__ == "__main__":
    unittest.main()
