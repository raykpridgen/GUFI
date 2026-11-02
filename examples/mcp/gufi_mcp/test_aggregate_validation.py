"""Offline validation tests for gufi_mcp_util (no GUFI binaries or indexes required)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("SCHEMAFILE", str(ROOT / "schema_hints.json"))
os.environ.setdefault("GUFIVTLIB", "gufi_vt")
os.environ.setdefault("GUFI_INDEX_ROOT", str(ROOT))

import gufi_mcp_util as util  # noqa: E402


@pytest.fixture(autouse=True)
def _fake_indexes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(util, "GUFI_INDEX_ROOT", tmp_path)
    monkeypatch.setattr(util, "get_gufi_indexes", lambda: ["testidx", "flat"])
    monkeypatch.setattr(util, "has_treesummary", lambda index: index == "testidx")


def _payload(**overrides: object) -> dict:
    base = {
        "index": "testidx",
        "per_directory_shape": "size INT64",
        "per_directory_sql": ["SELECT size FROM vrpentries WHERE type = 'f'"],
        "partial_shape": "total INT64",
        "partial_sql": "SELECT SUM(size) FROM per_directory",
        "final_sql": "SELECT SUM(total) AS total FROM partial",
    }
    base.update(overrides)
    return base


def _invalid(match: str, **overrides: object) -> None:
    with pytest.raises(ValidationError, match=match):
        util.GufiAggregateQuery.model_validate(_payload(**overrides))


# Aggregate pipeline: accepted queries

@pytest.mark.parametrize("field", ["index", "per_directory_sql", "per_directory_shape", "partial_sql", "partial_shape", "final_sql"])
def test_every_field_is_required(field: str) -> None:
    payload = _payload()
    del payload[field]
    with pytest.raises(ValidationError, match=f"{field}\n  Field required"):
        util.GufiAggregateQuery.model_validate(payload)


def test_schema_has_no_defaults() -> None:
    schema = util.GufiAggregateQuery.model_json_schema()
    assert not [name for name, prop in schema["properties"].items() if "default" in prop]
    assert set(schema["required"]) == set(schema["properties"])


def test_build_query_string_has_every_phase(tmp_path: Path) -> None:
    sql = util.GufiAggregateQuery.model_validate(_payload()).build_query_string()
    assert sql.startswith(f"CREATE VIRTUAL TABLE {util.AGGREGATE_VTAB} USING gufi_vt(")
    assert f'index="{tmp_path / "testidx"}"' in sql
    assert 'I="CREATE TABLE per_directory(size INT64);"' in sql
    assert "E=\"INSERT INTO per_directory SELECT size FROM vrpentries WHERE type = 'f';\"" in sql
    assert 'K="CREATE TABLE partial(total INT64);"' in sql
    assert 'J="INSERT INTO partial SELECT SUM(size) FROM per_directory;"' in sql
    assert 'G="SELECT total FROM partial;"' in sql
    assert "T=" not in sql and "S=" not in sql


def test_per_directory_phase_follows_table() -> None:
    query = util.GufiAggregateQuery.model_validate(_payload(
        per_directory_shape="n INT64",
        per_directory_sql=[
            "SELECT inode FROM vrpentries WHERE type = 'f'",
            "SELECT totfiles FROM treesummary",
            "SELECT inode FROM vrsummary;",
        ],
        partial_sql="SELECT SUM(n) FROM per_directory",
    ))
    sql = query.build_query_string()
    assert 'T="INSERT INTO per_directory SELECT totfiles FROM treesummary;"' in sql
    assert 'S="INSERT INTO per_directory SELECT inode FROM vrsummary;"' in sql
    assert "E=\"INSERT INTO per_directory SELECT inode FROM vrpentries WHERE type = 'f';\"" in sql


def test_shape_is_normalized() -> None:
    query = util.GufiAggregateQuery.model_validate(_payload(
        per_directory_shape="  UID int64 ,size integer",
        per_directory_sql=["SELECT uid, size FROM pentries"],
        partial_sql="SELECT SUM(size) FROM per_directory GROUP BY uid",
    ))
    assert query.per_directory_shape == "uid INT64, size INTEGER"


def test_per_directory_path_column_and_final_window_are_allowed() -> None:
    util.GufiAggregateQuery.model_validate(_payload(
        per_directory_shape="path TEXT, uid INT64, size INT64",
        per_directory_sql=["SELECT path, uid, size FROM vrpentries WHERE type = 'f'"],
        partial_shape="path TEXT, uid INT64, size INT64",
        partial_sql="SELECT path, uid, size FROM per_directory",
        final_sql=("SELECT path, uid, size FROM (SELECT path, uid, size, ROW_NUMBER() OVER "
                   "(PARTITION BY uid ORDER BY size DESC) AS rn FROM partial) WHERE rn <= 10 ORDER BY uid, size DESC"),
    ))


def test_path_columns_become_a_derived_table(tmp_path: Path) -> None:
    root = str(tmp_path / "testidx")
    sql = util.GufiAggregateQuery.model_validate(_payload(
        per_directory_shape="path TEXT",
        per_directory_sql=["SELECT e.path FROM vrpentries e WHERE e.dirpath LIKE 'testidx/a/%'"],
        partial_shape="path TEXT", partial_sql="SELECT path FROM per_directory", final_sql="SELECT path FROM partial",
    )).build_query_string()
    derived = (f"(SELECT 'testidx' || substr(rpath(sname, sroll, name), {len(root) + 1}) AS path, "
               f"'testidx' || substr(rpath(sname, sroll), {len(root) + 1}) AS dirpath, * FROM vrpentries)")
    assert f"E=\"INSERT INTO per_directory SELECT e.path FROM {derived} e WHERE e.dirpath LIKE 'testidx/a/%';\"" in sql


def test_path_columns_expand_every_reference_and_keep_the_rest() -> None:
    sql = util.expand_path_columns(
        "SELECT path, substr(name, 2) FROM vrsummary WHERE size IN (SELECT size FROM VRsummary AS s WHERE s.path = 'x')",
        "test", lambda table, column: f"<{table}.{column}>")
    assert sql == ("SELECT path, substr(name, 2) FROM (SELECT <vrsummary.path> AS path, * FROM vrsummary) AS vrsummary "
                   "WHERE size IN (SELECT size FROM (SELECT <vrsummary.path> AS path, * FROM vrsummary) AS s WHERE s.path = 'x')")


def test_tables_without_path_columns_are_untouched() -> None:
    sql = "SELECT size FROM vrpentries WHERE type = 'f'"
    assert util.expand_path_columns(sql, "test", lambda t, c: "x") == sql


def test_spath_uses_source_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "GUFI_SOURCE_ROOT", "/lustre/proj")
    sql = util.GufiAggregateQuery.model_validate(_payload(
        per_directory_shape="p TEXT", per_directory_sql=["SELECT spath FROM vrsummary"],
        partial_shape="p TEXT", partial_sql="SELECT p FROM per_directory", final_sql="SELECT p FROM partial",
    )).build_query_string()
    assert 'p="/lustre/proj/testidx"' in sql
    assert "(SELECT spath(sname, sroll) AS spath, * FROM vrsummary) AS vrsummary" in sql


def test_spath_requires_source_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "GUFI_SOURCE_ROOT", None)
    _invalid("spath do not exist in vrpentries", per_directory_sql=["SELECT spath FROM vrpentries"], per_directory_shape="p TEXT")


def test_final_sql_may_order_by_alias() -> None:
    util.GufiAggregateQuery.model_validate(_payload(final_sql="SELECT SUM(total) AS t FROM partial ORDER BY t DESC LIMIT 5"))


def test_final_query_reads_materialized_partial() -> None:
    query = util.GufiAggregateQuery.model_validate(_payload())
    assert query.final_query_string() == (
        f"WITH partial AS MATERIALIZED (SELECT * FROM {util.AGGREGATE_VTAB}) SELECT SUM(total) AS total FROM partial"
    )


# Aggregate pipeline: rejected queries

def test_unknown_index_fails() -> None:
    _invalid("Unknown index", index="../etc")


def test_per_directory_must_not_be_empty() -> None:
    _invalid("at least 1", per_directory_sql=[])


@pytest.mark.parametrize("sql, message", [
    ("SELECT size FROM entries; ATTACH 'x' AS y", "exactly one SQL statement"),
    ("DELETE FROM entries", "only a single SELECT"),
    ("INSERT INTO per_directory SELECT size FROM entries", "only a single SELECT"),
    ("SELECT size FROM entries WHERE name = \"x\"", "double quotes"),
    ("SELECT size FROM entries ORDER BY size", "ORDER"),
    ("SELECT size FROM entries LIMIT 3", "LIMIT"),
    ("SELECT size FROM entries LIMIT 3 OFFSET 2", "LIMIT"),
    ("SELECT ROW_NUMBER() OVER (ORDER BY size) FROM entries", "ORDER BY is only allowed in final_sql"),
    ("SELECT size FROM vrpentries WHERE size IN (SELECT size FROM vrpentries ORDER BY size LIMIT 5)",
     "only allowed in final_sql"),
    ("SELECT sname FROM vrpentries", "sname is internal rollup bookkeeping"),
    ("SELECT size FROM vrpentries WHERE sroll = 0", "sroll is internal rollup bookkeeping"),
    ("SELECT rpath(sname, sroll, name) FROM vrpentries", "sname is internal rollup bookkeeping"),
    ("SELECT path() FROM vrpentries", r"path\(\) is not available"),
    ("SELECT path FROM entries", "Path columns exist only in vrpentries"),
    ("SELECT size FROM nosuchtable", "cannot be used in per_directory_sql"),
    ("SELECT size FROM main.entries", "schema-qualified"),
    ("SELECT e.size FROM entries e JOIN pentries p ON e.inode = p.inode", "exactly one table"),
    ("SELECT missing FROM entries", "missing do not exist in entries"),
    ("SELECT * FROM entries", r"SELECT \*"),
    ("SELECT size, uid FROM entries", "returns 2 column"),
    ("WITH x AS (SELECT size FROM entries) SELECT size FROM x", "WITH"),
])
def test_bad_per_directory_sql_fails(sql: str, message: str) -> None:
    _invalid(message, per_directory_sql=[sql])


def test_two_per_directory_statements_in_one_phase_fail() -> None:
    _invalid("more than one", per_directory_sql=["SELECT size FROM entries", "SELECT size FROM pentries"])


def test_treesummary_requires_index_support() -> None:
    _invalid("no treesummary", index="flat", per_directory_sql=["SELECT totsize FROM treesummary"])


@pytest.mark.parametrize("shape", ["size INT64); DROP TABLE x; --", "size", "size INT64, size TEXT", "", "1abc INT64"])
def test_bad_shape_fails(shape: str) -> None:
    _invalid("per_directory_shape", per_directory_shape=shape)


def test_gather_unknown_column_fails() -> None:
    _invalid("filesize do not exist in per_directory", partial_sql="SELECT SUM(filesize) FROM per_directory")


def test_gather_column_substring_is_not_enough() -> None:
    _invalid("size do not exist", per_directory_shape="filesize INT64",
             per_directory_sql=["SELECT size FROM entries"], partial_sql="SELECT SUM(size) FROM per_directory")


@pytest.mark.parametrize("partial_sql", [
    "SELECT SUM(size) FROM per_directory ORDER BY 1",
    "SELECT SUM(size) FROM per_directory LIMIT 1",
    "SELECT SUM(size) FROM (SELECT size FROM per_directory ORDER BY size DESC LIMIT 10)",
])
def test_gather_rejects_ordering(partial_sql: str) -> None:
    _invalid("only allowed in final_sql", partial_sql=partial_sql)


def test_path_functions_fail_in_later_stages() -> None:
    _invalid(r"rpath\(\) is not available", final_sql="SELECT rpath(total, total) FROM partial")


@pytest.mark.parametrize("function", sorted(util.BLOCKED_FUNCTIONS))
@pytest.mark.parametrize("field, template", [
    ("per_directory_sql", "SELECT {f}('id') FROM entries"),
    ("partial_sql", "SELECT {f}('id') FROM per_directory"),
    ("final_sql", "SELECT {f}('id') AS total FROM partial"),
])
def test_shell_functions_fail_in_every_stage(function: str, field: str, template: str) -> None:
    sql = template.format(f=function)
    _invalid(rf"{function}\(\) runs shell commands", **{field: [sql] if field == "per_directory_sql" else sql})


def test_gufi_functions_pass_validation() -> None:
    util.GufiAggregateQuery.model_validate(_payload(
        per_directory_shape="band TEXT",
        per_directory_sql=["SELECT bytecat(size) FROM vrpentries WHERE type = 'f'"],
        partial_shape="band TEXT, n INT64",
        partial_sql="SELECT band, COUNT(*) FROM per_directory GROUP BY band",
        final_sql="SELECT band, SUM(n) AS n FROM partial GROUP BY band",
    ))


def test_rollup_internals_cannot_be_carried() -> None:
    _invalid("sname, sroll is internal rollup bookkeeping", per_directory_shape="sname TEXT, sroll INT64")


def test_path_column_must_be_carried_to_later_stages() -> None:
    _invalid("Path columns come from", partial_sql="SELECT COUNT(path) FROM per_directory")


def test_partial_must_read_per_directory() -> None:
    _invalid("only from the per_directory", partial_sql="SELECT SUM(size) FROM entries")


def test_partial_width_must_match_partial_shape() -> None:
    _invalid("returns 2 column", partial_sql="SELECT SUM(size), COUNT(size) FROM per_directory")


def test_final_unknown_column_fails() -> None:
    _invalid("missing do not exist in partial", final_sql="SELECT missing FROM partial")


def test_final_alias_does_not_hide_unknown_column() -> None:
    _invalid("missing do not exist in partial", final_sql="SELECT missing AS missing FROM partial")


def test_final_must_read_partial() -> None:
    _invalid("only from the partial", final_sql="SELECT total FROM per_directory")


def test_extra_fields_fail() -> None:
    _invalid("Extra inputs", threads=99)


# gufi_vt watchdog

def test_watchdog_turns_overrun_into_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "TOOL_TIMEOUT", 0.05)
    with pytest.raises(util.GufiError, match="did not finish"):
        with util._gufi_vt_watchdog():
            time.sleep(0.2)


def test_watchdog_passes_through_fast_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "TOOL_TIMEOUT", 5)
    with util._gufi_vt_watchdog():
        pass


# Schema resource

def test_schema_hides_rollup_internals_and_lists_path_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "GUFI_SOURCE_ROOT", None)
    monkeypatch.setattr(util, "list_schema_objects", lambda: {"vrpentries": "view", "entries": "table"})
    rows = [["dname", "TEXT"], ["sname", "TEXT"], ["sroll", "INT64"], ["srollsubdirs", "INT64"], ["name", "TEXT"]]
    monkeypatch.setattr(util, "table_columns", lambda name: rows)
    assert list(util.describe_schema_object("vrpentries")) == ["path", "dirpath", "dname", "name"]
    assert "path" not in util.describe_schema_object("entries")
    assert util.schema_columns("vrpentries") >= {"path", "dirpath"}
    assert not util.schema_columns("vrpentries") & util.HIDDEN_COLUMNS


# Result models

def test_query_result_rejects_inconsistent_rows() -> None:
    with pytest.raises(ValidationError):
        util.GufiQueryResult(columns=["a"], rows=[[1, 2]], row_count=1)
    with pytest.raises(ValidationError):
        util.GufiQueryResult(columns=["a"], rows=[[1]], row_count=2)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
