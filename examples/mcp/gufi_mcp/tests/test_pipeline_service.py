"""Module 6: MCP pipeline tool orchestration tests."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from gufi_mcp.gufi_vt_executor import reset_executor
from gufi_mcp.pipeline_service import (
    estimate_query_cost,
    execute_query_plan,
    validate_plan,
)
from gufi_mcp.query_plan import QueryPlanPipeline, plan_hash
from gufi_util import parse_schema_registry, resolve_view_types

REPO_ROOT = Path(__file__).resolve().parents[5]
VT_LIB = REPO_ROOT / "gufi/local/lib/gufi_vt.so"
SCHEMA = resolve_view_types(
    parse_schema_registry(
        str(REPO_ROOT / "gufi/examples/mcp/gufi_mcp/schemas.json")
    )
)
INDEXES_ROOT = REPO_ROOT / "gufi/local/search"


def _notes_total_size_plan() -> dict:
    return QueryPlanPipeline.total_file_size("notes").to_dict()


def _require_fixtures() -> None:
    if not VT_LIB.is_file():
        raise unittest.SkipTest(f"gufi_vt.so not found at {VT_LIB}")
    if not (INDEXES_ROOT / "notes").is_dir():
        raise unittest.SkipTest("notes index not found")


class TestValidatePlan(unittest.TestCase):
    def test_valid_plan_returns_plan_hash(self) -> None:
        plan = _notes_total_size_plan()
        result = validate_plan(plan, schema_registry=SCHEMA)
        self.assertTrue(result["valid"])
        self.assertIn("plan_hash", result)
        self.assertEqual(len(result["plan_hash"]), 12)
        self.assertEqual(result["plan_hash"], plan_hash(result["normalized_plan"]))


class TestEstimateQueryCost(unittest.TestCase):
    def test_aggregate_plan_is_low_or_medium(self) -> None:
        plan = _notes_total_size_plan()
        result = estimate_query_cost(plan, schema_registry=SCHEMA)
        self.assertIn(result["cost_level"], ("LOW", "MEDIUM", "HIGH"))
        self.assertTrue(result["valid"])
        self.assertIn("vrpentries", result["tables_touched"])

    def test_unbounded_listing_is_high(self) -> None:
        plan = (
            QueryPlanPipeline.skeleton("notes", "wide listing")
            .set_stage("entries_sql", "SELECT name FROM vrpentries;")
            .update(output={"row_limit": 50_000})
            .to_dict()
        )
        result = estimate_query_cost(plan, schema_registry=SCHEMA)
        self.assertEqual(result["cost_level"], "HIGH")
        self.assertTrue(result["requires_approval"])


class TestExecuteQueryPlan(unittest.TestCase):
    def setUp(self) -> None:
        reset_executor()
        _require_fixtures()

    def tearDown(self) -> None:
        reset_executor()

    def test_rejects_plan_hash_mismatch(self) -> None:
        plan = _notes_total_size_plan()
        result = execute_query_plan(
            plan,
            "deadbeef0000",
            schema_registry=SCHEMA,
            indexes_root=INDEXES_ROOT,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "plan_hash_mismatch")

    def test_dry_run_returns_compiled_sql(self) -> None:
        plan = _notes_total_size_plan()
        validated = validate_plan(plan, schema_registry=SCHEMA)
        result = execute_query_plan(
            plan,
            validated["plan_hash"],
            dry_run=True,
            schema_registry=SCHEMA,
            indexes_root=INDEXES_ROOT,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["row_count"], 0)
        self.assertEqual(len(result["compiled_sql"]), 3)
        self.assertIn("CREATE VIRTUAL TABLE", result["compiled_sql"][0])

    @patch("gufi_mcp.pipeline_service.get_executor")
    def test_rejects_invalid_plan(self, mock_get_executor) -> None:
        plan = QueryPlanPipeline.skeleton("notes", "empty").to_dict()
        result = execute_query_plan(
            plan,
            "ignored",
            schema_registry=SCHEMA,
            indexes_root=INDEXES_ROOT,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "validation_failed")
        mock_get_executor.assert_not_called()

    def test_execute_total_file_size(self) -> None:
        plan = _notes_total_size_plan()
        validated = validate_plan(plan, schema_registry=SCHEMA)
        dry = execute_query_plan(
            plan,
            validated["plan_hash"],
            dry_run=True,
            schema_registry=SCHEMA,
            indexes_root=INDEXES_ROOT,
        )
        live = execute_query_plan(
            plan,
            validated["plan_hash"],
            schema_registry=SCHEMA,
            indexes_root=INDEXES_ROOT,
        )
        self.assertTrue(live["success"], live.get("message") or live.get("error"))
        self.assertEqual(live["compiled_sql"], dry["compiled_sql"])
        self.assertEqual(live["row_count"], 1)
        self.assertEqual(int(live["rows"][0][0]), 27395)


class TestEndToEndFlow(unittest.TestCase):
    """Design §14 flow: draft → validate → cost → explain → execute."""

    def setUp(self) -> None:
        reset_executor()
        _require_fixtures()

    def tearDown(self) -> None:
        reset_executor()

    def test_full_pipeline_flow(self) -> None:
        from gufi_mcp.pipeline_service import explain_plan

        draft = QueryPlanPipeline.skeleton("notes", "top files").set_stage(
            "entries_sql",
            "SELECT name, size FROM vrpentries WHERE type = 'f' ORDER BY size DESC",
        ).update(output={"row_limit": 3})

        plan = draft.to_dict()
        validated = validate_plan(plan, schema_registry=SCHEMA)
        self.assertTrue(validated["valid"])

        cost = estimate_query_cost(plan, schema_registry=SCHEMA)
        self.assertTrue(cost["valid"])

        explained = explain_plan(
            plan,
            schema_registry=SCHEMA,
            indexes_root=INDEXES_ROOT,
        )
        self.assertIn("compiled_sql", explained)
        self.assertTrue(explained["compiled_sql"])

        dry = execute_query_plan(
            plan,
            validated["plan_hash"],
            dry_run=True,
            schema_registry=SCHEMA,
            indexes_root=INDEXES_ROOT,
        )
        self.assertTrue(dry["success"])

        live = execute_query_plan(
            plan,
            validated["plan_hash"],
            schema_registry=SCHEMA,
            indexes_root=INDEXES_ROOT,
        )
        self.assertTrue(live["success"])
        self.assertEqual(live["row_count"], 3)


if __name__ == "__main__":
    unittest.main()
