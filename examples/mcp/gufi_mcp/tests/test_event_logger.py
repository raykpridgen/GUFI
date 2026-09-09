"""Module 7: JSONL event logger tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from gufi_mcp.event_logger import (
    EventLogger,
    EventLoggingMiddleware,
    _extract_correlation_fields,
    log_pipeline_step,
)
from gufi_mcp.gufi_vt_executor import reset_executor
from gufi_mcp.pipeline_service import (
    estimate_query_cost,
    execute_query_plan,
    explain_plan,
    validate_plan,
)
from gufi_mcp.query_plan import QueryPlanPipeline
from gufi_util import parse_schema_registry, resolve_view_types

REPO_ROOT = Path(__file__).resolve().parents[5]
SCHEMA = resolve_view_types(
    parse_schema_registry(str(REPO_ROOT / "gufi/examples/mcp/gufi_mcp/schemas.json"))
)
INDEXES_ROOT = REPO_ROOT / "gufi/local/search"
VT_LIB = REPO_ROOT / "gufi/local/lib/gufi_vt.so"


class TestEventLogger(unittest.TestCase):
    def test_record_writes_jsonl_with_correlation_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "events.jsonl"
            logger = EventLogger(log_path)

            logger.record(
                event_type="tool",
                name="validate_query_plan",
                input_data={"plan": {"index": "notes"}},
                output_data={"valid": True, "plan_hash": "abc123def456"},
                duration_ms=1.5,
            )
            logger.record(
                event_type="tool",
                name="execute_query_plan",
                input_data={"plan_hash": "abc123def456"},
                output_data={
                    "success": True,
                    "plan_hash": "abc123def456",
                    "compiled_sql": ["SELECT 1;"],
                    "rows": [["1"]],
                    "row_count": 1,
                    "elapsed_ms": 3.2,
                },
                duration_ms=4.0,
            )

            events = logger.read_events()
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["plan_hash"], "abc123def456")
            self.assertGreater(events[1]["gap_since_prev_ms"], 0)
            self.assertEqual(events[1]["compiled_sql"], ["SELECT 1;"])
            self.assertEqual(events[1]["execute_elapsed_ms"], 3.2)

    def test_extract_correlation_fields_execute(self) -> None:
        fields = _extract_correlation_fields(
            "execute_query_plan",
            {
                "plan_hash": "h1",
                "compiled_sql": ["CREATE VIRTUAL TABLE gufi USING gufi_vt(...);"],
                "elapsed_ms": 9.0,
                "row_count": 2,
            },
        )
        self.assertEqual(fields["plan_hash"], "h1")
        self.assertIn("CREATE VIRTUAL TABLE", fields["compiled_sql"][0])


class TestEventLoggingMiddleware(unittest.IsolatedAsyncioTestCase):
    async def test_middleware_logs_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp) / "events.jsonl")
            middleware = EventLoggingMiddleware(logger)

            class FakeCtx:
                method = "tools/call"
                params = {
                    "name": "validate_query_plan",
                    "arguments": {"plan": {"index": "notes"}},
                }

            async def call_next(_ctx):
                return {"valid": True, "plan_hash": "hash0011223344"}

            result = await middleware(FakeCtx(), call_next)
            self.assertTrue(result["valid"])
            events = logger.read_events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "tool")
            self.assertEqual(events[0]["name"], "validate_query_plan")
            self.assertEqual(events[0]["plan_hash"], "hash0011223344")


class TestLoggedPipelineSession(unittest.TestCase):
    def setUp(self) -> None:
        reset_executor()
        if not VT_LIB.is_file() or not (INDEXES_ROOT / "notes").is_dir():
            raise unittest.SkipTest("fixture index or gufi_vt unavailable")

    def tearDown(self) -> None:
        reset_executor()

    def test_module6_flow_reconstructable_by_plan_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp) / "session.jsonl")
            plan = QueryPlanPipeline.total_file_size("notes").to_dict()

            validated = log_pipeline_step(
                logger,
                "validate_query_plan",
                {"plan": plan},
                lambda plan: validate_plan(plan, schema_registry=SCHEMA),
            )
            plan_hash_value = validated["plan_hash"]

            log_pipeline_step(
                logger,
                "estimate_query_cost",
                {"plan": plan},
                lambda plan: estimate_query_cost(plan, schema_registry=SCHEMA),
            )
            log_pipeline_step(
                logger,
                "explain_query_plan",
                {"plan": plan},
                lambda plan: explain_plan(
                    plan,
                    schema_registry=SCHEMA,
                    indexes_root=INDEXES_ROOT,
                ),
            )
            log_pipeline_step(
                logger,
                "execute_query_plan",
                {
                    "plan": plan,
                    "plan_hash": plan_hash_value,
                    "dry_run": True,
                },
                lambda plan, plan_hash, dry_run=False, approved=False: execute_query_plan(
                    plan,
                    plan_hash,
                    dry_run=dry_run,
                    approved=approved,
                    schema_registry=SCHEMA,
                    indexes_root=INDEXES_ROOT,
                ),
            )
            live = log_pipeline_step(
                logger,
                "execute_query_plan",
                {
                    "plan": plan,
                    "plan_hash": plan_hash_value,
                    "dry_run": False,
                },
                lambda plan, plan_hash, dry_run=False, approved=False: execute_query_plan(
                    plan,
                    plan_hash,
                    dry_run=dry_run,
                    approved=approved,
                    schema_registry=SCHEMA,
                    indexes_root=INDEXES_ROOT,
                ),
            )

            events = logger.events_for_plan_hash(plan_hash_value)
            tool_names = [event["name"] for event in events]
            self.assertIn("validate_query_plan", tool_names)
            self.assertEqual(tool_names.count("execute_query_plan"), 2)

            execute_events = [e for e in events if e["name"] == "execute_query_plan"]
            dry_event = next(e for e in execute_events if e["input"].get("dry_run") is True)
            live_event = next(e for e in execute_events if e["input"].get("dry_run") is False)
            self.assertIn("compiled_sql", dry_event)
            self.assertTrue(live_event["success"])
            self.assertTrue(live["success"])

            all_events = logger.read_events()
            self.assertEqual(len(all_events), 5)
            for event in all_events:
                json.dumps(event)
                self.assertIn("duration_ms", event)
                self.assertIn("gap_since_prev_ms", event)


if __name__ == "__main__":
    unittest.main()
