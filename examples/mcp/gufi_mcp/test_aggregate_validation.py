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
        "scatter_shape": "size INT64",
        "scatter_sql": ["SELECT size FROM vrpentries WHERE type = 'f'"],
        "gather_shape": "total INT64",
        "gather_sql": "SELECT SUM(size) FROM intermediate",
        "final_sql": "SELECT SUM(total) AS total FROM aggregate",
    }
    base.update(overrides)
    return base


def _invalid(match: str, **overrides: object) -> None:
    with pytest.raises(ValidationError, match=match):
        util.GufiAggregateQuery.model_validate(_payload(**overrides))


# Aggregate pipeline: accepted queries

@pytest.mark.parametrize("field", ["index", "scatter_sql", "scatter_shape", "gather_sql", "gather_shape", "final_sql"])
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
    assert 'I="CREATE TABLE intermediate(size INT64);"' in sql
    assert "E=\"INSERT INTO intermediate SELECT size FROM vrpentries WHERE type = 'f';\"" in sql
    assert 'K="CREATE TABLE aggregate(total INT64);"' in sql
    assert 'J="INSERT INTO aggregate SELECT SUM(size) FROM intermediate;"' in sql
    assert 'G="SELECT total FROM aggregate;"' in sql
    assert "T=" not in sql and "S=" not in sql


def test_scatter_phase_follows_table() -> None:
    query = util.GufiAggregateQuery.model_validate(_payload(
        scatter_shape="n INT64",
        scatter_sql=[
            "SELECT inode FROM vrpentries WHERE type = 'f'",
            "SELECT totfiles FROM treesummary",
            "SELECT inode FROM vrsummary;",
        ],
        gather_sql="SELECT SUM(n) FROM intermediate",
    ))
    sql = query.build_query_string()
    assert 'T="INSERT INTO intermediate SELECT totfiles FROM treesummary;"' in sql
    assert 'S="INSERT INTO intermediate SELECT inode FROM vrsummary;"' in sql
    assert "E=\"INSERT INTO intermediate SELECT inode FROM vrpentries WHERE type = 'f';\"" in sql


def test_shape_is_normalized() -> None:
    query = util.GufiAggregateQuery.model_validate(_payload(
        scatter_shape="  UID int64 ,size integer",
        scatter_sql=["SELECT uid, size FROM pentries"],
        gather_sql="SELECT SUM(size) FROM intermediate GROUP BY uid",
    ))
    assert query.scatter_shape == "uid INT64, size INTEGER"


def test_scatter_path_column_and_final_window_are_allowed() -> None:
    util.GufiAggregateQuery.model_validate(_payload(
        scatter_shape="path TEXT, uid INT64, size INT64",
        scatter_sql=["SELECT path, uid, size FROM vrpentries WHERE type = 'f'"],
        gather_shape="path TEXT, uid INT64, size INT64",
        gather_sql="SELECT path, uid, size FROM intermediate",
        final_sql=("SELECT path, uid, size FROM (SELECT path, uid, size, ROW_NUMBER() OVER "
                   "(PARTITION BY uid ORDER BY size DESC) AS rn FROM aggregate) WHERE rn <= 10 ORDER BY uid, size DESC"),
    ))


def test_path_columns_become_a_derived_table(tmp_path: Path) -> None:
    root = str(tmp_path / "testidx")
    sql = util.GufiAggregateQuery.model_validate(_payload(
        scatter_shape="path TEXT",
        scatter_sql=["SELECT e.path FROM vrpentries e WHERE e.dirpath LIKE 'testidx/a/%'"],
        gather_shape="path TEXT", gather_sql="SELECT path FROM intermediate", final_sql="SELECT path FROM aggregate",
    )).build_query_string()
    derived = (f"(SELECT 'testidx' || substr(rpath(sname, sroll, name), {len(root) + 1}) AS path, "
               f"'testidx' || substr(rpath(sname, sroll), {len(root) + 1}) AS dirpath, * FROM vrpentries)")
    assert f"E=\"INSERT INTO intermediate SELECT e.path FROM {derived} e WHERE e.dirpath LIKE 'testidx/a/%';\"" in sql


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
        scatter_shape="p TEXT", scatter_sql=["SELECT spath FROM vrsummary"],
        gather_shape="p TEXT", gather_sql="SELECT p FROM intermediate", final_sql="SELECT p FROM aggregate",
    )).build_query_string()
    assert 'p="/lustre/proj/testidx"' in sql
    assert "(SELECT spath(sname, sroll) AS spath, * FROM vrsummary) AS vrsummary" in sql


