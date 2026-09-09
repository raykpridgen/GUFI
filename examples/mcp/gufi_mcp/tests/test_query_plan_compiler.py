"""Unit tests for QueryPlan → gufi_vt SQL compilation (Module 3)."""

from __future__ import annotations

import unittest

from gufi_mcp.query_plan import (
    QueryExecution,
    QueryOutput,
    QueryPipeline,
    QueryPlan,
    QueryPlanPipeline,
    compile_aggregate_sql,
    compile_listing_sql,
    escape_gufi_vt_module_value,
)

FIXTURE_INDEX = "/fixture/index/"


class TestEscapeGufiVtModuleValue(unittest.TestCase):
    def test_embedded_single_quote_in_sql_literal(self) -> None:
        sql = "INSERT INTO t SELECT name FROM vrpentries WHERE owner = 'O''Brien';"
        self.assertEqual(
            escape_gufi_vt_module_value(sql),
            '"INSERT INTO t SELECT name FROM vrpentries WHERE owner = \'O\'\'Brien\';"',
        )

    def test_embedded_double_quote_doubled(self) -> None:
        sql = 'SELECT "weird" AS label FROM vrpentries;'
        self.assertEqual(
            escape_gufi_vt_module_value(sql),
            '"SELECT ""weird"" AS label FROM vrpentries;"',
        )


class TestListingCompiler(unittest.TestCase):
    def test_plain_listing_rewrites_from_clause(self) -> None:
        plan = QueryPlan(
            index="notes",
            pipeline=QueryPipeline(
                entries_sql=(
                    "SELECT name, size FROM vrpentries "
                    "WHERE type = 'f' AND size > 1048576;"
                ),
            ),
            output=QueryOutput(row_limit=1000),
            execution=QueryExecution(threads=4),
        )
        expected = (
            "SELECT name, size FROM gufi_vt_vrpentries('/fixture/index/', 4) "
            "WHERE type = 'f' AND size > 1048576 LIMIT 1000;"
        )
        self.assertEqual(compile_listing_sql(plan, FIXTURE_INDEX), expected)

    def test_listing_respects_existing_limit(self) -> None:
        plan = QueryPlan(
            index="notes",
            pipeline=QueryPipeline(
                entries_sql="SELECT name FROM vrpentries WHERE type = 'f' LIMIT 5;",
            ),
            output=QueryOutput(row_limit=1000),
        )
        sql = compile_listing_sql(plan, FIXTURE_INDEX)
        self.assertNotIn("LIMIT 1000", sql)
        self.assertIn("LIMIT 5;", sql)


class TestAggregateCompiler(unittest.TestCase):
    def test_canonical_aggregate_pipeline(self) -> None:
        plan = QueryPlan(
            index="notes",
            pipeline=QueryPipeline(
                init="CREATE TABLE intermediate(uid INT64, size INT64);",
                entries_sql=(
                    "INSERT INTO intermediate SELECT uid, size FROM vrpentries "
                    "WHERE type = 'f';"
                ),
                aggregate_create="CREATE TABLE aggregate(uid INT64, total INT64);",
                aggregate_insert=(
                    "INSERT INTO aggregate SELECT uid, SUM(size) "
                    "FROM intermediate GROUP BY uid;"
                ),
                final_select=(
                    "SELECT uid, total FROM aggregate ORDER BY total DESC LIMIT 10;"
                ),
            ),
            execution=QueryExecution(threads=4),
        )
        compiled = compile_aggregate_sql(plan, FIXTURE_INDEX, table_suffix="a1b2c3")
        self.assertEqual(len(compiled), 3)
        self.assertEqual(compiled[1], "SELECT * FROM temp.plan_a1b2c3;")
        self.assertEqual(compiled[2], "DROP TABLE temp.plan_a1b2c3;")
        self.assertEqual(
            compiled[0],
            (
                "CREATE VIRTUAL TABLE temp.plan_a1b2c3 USING gufi_vt("
                "index='/fixture/index/', threads=4, "
                'I="CREATE TABLE intermediate(uid INT64, size INT64);", '
                "E=\"INSERT INTO intermediate SELECT uid, size FROM vrpentries "
                "WHERE type = 'f';\", "
                'K="CREATE TABLE aggregate(uid INT64, total INT64);", '
                'J="INSERT INTO aggregate SELECT uid, SUM(size) FROM intermediate '
                'GROUP BY uid;", '
                'G="SELECT uid, total FROM aggregate ORDER BY total DESC LIMIT 10"'
                ");"
            ),
        )

    def test_embedded_single_quote_in_aggregate_stage(self) -> None:
        plan = QueryPlan(
            index="notes",
            pipeline=QueryPipeline(
                init="CREATE TABLE intermediate(uid INT64, size INT64);",
                entries_sql=(
                    "INSERT INTO intermediate SELECT uid, size FROM vrpentries "
                    "WHERE type = 'f' AND name = 'O''Brien';"
                ),
                aggregate_create="CREATE TABLE aggregate(uid INT64, total INT64);",
                aggregate_insert=(
                    "INSERT INTO aggregate SELECT uid, SUM(size) "
                    "FROM intermediate GROUP BY uid;"
                ),
                final_select="SELECT uid, total FROM aggregate;",
            ),
            output=QueryOutput(row_limit=25),
            execution=QueryExecution(threads=4),
        )
        compiled = compile_aggregate_sql(plan, FIXTURE_INDEX, table_suffix="quote1")
        create = compiled[0]
        self.assertIn(
            "E=\"INSERT INTO intermediate SELECT uid, size FROM vrpentries "
            "WHERE type = 'f' AND name = 'O''Brien';\"",
            create,
        )
        self.assertIn('G="SELECT uid, total FROM aggregate LIMIT 25"', create)


class TestQueryPlanPipelineCompileSql(unittest.TestCase):
    def test_pipeline_routes_listing_vs_aggregate(self) -> None:
        listing = QueryPlanPipeline(
            QueryPlan(
                pipeline=QueryPipeline(
                    entries_sql="SELECT name FROM vrpentries WHERE type = 'f';",
                ),
                output=QueryOutput(row_limit=10),
            )
        )
        aggregate = QueryPlanPipeline.total_file_size("notes")

        self.assertEqual(len(listing.compile_sql(FIXTURE_INDEX)), 1)
        self.assertEqual(len(aggregate.compile_sql(FIXTURE_INDEX)), 3)


if __name__ == "__main__":
    unittest.main()
