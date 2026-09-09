"""Module 9: eval harness tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gufi_mcp.eval_harness import default_eval_cases, run_eval_suite, write_eval_report
from gufi_mcp.gufi_vt_executor import reset_executor
from gufi_util import parse_schema_registry, resolve_view_types

REPO_ROOT = Path(__file__).resolve().parents[5]
VT_LIB = REPO_ROOT / "gufi/local/lib/gufi_vt.so"
INDEXES_ROOT = REPO_ROOT / "gufi/local/search"
SCHEMA = resolve_view_types(
    parse_schema_registry(str(REPO_ROOT / "gufi/examples/mcp/gufi_mcp/schemas.json"))
)


def _require_fixtures() -> None:
    if not VT_LIB.is_file():
        raise unittest.SkipTest("gufi_vt.so missing")
    if not (INDEXES_ROOT / "notes").is_dir():
        raise unittest.SkipTest("notes index missing")


class TestEvalHarness(unittest.TestCase):
    def setUp(self) -> None:
        reset_executor()
        _require_fixtures()

    def tearDown(self) -> None:
        reset_executor()

    def test_eval_suite_runs_on_notes_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            report = run_eval_suite(
                schema_registry=SCHEMA,
                indexes_root=INDEXES_ROOT,
                vt_lib=VT_LIB,
                log_path=log_dir / "case.jsonl",
            )
            self.assertEqual(len(report.cases), len(default_eval_cases()))
            self.assertEqual(report.summary["structured_correct"], len(report.cases))
            self.assertEqual(report.summary["raw_correct"], len(report.cases))
            for case in report.cases:
                self.assertTrue(case.correctness_structured, case.case_id)
                self.assertTrue(case.correctness_raw, case.case_id)
                self.assertGreaterEqual(case.structured_tool_steps, 5)

    def test_write_eval_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = run_eval_suite(
                schema_registry=SCHEMA,
                indexes_root=INDEXES_ROOT,
                vt_lib=VT_LIB,
            )
            out = Path(tmp) / "report.json"
            write_eval_report(report, out)
            self.assertTrue(out.is_file())
            self.assertIn("summary", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