def test_spath_requires_source_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "GUFI_SOURCE_ROOT", None)
    _invalid("spath do not exist in vrpentries", scatter_sql=["SELECT spath FROM vrpentries"], scatter_shape="p TEXT")


def test_final_sql_may_order_by_alias() -> None:
    util.GufiAggregateQuery.model_validate(_payload(final_sql="SELECT SUM(total) AS t FROM aggregate ORDER BY t DESC LIMIT 5"))


def test_final_query_reads_materialized_aggregate() -> None:
    query = util.GufiAggregateQuery.model_validate(_payload())
    assert query.final_query_string() == (
        f"WITH aggregate AS MATERIALIZED (SELECT * FROM {util.AGGREGATE_VTAB}) SELECT SUM(total) AS total FROM aggregate"
    )


# Aggregate pipeline: rejected queries

def test_unknown_index_fails() -> None:
    _invalid("Unknown index", index="../etc")


def test_scatter_must_not_be_empty() -> None:
    _invalid("at least 1", scatter_sql=[])


@pytest.mark.parametrize("sql, message", [
    ("SELECT size FROM entries; ATTACH 'x' AS y", "exactly one SQL statement"),
    ("DELETE FROM entries", "only a single SELECT"),
    ("INSERT INTO intermediate SELECT size FROM entries", "only a single SELECT"),
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
    ("SELECT size FROM nosuchtable", "cannot be used in scatter_sql"),
    ("SELECT size FROM main.entries", "schema-qualified"),
    ("SELECT e.size FROM entries e JOIN pentries p ON e.inode = p.inode", "exactly one table"),
    ("SELECT missing FROM entries", "missing do not exist in entries"),
    ("SELECT * FROM entries", r"SELECT \*"),
    ("SELECT size, uid FROM entries", "returns 2 column"),
    ("WITH x AS (SELECT size FROM entries) SELECT size FROM x", "WITH"),
])
def test_bad_scatter_sql_fails(sql: str, message: str) -> None:
    _invalid(message, scatter_sql=[sql])


def test_two_scatter_statements_in_one_phase_fail() -> None:
    _invalid("more than one", scatter_sql=["SELECT size FROM entries", "SELECT size FROM pentries"])


def test_treesummary_requires_index_support() -> None:
    _invalid("no treesummary", index="flat", scatter_sql=["SELECT totsize FROM treesummary"])


@pytest.mark.parametrize("shape", ["size INT64); DROP TABLE x; --", "size", "size INT64, size TEXT", "", "1abc INT64"])
def test_bad_shape_fails(shape: str) -> None:
    _invalid("scatter_shape", scatter_shape=shape)


def test_gather_unknown_column_fails() -> None:
    _invalid("filesize do not exist in intermediate", gather_sql="SELECT SUM(filesize) FROM intermediate")


def test_gather_column_substring_is_not_enough() -> None:
    _invalid("size do not exist", scatter_shape="filesize INT64",
             scatter_sql=["SELECT size FROM entries"], gather_sql="SELECT SUM(size) FROM intermediate")


@pytest.mark.parametrize("gather_sql", [
    "SELECT SUM(size) FROM intermediate ORDER BY 1",
    "SELECT SUM(size) FROM intermediate LIMIT 1",
    "SELECT SUM(size) FROM (SELECT size FROM intermediate ORDER BY size DESC LIMIT 10)",
])
def test_gather_rejects_ordering(gather_sql: str) -> None:
    _invalid("only allowed in final_sql", gather_sql=gather_sql)


