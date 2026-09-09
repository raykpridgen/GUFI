"""Module 5: gufi_vt execution engine tests (parity + plan execute)."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from gufi_mcp.gufi_vt_executor import (
    GufiVtExecutor,
    execute_plan,
    reset_executor,
)
from gufi_mcp.query_plan import QueryPlanPipeline

REPO_ROOT = Path(__file__).resolve().parents[5]
VT_LIB = REPO_ROOT / "gufi/local/lib/gufi_vt.so"
NOTES_INDEX = REPO_ROOT / "gufi/local/search/notes"


def _require_fixtures() -> tuple[Path, str]:
    if not VT_LIB.is_file():
        raise unittest.SkipTest(f"gufi_vt.so not found at {VT_LIB}")
    if not (NOTES_INDEX / "db.db").is_file() and not NOTES_INDEX.is_dir():
        raise unittest.SkipTest(f"notes index not found at {NOTES_INDEX}")
    index_path = str(NOTES_INDEX.resolve()) + "/"
    return VT_LIB, index_path


def _baseline_rows(vt_lib: Path, statements: list[str]) -> list[list[object]]:
    """Module 1 style: fresh connection, load extension, run SQL."""
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    conn.load_extension(str(vt_lib))
    conn.enable_load_extension(False)
    rows: list[tuple] = []
    try:
        for stmt in statements:
            cur = conn.execute(stmt)
            if cur.description is not None:
                rows = cur.fetchall()
    finally:
        conn.close()
    return [list(row) for row in rows]


def _module1_listing_sql(index_path: str) -> list[str]:
    return [
        (
            "SELECT name, size, type "
            f"FROM gufi_vt_vrpentries('{index_path}', 4) "
            "WHERE type = 'f';"
        ),
    ]


def _module1_aggregate_sql(index_path: str) -> list[str]:
    return [
        (
            "CREATE VIRTUAL TABLE gufi USING gufi_vt("
            f"index='{index_path}', threads=4, "
            'I="CREATE TABLE intermediate(uid INT64, size INT64);", '
            "E=\"INSERT INTO intermediate SELECT uid, size FROM vrpentries "
            "WHERE type = 'f';\", "
            'K="CREATE TABLE aggregate(uid INT64, total INT64);", '
            'J="INSERT INTO aggregate SELECT uid, SUM(size) FROM intermediate '
            'GROUP BY uid;", '
            'G="SELECT uid, total FROM aggregate ORDER BY total DESC LIMIT 10"'
            ");"
        ),
        "SELECT * FROM gufi;",
        "DROP TABLE gufi;",
    ]


class TestGufiVtExecutorParity(unittest.TestCase):
    def setUp(self) -> None:
        reset_executor()

    def tearDown(self) -> None:
        reset_executor()

    def test_listing_sql_matches_module1_baseline(self) -> None:
        vt_lib, index_path = _require_fixtures()
        statements = _module1_listing_sql(index_path)
        expected = _baseline_rows(vt_lib, statements)

        executor = GufiVtExecutor(vt_lib)
        result = executor.execute_sql(statements)

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.rows, expected)
        self.assertEqual(result.row_count, 4)

    def test_aggregate_sql_matches_module1_baseline(self) -> None:
        vt_lib, index_path = _require_fixtures()
        statements = _module1_aggregate_sql(index_path)
        expected = _baseline_rows(vt_lib, statements)

        executor = GufiVtExecutor(vt_lib)
        result = executor.execute_sql(statements)

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.rows, expected)
        self.assertEqual(result.rows, [[1000, 27395]])

    def test_singleton_reuses_connection(self) -> None:
        vt_lib, index_path = _require_fixtures()
        executor = GufiVtExecutor(vt_lib)
        executor.execute_sql(_module1_listing_sql(index_path))
        self.assertTrue(executor.connected)
        executor.execute_sql(_module1_aggregate_sql(index_path))
        executor.close()
        self.assertFalse(executor.connected)


class TestExecutePlan(unittest.TestCase):
    def setUp(self) -> None:
        reset_executor()

    def tearDown(self) -> None:
        reset_executor()

    def test_execute_plan_listing(self) -> None:
        vt_lib, index_path = _require_fixtures()
        pipeline = (
            QueryPlanPipeline.skeleton("notes", "parity listing")
            .set_stage(
                "entries_sql",
                "SELECT name, size, type FROM vrpentries WHERE type = 'f';",
            )
            .update(output={"row_limit": 1000})
        )
        executor = GufiVtExecutor(vt_lib)
        result = execute_plan(pipeline, index_path, executor=executor)

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.row_count, 4)
        names = {row[0] for row in result.rows}
        self.assertIn("GUFInotes.md", names)

    def test_execute_plan_aggregate(self) -> None:
        vt_lib, index_path = _require_fixtures()
        pipeline = QueryPlanPipeline.total_file_size("notes")
        executor = GufiVtExecutor(vt_lib)
        result = execute_plan(pipeline, index_path, executor=executor)

        self.assertTrue(result.success, result.error)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(int(result.rows[0][0]), 27395)

    def test_dry_run_does_not_execute(self) -> None:
        _vt_lib, index_path = _require_fixtures()
        pipeline = QueryPlanPipeline.total_file_size("notes")
        result = execute_plan(pipeline, index_path, dry_run=True)

        self.assertTrue(result.success)
        self.assertEqual(result.row_count, 0)
        self.assertEqual(len(result.compiled_sql), 3)
        self.assertIn("CREATE VIRTUAL TABLE", result.compiled_sql[0])


if __name__ == "__main__":
    unittest.main()
