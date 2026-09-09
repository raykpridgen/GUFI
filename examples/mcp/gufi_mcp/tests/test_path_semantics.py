"""gufi_vt listing vs aggregate path semantics tests."""

from __future__ import annotations

import unittest

from gufi_mcp.path_semantics import (
    rewrite_listing_path_expressions,
    validate_listing_path_semantics,
)
from gufi_mcp.query_plan import QueryPlanPipeline, compile_listing_sql
from gufi_util import parse_schema_registry, resolve_view_types

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[5]
SCHEMA = resolve_view_types(
    parse_schema_registry(
        str(REPO_ROOT / "gufi/examples/mcp/gufi_mcp/schemas.json")
    )
)
FIXTURE_INDEX = str(REPO_ROOT / "gufi/local/search/notes/")


class TestRewriteListingPaths(unittest.TestCase):
    def test_rpath_three_arg_rewrites_to_column_concat(self) -> None:
        sql = (
            "SELECT rpath(sname, sroll, name), size FROM vrpentries "
            "WHERE type = 'f' ORDER BY size DESC"
        )
        out = rewrite_listing_path_expressions(sql)
        self.assertIn("(rpath || '/' || name)", out)
        self.assertNotIn("rpath(sname", out)

    def test_rpath_two_arg_rewrites_to_column(self) -> None:
        sql = "SELECT rpath(sname, sroll), totfiles FROM vrsummary"
        out = rewrite_listing_path_expressions(sql)
        self.assertIn("SELECT rpath, totfiles", out)

    def test_level_zero_arg_rewrites_to_column(self) -> None:
        sql = "SELECT level(), name FROM vrpentries"
        out = rewrite_listing_path_expressions(sql)
        self.assertIn("SELECT level, name", out)


class TestValidateListingPathSemantics(unittest.TestCase):
    def test_listing_with_rpath_udf_warns_and_stays_valid(self) -> None:
        plan = (
            QueryPlanPipeline.skeleton("notes", "large files with paths")
            .set_stage(
                "entries_sql",
                "SELECT rpath(sname, sroll, name), size FROM vrpentries "
                "WHERE type = 'f' ORDER BY size DESC",
            )
            .update(output={"row_limit": 100})
        )
        errors, warnings = validate_listing_path_semantics(plan.plan)
        self.assertEqual(errors, [])
        self.assertTrue(any("rewrit" in w.lower() for w in warnings))

        report = plan.validate(SCHEMA)
        self.assertTrue(report.valid, report.errors)
        self.assertTrue(any("rewrit" in w.lower() for w in report.warnings))

    def test_listing_with_unsupported_rpath_form_errors(self) -> None:
        plan = (
            QueryPlanPipeline.skeleton("notes", "bad rpath")
            .set_stage(
                "entries_sql",
                "SELECT rpath(foo, bar, baz), size FROM vrpentries WHERE type = 'f'",
            )
            .update(output={"row_limit": 10})
        )
        errors, _warnings = validate_listing_path_semantics(plan.plan)
        self.assertTrue(errors)
        report = plan.validate(SCHEMA)
        self.assertFalse(report.valid)

    def test_aggregate_plan_allows_rpath_in_entries_sql(self) -> None:
        plan = QueryPlanPipeline.total_file_size("notes")
        errors, warnings = validate_listing_path_semantics(plan.plan)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_listing_column_form_validates_clean(self) -> None:
        plan = (
            QueryPlanPipeline.skeleton("notes", "paths via columns")
            .set_stage(
                "entries_sql",
                "SELECT (rpath || '/' || name) AS path, size FROM vrpentries "
                "WHERE type = 'f' ORDER BY size DESC",
            )
            .update(output={"row_limit": 5})
        )
        report = plan.validate(SCHEMA)
        self.assertTrue(report.valid, report.errors)
        self.assertFalse(any("rpath" in w and "rewrit" in w for w in report.warnings))


class TestCompileListingRewrite(unittest.TestCase):
    def test_compiler_rewrites_rpath_before_vt_call(self) -> None:
        plan = QueryPlanPipeline(
            {
                "index": "notes",
                "pipeline": {
                    "entries_sql": (
                        "SELECT rpath(sname, sroll, name), size FROM vrpentries "
                        "WHERE type = 'f' ORDER BY size DESC"
                    ),
                },
                "output": {"row_limit": 3},
                "execution": {"threads": 4},
            }
        ).plan
        sql = compile_listing_sql(plan, FIXTURE_INDEX)
        self.assertIn("gufi_vt_vrpentries", sql)
        self.assertIn("(rpath || '/' || name)", sql)
        self.assertNotIn("rpath(sname", sql)


if __name__ == "__main__":
    unittest.main()