def test_path_functions_fail_in_later_stages() -> None:
    _invalid(r"rpath\(\) is not available", final_sql="SELECT rpath(total, total) FROM aggregate")


def test_rollup_internals_cannot_be_carried() -> None:
    _invalid("sname, sroll is internal rollup bookkeeping", scatter_shape="sname TEXT, sroll INT64")


def test_path_column_must_be_carried_to_later_stages() -> None:
    _invalid("Path columns come from", gather_sql="SELECT COUNT(path) FROM intermediate")


def test_gather_must_read_intermediate() -> None:
    _invalid("only from the intermediate", gather_sql="SELECT SUM(size) FROM entries")


def test_gather_width_must_match_aggregate_shape() -> None:
    _invalid("returns 2 column", gather_sql="SELECT SUM(size), COUNT(size) FROM intermediate")


def test_final_unknown_column_fails() -> None:
    _invalid("missing do not exist in aggregate", final_sql="SELECT missing FROM aggregate")


def test_final_alias_does_not_hide_unknown_column() -> None:
    _invalid("missing do not exist in aggregate", final_sql="SELECT missing AS missing FROM aggregate")


def test_final_must_read_aggregate() -> None:
    _invalid("only from the aggregate", final_sql="SELECT total FROM intermediate")


def test_extra_fields_fail() -> None:
    _invalid("Extra inputs", threads=99)


# sql_file_index rewriting

def test_vt_select_rewrites_table(tmp_path: Path) -> None:
    sql, params = util.build_gufi_vt_select(
        "SELECT name, size FROM vrpentries WHERE type = 'f' ORDER BY size DESC LIMIT 3;", "testidx")
    assert sql == (
        'WITH __gufi_mcp_source__ AS MATERIALIZED (SELECT "name", "size", "type" '
        "FROM gufi_vt_vrpentries(?, ?, NULL, NULL, NULL, 0)) "
        "SELECT name, size FROM __gufi_mcp_source__ AS vrpentries WHERE type = 'f' ORDER BY size DESC LIMIT 3"
    )
    assert params == (str(tmp_path / "testidx"), util.THREADS)


def test_vt_select_keeps_alias_and_prefix() -> None:
    sql, _ = util.build_gufi_vt_select("SELECT p.name FROM gufi_vt_pentries p", "testidx")
    assert "FROM gufi_vt_pentries(" in sql and "__gufi_mcp_source__ AS p" in sql


def test_vt_select_star_reads_visible_columns_and_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(util._GUFI_VT_COLUMN_MAPS, "vrsummary", None)
    sql, _ = util.build_gufi_vt_select("SELECT * FROM vrsummary", "testidx")
    source = sql[:sql.index("FROM gufi_vt_vrsummary(")]
    assert '"dname", "name", "type"' in source and '"totfiles"' in source
    assert '"sname"' not in source and '"sroll"' not in source and '"srollsubdirs"' not in source
    assert source.endswith("""'testidx' || substr("rpath", %d) AS "path" """ % (len(str(util.GUFI_INDEX_ROOT / "testidx")) + 1))


