"""Module 8: routing and prompt content tests."""

from __future__ import annotations

import unittest

from gufi_mcp.agent_routing import (
    APPROACH_PIPELINE,
    APPROACH_WRAPPER,
    APPROACH_WHEN_USER_ASKS,
    PIPELINE_TOOLS_REFERENCE,
    PIPELINE_WORKFLOW_STEPS,
    RESOURCES_REFERENCE,
    ROUTING_GATE_TEXT,
    WRAPPER_SATISFIED_RULES,
    WRAPPER_TOOLS_REFERENCE,
    build_find_biggest_files_prompt,
    build_plan_gufi_query_prompt,
    build_routing_payload,
    build_session_briefing_prompt,
    build_simple_query_prompt,
    route_query,
)


class TestRouteQuery(unittest.TestCase):
    def test_largest_files_routes_wrapper(self) -> None:
        decision = route_query("Find the largest files in the notes index")
        self.assertEqual(decision.route, "wrapper")
        self.assertIn("gufi_client_find", decision.suggested_tools)

    def test_uid_breakdown_routes_pipeline(self) -> None:
        decision = route_query(
            "Total size of regular files broken down by uid, top 10"
        )
        self.assertEqual(decision.route, "pipeline")

    def test_total_bytes_routes_wrapper(self) -> None:
        decision = route_query("What is the total size of regular files?")
        self.assertEqual(decision.route, "wrapper")
        self.assertIn("gufi_client_du", decision.suggested_tools)

    def test_how_much_space_routes_wrapper(self) -> None:
        decision = route_query("How much space does vault use?")
        self.assertEqual(decision.route, "wrapper")
        self.assertIn("gufi_client_du", decision.suggested_tools)

    def test_disk_usage_routes_wrapper(self) -> None:
        decision = route_query("Show disk usage under vault/")
        self.assertEqual(decision.route, "wrapper")

    def test_large_files_filter_routes_wrapper(self) -> None:
        decision = route_query("List files in vault larger than 500 MiB")
        self.assertEqual(decision.route, "wrapper")
        self.assertIn("gufi_client_find", decision.suggested_tools)

    def test_ambiguous_defaults_wrapper(self) -> None:
        decision = route_query("Tell me about the vault index")
        self.assertEqual(decision.route, "wrapper")

    def test_total_by_uid_routes_pipeline(self) -> None:
        decision = route_query("Total file bytes by uid top 10")
        self.assertEqual(decision.route, "pipeline")


class TestRoutingPayload(unittest.TestCase):
    def test_wrapper_payload_avoids_pipeline_tools(self) -> None:
        payload = build_routing_payload("How big is vault?")
        self.assertEqual(payload["route"], "wrapper")
        self.assertIn("new_query_plan", payload["avoid_tools"])
        self.assertEqual(payload["resources_needed"], ["gufi://indexes"])
        self.assertIn("STOP", payload["stop_if_satisfied"])

    def test_pipeline_payload_includes_schema_resources(self) -> None:
        payload = build_routing_payload("Total bytes grouped by uid")
        self.assertEqual(payload["route"], "pipeline")
        self.assertEqual(payload["avoid_tools"], [])
        self.assertIn("gufi://query-flags", payload["resources_needed"])


class TestPromptContent(unittest.TestCase):
    def test_session_briefing_covers_stop_rule_and_wrapper_first(self) -> None:
        prompt = build_session_briefing_prompt()
        self.assertIn("Wrapper-first", prompt)
        self.assertIn("Stop rule", prompt)
        self.assertIn("gufi_route_query", prompt)
        self.assertIn("plan_gufi_query", prompt)
        self.assertIn("Routing gate", prompt)
        self.assertIn("gufi_vt path rules", prompt)
        self.assertIn("rpath || '/' || name", prompt)

    def test_plan_gufi_query_pipeline_includes_full_workflow(self) -> None:
        prompt = build_plan_gufi_query_prompt(
            "notes",
            "Total file bytes by uid top 10",
        )
        self.assertIn("Routing gate", prompt)
        self.assertIn("Pipeline path", prompt)
        self.assertIn("Workflow (pipeline path):", prompt)
        self.assertIn("validate_query_plan", prompt)
        self.assertIn("execute_query_plan", prompt)

    def test_plan_gufi_query_wrapper_omits_pipeline_workflow(self) -> None:
        prompt = build_plan_gufi_query_prompt(
            "vault",
            "How much space does vault use?",
        )
        self.assertIn("Wrapper path", prompt)
        self.assertIn("STOP", prompt)
        self.assertNotIn("Workflow (pipeline path):", prompt)
        self.assertNotIn("| `gufi://query-flags` |", prompt)

    def test_simple_query_prompt_is_wrapper_only(self) -> None:
        prompt = build_simple_query_prompt("vault", "disk usage for vault")
        self.assertIn("wrapper tools", prompt.lower())
        self.assertIn("STOP", prompt)
        self.assertNotIn("Workflow (pipeline path):", prompt)
        self.assertIn("gufi_client_du", prompt)

    def test_find_biggest_files_prefers_wrapper(self) -> None:
        prompt = build_find_biggest_files_prompt("notes")
        self.assertIn("wrapper", prompt.lower())
        self.assertIn("gufi_client_find", prompt)
        self.assertIn("STOP", prompt)
        self.assertNotIn("Workflow (pipeline path):", prompt)

    def test_reference_blocks_present(self) -> None:
        self.assertIn("gufi_client_du", WRAPPER_TOOLS_REFERENCE)
        self.assertIn("gufi_route_query", WRAPPER_TOOLS_REFERENCE)
        self.assertIn("new_query_plan", PIPELINE_TOOLS_REFERENCE)
        self.assertIn("Routing gate", ROUTING_GATE_TEXT)
        self.assertIn("wrapper first", ROUTING_GATE_TEXT.lower())
        self.assertIn("gufi://indexes", RESOURCES_REFERENCE)
        self.assertIn("Pipeline path only", RESOURCES_REFERENCE)
        self.assertIn("Wrapper path", APPROACH_WHEN_USER_ASKS)
        self.assertIn("gufi_route_query", APPROACH_WRAPPER)
        self.assertIn("validate_query_plan", APPROACH_PIPELINE)
        self.assertIn("STOP", WRAPPER_SATISFIED_RULES)
        self.assertIn("new_query_plan", WRAPPER_SATISFIED_RULES)
        self.assertIn("validate_query_plan", PIPELINE_WORKFLOW_STEPS)


if __name__ == "__main__":
    unittest.main()
