"""Module 4: QueryPlan pipeline validation engine tests."""

from __future__ import annotations

import unittest

from gufi_mcp.query_plan import QueryPlanPipeline
from gufi_mcp.validation_engine import validate_pipeline_consistency
from gufi_util import parse_schema_registry, resolve_view_types

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[5]
SCHEMA = resolve_view_types(
    parse_schema_registry(
        str(REPO_ROOT / "gufi/examples/mcp/gufi_mcp/schemas.json")
    )
)


def _valid_aggregate() -> dict:
    return QueryPlanPipeline.total_file_size("notes").to_dict()


class TestPipelinePresenceRules(unittest.TestCase):
    def test_valid_aggregate_plan_passes(self) -> None:
        errors = validate_pipeline_consistency(_valid_aggregate())
        self.assertEqual(errors, [])

    def test_k_without_i_fails(self) -> None:
        plan = (
            QueryPlanPipeline.skeleton("notes", "bad")
            .set_stage("aggregate_create", "CREATE TABLE aggregate(total INT64);")
            .to_dict()
        )
        errors = validate_pipeline_consistency(plan)
        self.assertTrue(any("-K" in e and "-I" in e for e in errors))

    def test_j_without_k_fails(self) -> None:
        plan = (
            QueryPlanPipeline.skeleton("notes", "bad")
            .set_stage("init", "CREATE TABLE intermediate(size INT64);")
            .set_stage(
                "aggregate_insert",
                "INSERT INTO aggregate SELECT SUM(size) FROM intermediate;",
            )
            .to_dict()
        )
        errors = validate_pipeline_consistency(plan)
        self.assertTrue(any("-J" in e and "-K" in e for e in errors))

    def test_i_without_k_and_j_fails(self) -> None:
        plan = (
            QueryPlanPipeline.skeleton("notes", "bad")
            .set_stage("init", "CREATE TABLE intermediate(size INT64);")
            .set_stage(
                "entries_sql",
                "INSERT INTO intermediate SELECT size FROM vrpentries WHERE type = 'f';",
            )
            .to_dict()
        )
        errors = validate_pipeline_consistency(plan)
        self.assertTrue(any("aggregate_create" in e for e in errors))
        self.assertTrue(any("aggregate_insert" in e for e in errors))

    def test_listing_plan_without_init_passes(self) -> None:
        plan = (
            QueryPlanPipeline.skeleton("notes", "listing")
            .set_stage("entries_sql", "SELECT name FROM vrpentries LIMIT 10;")
            .to_dict()
        )
        self.assertEqual(validate_pipeline_consistency(plan), [])


class TestTableNameConsistency(unittest.TestCase):
    def test_mismatched_aggregate_insert_target_fails(self) -> None:
        plan = (
            QueryPlanPipeline.skeleton("notes", "bad names")
            .set_stage("init", "CREATE TABLE intermediate(size INT64);")
            .set_stage("aggregate_create", "CREATE TABLE aggregate(total INT64);")
            .set_stage(
                "aggregate_insert",
                "INSERT INTO wrong SELECT SUM(size) FROM intermediate;",
            )
            .set_stage("final_select", "SELECT total FROM aggregate;")
            .to_dict()
        )
        errors = validate_pipeline_consistency(plan)
        self.assertTrue(any("does not match aggregate table" in e for e in errors))

    def test_mismatched_intermediate_in_j_fails(self) -> None:
        plan = (
            QueryPlanPipeline.skeleton("notes", "bad intermediate")
            .set_stage("init", "CREATE TABLE intermediate(size INT64);")
            .set_stage("aggregate_create", "CREATE TABLE aggregate(total INT64);")
            .set_stage(
                "aggregate_insert",
                "INSERT INTO aggregate SELECT SUM(size) FROM staging;",
            )
            .set_stage("final_select", "SELECT total FROM aggregate;")
            .to_dict()
        )
        errors = validate_pipeline_consistency(plan)
        self.assertTrue(any("does not match intermediate table" in e for e in errors))

    def test_mismatched_final_select_from_fails(self) -> None:
        plan = (
            QueryPlanPipeline.skeleton("notes", "bad final")
            .set_stage("init", "CREATE TABLE intermediate(size INT64);")
            .set_stage("aggregate_create", "CREATE TABLE aggregate(total INT64);")
            .set_stage(
                "aggregate_insert",
                "INSERT INTO aggregate SELECT SUM(size) FROM intermediate;",
            )
            .set_stage("final_select", "SELECT total FROM staging;")
            .to_dict()
        )
        errors = validate_pipeline_consistency(plan)
        self.assertTrue(any("final_select" in e for e in errors))

    def test_matching_names_pass_via_full_validate(self) -> None:
        pipeline = QueryPlanPipeline(_valid_aggregate())
        report = pipeline.validate(SCHEMA)
        self.assertTrue(report.valid, report.errors)


if __name__ == "__main__":
    unittest.main()