def test_vt_select_builds_paths_from_the_rpath_column(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(util._GUFI_VT_COLUMN_MAPS, "vrpentries", None)
    sql, _ = util.build_gufi_vt_select("SELECT path FROM vrpentries WHERE dirpath = 'testidx/a'", "testidx")
    start = len(str(tmp_path / "testidx")) + 1
    assert (f"""(SELECT 'testidx' || substr("rpath", {start}) AS "dirpath", """
            f"""'testidx' || substr("rpath", {start}) || '/' || "name" AS "path" FROM gufi_vt_vrpentries(""") in sql
    assert sql.endswith("SELECT path FROM __gufi_mcp_source__ AS vrpentries WHERE dirpath = 'testidx/a'")


def test_vt_select_spath(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(util._GUFI_VT_COLUMN_MAPS, "vrsummary", None)
    monkeypatch.setattr(util, "GUFI_SOURCE_ROOT", "/lustre/proj")
    sql, _ = util.build_gufi_vt_select("SELECT spath FROM vrsummary", "testidx")
    assert f"""'/lustre/proj/testidx' || substr("rpath", {len(str(tmp_path / 'testidx')) + 1}) AS "spath" """ in sql


def test_vt_select_remote_paths_use_the_remote_index_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "REMOTEHOST", "user@host")
    monkeypatch.setitem(util._GUFI_VT_COLUMN_MAPS, "vrsummary", None)
    sql, params = util.build_gufi_vt_select("SELECT path FROM vrsummary", "/srv/search/proj/", remote=True)
    assert """'proj' || substr("rpath", 17) AS "path" """ in sql
    assert params[0] == "/srv/search/proj"


def test_vt_select_remaps_shifted_vt_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    view = list(util.get_static_schema()["vrsummary"]["columns"])
    shifted = [c for c in view if c != "crtime"]
    monkeypatch.setitem(util._GUFI_VT_COLUMN_MAPS, "vrsummary", dict(zip(view, shifted)))
    sql, _ = util.build_gufi_vt_select("SELECT name, totfiles FROM vrsummary", "testidx")
    assert '(SELECT "name", "totlinks" AS "totfiles" FROM gufi_vt_vrsummary(' in sql
    with pytest.raises(util.GufiError, match="isrolledup cannot be read"):
        util.build_gufi_vt_select("SELECT isrolledup FROM vrsummary", "testidx")


def test_vt_select_reads_entries_through_pentries() -> None:
    sql, _ = util.build_gufi_vt_select("SELECT name FROM entries", "testidx")
    assert "FROM gufi_vt_pentries(" in sql and "__gufi_mcp_source__ AS entries" in sql


def test_watchdog_turns_overrun_into_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "TOOL_TIMEOUT", 0.05)
    with pytest.raises(util.GufiError, match="did not finish"):
        with util._gufi_vt_watchdog():
            time.sleep(0.2)


def test_watchdog_passes_through_fast_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "TOOL_TIMEOUT", 5)
    with util._gufi_vt_watchdog():
        pass


def test_vt_select_preserves_literal_case() -> None:
    sql, _ = util.build_gufi_vt_select("SELECT name FROM pentries WHERE name = 'README.md'", "testidx")
    assert "'README.md'" in sql


@pytest.mark.parametrize("sql, message", [
    ("SELECT name FROM pentries WHERE name IN (SELECT name FROM pentries)", "only be referenced once"),
    ("SELECT name FROM vsummarydir", "not queryable"),
    ("SELECT nope FROM pentries", "nope do not exist"),
    ("SELECT name FROM pentries WHERE size > ?", "bind parameters"),
    ("SELECT 1", "exactly one table"),
    ("SELECT rpath(sname, sroll, name) FROM vrpentries", "sname is internal rollup bookkeeping"),
    ("SELECT sname, name FROM vrpentries", "sname is internal rollup bookkeeping"),
    ("SELECT fpath() FROM vrpentries", r"fpath\(\) is not available"),
    ("SELECT path FROM pentries", "Path columns exist only in vrpentries"),
])
def test_vt_select_rejects(sql: str, message: str) -> None:
    with pytest.raises(util.GufiError, match=message):
        util.build_gufi_vt_select(sql, "testidx")


def test_vt_select_rejects_unknown_index() -> None:
    with pytest.raises(util.GufiError, match="Unknown index"):
        util.build_gufi_vt_select("SELECT name FROM pentries", "nope")


def test_remote_requires_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "REMOTEHOST", None)
    with pytest.raises(util.GufiError, match="REMOTEHOST"):
        util.build_gufi_vt_select("SELECT name FROM pentries", "idx", remote=True)


def test_remote_rejects_unsafe_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "REMOTEHOST", "user@host")
    with pytest.raises(util.GufiError, match="plain path"):
        util.build_gufi_vt_select("SELECT name FROM pentries", "../x", remote=True)


