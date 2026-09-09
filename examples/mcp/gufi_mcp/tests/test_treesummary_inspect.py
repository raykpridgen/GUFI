"""Lightweight treesummary availability probe tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from gufi_util import check_treesummary, get_settings, inspect_treesummary_status

REPO_ROOT = Path(__file__).resolve().parents[5]
NOTES_INDEX = REPO_ROOT / "gufi/local/search/notes"


class TestCheckTreesummary(unittest.TestCase):
    def test_missing_db_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(check_treesummary(Path(tmp)))

    def test_db_without_treesummary_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            start = Path(tmp)
            conn = sqlite3.connect(str(start / "db.db"))
            conn.execute("CREATE TABLE summary(name TEXT)")
            conn.commit()
            conn.close()
            self.assertFalse(check_treesummary(start))

    def test_db_with_treesummary_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            start = Path(tmp)
            conn = sqlite3.connect(str(start / "db.db"))
            conn.execute("CREATE TABLE treesummary(inode TEXT)")
            conn.commit()
            conn.close()
            self.assertTrue(check_treesummary(start))


class TestInspectTreesummaryStatus(unittest.TestCase):
    def test_unknown_index_returns_warning(self) -> None:
        status = inspect_treesummary_status("nonexistent_index_xyz")
        self.assertFalse(status["treesummary_available"])
        self.assertTrue(status["warnings"])

    def test_notes_fixture_if_present(self) -> None:
        if not (NOTES_INDEX / "db.db").is_file():
            self.skipTest("notes fixture index not available")
        get_settings()
        status = inspect_treesummary_status("notes")
        self.assertEqual(status["index_name"], "notes")
        self.assertIn("notes", status["target_path"])
        self.assertIsNotNone(status["db_path"])
        self.assertIsInstance(status["treesummary_available"], bool)
        self.assertGreaterEqual(status["execution_time_ms"], 0.0)


if __name__ == "__main__":
    unittest.main()