# Command-line tool arguments

def test_tool_command_keeps_zero_and_repeats_lists() -> None:
    args = util.GufiToolArgsFind(paths=["idx"], maxdepth=0, uid=0, file_type=["f", "d"], size="-2k", empty=True)
    cmd = util.build_tool_command("gufi_find", args, util.args_find_dict, leading=args.paths)
    assert cmd == ["gufi_find", "idx", "-maxdepth=0", "-empty", "-size=-2k", "-type=f", "-type=d", "-uid=0"]


def test_tool_command_uses_enum_value() -> None:
    args = util.GufiToolArgsStats(statistic="depth", order="DESC")
    cmd = util.build_tool_command("gufi_stats", args, util.args_stats_dict, trailing=[args.statistic.value])
    assert cmd == ["gufi_stats", "--order=DESC", "depth"]


@pytest.mark.parametrize("model, field", [
    (util.GufiToolArgsLS, "path"),
    (util.GufiToolArgsDU, "paths"),
    (util.GufiToolArgsStat, "files"),
    (util.GufiToolArgsFind, "paths"),
    (util.GufiToolArgsGetfattr, "paths"),
    (util.GufiToolArgsStats, "path"),
])
def test_paths_cannot_look_like_options(model: type, field: str) -> None:
    value = "--help" if field == "path" else ["--help"]
    with pytest.raises(ValidationError, match="must not start with '-'"):
        model(**{field: value})


@pytest.mark.parametrize("field", ["fprint", "print0", "skip_file", "delim"])
def test_removed_find_options_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        util.GufiToolArgsFind(**{field: "x"})


@pytest.mark.parametrize("value, ok", [("+5", True), ("-5", True), (7, True), ("5d", False), ("", False)])
def test_find_numbers(value: object, ok: bool) -> None:
    if ok:
        util.GufiToolArgsFind(mtime=value)
    else:
        with pytest.raises(ValidationError):
            util.GufiToolArgsFind(mtime=value)


@pytest.mark.parametrize("model, flags", [
    (util.GufiToolArgsLS, util.args_ls_dict),
    (util.GufiToolArgsDU, util.args_du_dict),
    (util.GufiToolArgsStat, util.args_stat_dict),
    (util.GufiToolArgsStats, util.args_stats_dict),
    (util.GufiToolArgsGetfattr, util.args_getfattr_dict),
    (util.GufiToolArgsFind, util.args_find_dict),
])
def test_flag_maps_cover_only_model_fields(model: type, flags: dict) -> None:
    assert set(flags) <= set(model.model_fields)
    # Every valued option must use a long flag so it can be passed as --flag=value.
    for field, flag in flags.items():
        if model.model_fields[field].annotation is not bool:
            assert len(flag) > 2, field


def test_tool_models_build_with_defaults() -> None:
    for model in (util.GufiToolArgsLS, util.GufiToolArgsDU, util.GufiToolArgsStat,
                  util.GufiToolArgsStats, util.GufiToolArgsGetfattr, util.GufiToolArgsFind):
        model()


def test_cli_output_paths_are_index_relative(tmp_path: Path) -> None:
    text = f"  File: '{tmp_path}/idx/a'\n8618796 {tmp_path}/idx\nidx/b\n/other{tmp_path}/idx/c"
    assert util.relative_index_paths(text) == f"  File: 'idx/a'\n8618796 idx\nidx/b\n/other{tmp_path}/idx/c"


# Schema resource

def test_schema_hides_rollup_internals_and_lists_path_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "GUFI_SOURCE_ROOT", None)
    monkeypatch.setattr(util, "list_schema_objects", lambda: {"vrpentries": "view", "entries": "table"})
    rows = [["dname", "TEXT"], ["sname", "TEXT"], ["sroll", "INT64"], ["srollsubdirs", "INT64"], ["name", "TEXT"]]
    monkeypatch.setattr(util, "run_db_sql", lambda *a: util.GufiQueryResult(columns=["name", "type"], rows=rows, row_count=len(rows)))
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
