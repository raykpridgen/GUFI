"""Offline tests for the server ablation switches (gufi_ablation, gufi_plain, build_server) and the
grading changes they need (alternates, index-root path normalization)."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("SCHEMAFILE", str(ROOT / "schema_hints.json"))
os.environ.setdefault("GUFIVTLIB", "gufi_vt")
os.environ.setdefault("GUFI_INDEX_ROOT", str(ROOT))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import gufi_mcp_server as server  # noqa: E402
import gufi_mcp_util as util  # noqa: E402
import gufi_plain  # noqa: E402
from gufi_ablation import CHOICES, FULL, AblationConfig, AblationError  # noqa: E402

ALL_CONFIGS = [AblationConfig.from_mapping(dict(zip(CHOICES, values))) for values in itertools.product(*CHOICES.values())]
SCENARIOS = yaml.safe_load((ROOT / "eval" / "scenarios.single.yaml").read_text())


@pytest.fixture(autouse=True)
def _fake_indexes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(util, "GUFI_INDEX_ROOT", tmp_path)
    monkeypatch.setattr(util, "get_gufi_indexes", lambda: ["eval_master", "testidx"])
    monkeypatch.setattr(util, "has_treesummary", lambda index: False)
    for cached in (util.get_static_schema, util.path_column_hints, util.function_catalog, util._column_tags):
        cached.cache_clear()


# Config

def test_defaults_are_the_full_server() -> None:
    assert AblationConfig.from_env({}) == FULL
    assert FULL.is_full and FULL.expected_tools() == [
        "aggregate_sql_query", "gufi_indexes", "gufi_schemas", "gufi_functions", "gufi_function"]


def test_config_reads_env_and_round_trips() -> None:
    env = {"GUFI_ABLATION_SUBMISSION": "plain", "GUFI_ABLATION_SCHEMA": "naive", "GUFI_ABLATION_PRIMER": "0",
           "GUFI_ABLATION_FUNCTIONS": "off", "GUFI_ABLATION_PATH": "rpath"}
    cfg = AblationConfig.from_env(env)
    assert cfg == AblationConfig("plain", "naive", False, False, "rpath")
    assert AblationConfig.from_env(cfg.env()) == cfg
    assert cfg.id == "plain-naive-p0-f0-rpath"
    assert len({c.id for c in ALL_CONFIGS}) == 48


@pytest.mark.parametrize("env", [{"GUFI_ABLATION_SCHEMA": "rich"}, {"GUFI_ABLATION_PRIMER": "maybe"},
                                 {"GUFI_ABLATION_PATH": "client"}])
def test_bad_values_fail_fast(env: dict[str, str]) -> None:
    with pytest.raises(AblationError):
        AblationConfig.from_env(env)


def test_unknown_factor_is_rejected() -> None:
    with pytest.raises(AblationError, match="unknown"):
        AblationConfig.from_mapping({"primers": 1})


# Server surface: tools, primer, descriptions

def _surface(cfg: AblationConfig) -> tuple[list[str], str]:
    srv = server.build_server(cfg)
    tools = asyncio.run(srv.list_tools())
    text = (srv.instructions or "") + " " + json.dumps([t.model_dump(mode="json") for t in tools])
    return [t.name for t in tools], text


@pytest.mark.parametrize("cfg", ALL_CONFIGS, ids=lambda c: c.id)
def test_server_exposes_exactly_the_configured_parts(cfg: AblationConfig) -> None:
    names, text = _surface(cfg)
    assert names == cfg.expected_tools()
    if cfg.schema == "none":
        assert "gufi_schemas" not in text
    if not cfg.functions:
        assert "gufi_function" not in text
    if cfg.path == "rpath":
        assert "dirpath" not in text and "path columns" not in text
    else:
        assert not re.search(r"\brpath\(", text)
    assert bool(server.assemble_primer(cfg)) == cfg.primer


def test_plain_tool_takes_a_command_string() -> None:
    srv = server.build_server(AblationConfig(submission="plain"))
    tool = next(t for t in asyncio.run(srv.list_tools()) if t.name == "aggregate_sql_query")
    assert list(tool.input_schema["properties"]) == ["command"]
    assert "per_directory_shape" not in json.dumps(tool.input_schema)


def test_no_ground_truth_statement_is_shown_under_any_config() -> None:
    statements = [" ".join(sql.lower().split()) for s in SCENARIOS
                  for q in [s["ground_truth"], *s.get("alternates", [])]
                  for sql in [*q["per_directory_sql"], q["final_sql"]]]
    for cfg in ALL_CONFIGS:
        visible = " ".join(_surface(cfg)[1].lower().split())
        for sql in statements:
            assert sql not in visible, f"{cfg.id} shows {sql}"


# Schema levels and path mode

ROWS = [["name", "TEXT"], ["size", "INT64"], ["mtime", "INT64"], ["sname", "TEXT"], ["sroll", "INT64"],
        ["srollsubdirs", "INT64"]]


def _describe(monkeypatch: pytest.MonkeyPatch, cfg: AblationConfig) -> dict:
    monkeypatch.setattr(util, "CONFIG", cfg)
    monkeypatch.setattr(util, "list_schema_objects", lambda: {"vrpentries": "view"})
    monkeypatch.setattr(util, "table_columns", lambda name: ROWS)
    return util.describe_schema_object("vrpentries")


def test_naive_schema_has_types_only(monkeypatch: pytest.MonkeyPatch) -> None:
    described = _describe(monkeypatch, AblationConfig(schema="naive"))
    assert all(set(info) == {"type"} for info in described.values())
    assert {"path", "dirpath", "size"} <= set(described)


def test_function_tags_follow_the_functions_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "Functions:" in _describe(monkeypatch, FULL)["size"]["hint"]
    assert "Functions:" not in _describe(monkeypatch, AblationConfig(functions=False))["size"]["hint"]


def test_rpath_mode_shows_rpath_inputs_instead_of_path_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    described = _describe(monkeypatch, AblationConfig(path="rpath"))
    assert "path" not in described and "dirpath" not in described and "srollsubdirs" not in described
    assert "rpath(sname, sroll, name)" in described["sname"]["hint"]
    server_mode = _describe(monkeypatch, FULL)
    assert "sname" not in server_mode and "path" in server_mode


def _query(**overrides: object) -> dict:
    base = {"index": "eval_master", "per_directory_shape": "p TEXT, size INT64",
            "per_directory_sql": ["SELECT rpath(sname, sroll, name), size FROM vrpentries WHERE type = 'f'"],
            "partial_shape": "p TEXT, size INT64", "partial_sql": "SELECT p, size FROM per_directory",
            "final_sql": "SELECT p, size FROM partial ORDER BY size DESC LIMIT 3"}
    base.update(overrides)
    return base


def test_rpath_is_rejected_in_server_mode_and_allowed_per_directory_in_rpath_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "CONFIG", FULL)
    with pytest.raises(ValueError, match="rpath"):
        util.GufiAggregateQuery.model_validate(_query())
    monkeypatch.setattr(util, "CONFIG", AblationConfig(path="rpath"))
    query = util.query_model(AblationConfig(path="rpath")).model_validate(_query())
    assert "rpath(sname, sroll, name)" in query.build_query_string()
    with pytest.raises(ValueError, match="only works in per_directory_sql"):
        util.GufiAggregateQuery.model_validate(_query(final_sql="SELECT rpath(p, 0), size FROM partial"))
    with pytest.raises(ValueError, match="no path columns"):
        util.GufiAggregateQuery.model_validate(_query(per_directory_sql=["SELECT path, size FROM vrpentries"]))


def test_query_model_keeps_validation_and_changes_descriptions() -> None:
    cfg = AblationConfig(schema="none", path="rpath")
    model = util.query_model(cfg)
    assert model is not util.GufiAggregateQuery and issubclass(model, util.GufiAggregateQuery)
    assert "gufi_schemas" not in json.dumps(model.model_json_schema())
    assert util.query_model(FULL) is util.GufiAggregateQuery
    with pytest.raises(ValueError, match="ORDER BY"):
        model.model_validate(_query(per_directory_sql=["SELECT name, size FROM vrpentries ORDER BY size"],
                                    final_sql="SELECT p, size FROM partial"))


# Plain submission

@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["id"])
def test_plain_round_trips_every_ground_truth(scenario: dict) -> None:
    for query in [scenario["ground_truth"], *scenario.get("alternates", [])]:
        query = {"index": "eval_master", **query}
        for multiline in (False, True):
            assert gufi_plain.parse_plain_command(gufi_plain.to_plain_command(query, multiline)) == query


def test_plain_accepts_user_table_names_and_paths() -> None:
    payload = gufi_plain.parse_plain_command(
        "gufi_query -n 8 -I 'CREATE TABLE inter(gid INT64, size INT64)' "
        "-E \"INSERT INTO inter SELECT gid, size FROM vrpentries WHERE type = 'l'\" "
        "-K 'create table agg(gid INT64, n INT64)' -J 'INSERT INTO agg SELECT gid, COUNT(*) FROM inter GROUP BY gid;' "
        "-G 'SELECT gid, SUM(n) FROM agg GROUP BY gid' /some/root/eval_master/")
    assert payload["index"] == "eval_master"
    assert payload["partial_sql"] == "SELECT gid, COUNT(*) FROM per_directory AS inter GROUP BY gid"
    assert payload["final_sql"] == "SELECT gid, SUM(n) FROM partial AS agg GROUP BY gid"
    util.GufiAggregateQuery.model_validate(payload)


@pytest.mark.parametrize("command, match", [
    ("", "empty"),
    ("gufi_query -x foo idx", "unsupported flag"),
    ("gufi_query -I 'CREATE TABLE t(a INT64)' -K 'CREATE TABLE a(b INT64)' -J 'INSERT INTO a SELECT a FROM t' "
     "-G 'SELECT b FROM a' idx", "at least one of -T, -S, -E"),
    ("gufi_query -I 'CREATE TABLE t(a INT64)' -E 'INSERT INTO other SELECT size FROM vrpentries' "
     "-K 'CREATE TABLE a(b INT64)' -J 'INSERT INTO a SELECT a FROM t' -G 'SELECT b FROM a' idx", "inserts into other"),
    ("gufi_query -I 'CREATE TABLE t(a INT64)' -E 'INSERT INTO t SELECT size FROM vrpentries' "
     "-K 'CREATE TABLE a(b INT64)' -J 'INSERT INTO a SELECT a FROM t' -G 'SELECT b FROM a'", "exactly one index"),
])
def test_plain_errors_explain_the_form(command: str, match: str) -> None:
    with pytest.raises(gufi_plain.PlainCommandError, match=match):
        gufi_plain.parse_plain_command(command)


def test_rpath_rewrite_for_replay() -> None:
    query = {"index": "eval_master", "per_directory_sql": [
        "SELECT path, size FROM vrpentries WHERE type = 'f' AND path LIKE 'eval_master/a/%'"]}
    sql = gufi_plain.to_rpath_query(query)["per_directory_sql"][0]
    assert sql == ("SELECT rpath(sname, sroll, name), size FROM vrpentries WHERE type = 'f' "
                   "AND rpath(sname, sroll, name) LIKE '%/eval_master/a/%'")


# Grading

def test_index_root_is_stripped_from_paths() -> None:
    from eval_lib.comparator import compare_rows

    expected = [["eval_master/a/f", 3]]
    assert compare_rows(expected, [["/idx/root/eval_master/a/f", 3]], False, "/idx/root/").passed
    assert not compare_rows(expected, [["/idx/root/eval_master/a/f", 3]], False).passed


def test_alternates_pass_and_are_recorded() -> None:
    from eval_lib.agent import AgentResult, AggregateCall
    from eval_lib.metrics import score_single

    def result(rows: list[list[int]]) -> AgentResult:
        call = AggregateCall(t_ms=0, query={"index": "eval_master"}, ok=True, duration_ms=1, rows=rows)
        return AgentResult("", [call], [], "", "", 1, "final")

    final = "SELECT SUM(n) FROM partial"
    alternates = [([[10289, 2]], final)]
    exact = score_single(result([[9989, 1]]), "eval_master", [[9989, 1]], final, 1, 3, alternates)
    alt = score_single(result([[10289, 2]]), "eval_master", [[9989, 1]], final, 1, 3, alternates)
    wrong = score_single(result([[5, 5]]), "eval_master", [[9989, 1]], final, 1, 3, alternates)
    assert (exact.passed, exact.matched_ground_truth) == (True, 0)
    assert (alt.passed, alt.matched_ground_truth) == (True, 1)
    assert (wrong.passed, wrong.matched_ground_truth) == (False, None)


def test_plain_call_reports_its_index() -> None:
    from eval_lib.agent import AggregateCall

    command = gufi_plain.to_plain_command({"index": "eval_master", **SCENARIOS[0]["ground_truth"]})
    assert AggregateCall(t_ms=0, query=command, ok=True, duration_ms=1).index == "eval_master"


# Checkpoints and restart

def _summary_line(sid: str, trial: int) -> dict:
    from eval_lib.reporter import SUMMARY_KEYS

    return {**{k: None for k in SUMMARY_KEYS}, "scenario_id": sid, "trial": trial, "mode": "single", "pass": True,
            "failure_tags": [], "redundancies": []}


def test_summary_survives_a_torn_last_line(tmp_path: Path) -> None:
    from eval_lib.reporter import RunWriter

    writer = RunWriter(tmp_path, "run")
    for t in (1, 2, 3):
        writer.append_summary(_summary_line("s", t))
    with writer.summary_path.open("a") as f:
        f.write('{"scenario_id": "s", "tri')  # the process died mid-write
    reopened = RunWriter.reopen(tmp_path / "run")
    assert [line["trial"] for line in reopened.load_summary()] == [1, 2, 3]
    reopened.append_summary(_summary_line("s", 4))
    assert [line["trial"] for line in reopened.load_summary()] == [1, 2, 3, 4]


def test_json_artifacts_are_written_atomically(tmp_path: Path) -> None:
    from eval_lib.reporter import RunWriter

    writer = RunWriter(tmp_path, "run")
    writer.write_json("manifest.json", {"a": 1})
    writer.write_json("manifest.json", {"a": 2})
    assert json.loads((writer.root / "manifest.json").read_text()) == {"a": 2}
    assert not list(writer.root.glob(".*.tmp"))


def test_api_failures_are_split_into_infrastructure_and_agent_errors() -> None:
    import httpx2 as httpx  # the HTTP client this openai release uses
    import openai
    from eval_lib.driver import is_infrastructure_error

    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")

    def status(code: int) -> openai.APIStatusError:
        return openai.APIStatusError("x", response=httpx.Response(code, request=request), body=None)

    assert is_infrastructure_error(openai.APIConnectionError(request=request))
    assert is_infrastructure_error(openai.APITimeoutError(request=request))
    for code in (401, 402, 403, 404, 408, 429, 500, 502, 503):
        assert is_infrastructure_error(status(code)), code
    assert not is_infrastructure_error(status(400))  # e.g. the agent overflowed the context window
    assert is_infrastructure_error(RuntimeError("model returned no choices: ..."))
    assert not is_infrastructure_error(ValueError("anything else"))


def test_closed_connections_count_as_transport_failures() -> None:
    from mcp.shared.exceptions import MCPError
    from mcp_types import CONNECTION_CLOSED, INVALID_PARAMS, REQUEST_TIMEOUT

    from eval_lib.mcp_session import is_transport_failure

    assert is_transport_failure(MCPError(CONNECTION_CLOSED, "Connection closed"))
    assert is_transport_failure(MCPError(REQUEST_TIMEOUT, "timed out"))
    assert is_transport_failure(BrokenPipeError())
    assert not is_transport_failure(MCPError(INVALID_PARAMS, "bad arguments"))


def test_sweep_finds_finished_and_resumable_runs(tmp_path: Path) -> None:
    import run_ablation as ra

    cfg = AblationConfig(primer=False)
    expect = {"config": {"mode": "single", "trials": 2}, "registry_sha": "abc", "trials_total": 2}

    def make(name: str, lines: int, manifest: bool, factors: dict = cfg.as_dict()) -> Path:
        run = tmp_path / name
        run.mkdir()
        (run / "config.json").write_text(json.dumps({"ablation": {"factors": factors}, "mode": "single", "trials": 2,
                                                     "registry_sha256": {"single": "abc"}}))
        (run / "summary.jsonl").write_text("".join("{}\n" for _ in range(lines)))
        if manifest:
            (run / "manifest.json").write_text(json.dumps({"results": {"infrastructure_errors": [],
                                                                       "expected_trials": 2}}))
        return run

    assert ra.run_state(tmp_path, cfg, expect) == ("none", None)
    partial = make("ablation_no_primer_1", 1, manifest=False)
    assert ra.run_state(tmp_path, cfg, expect) == ("resumable", partial)
    make("ablation_other_2", 2, manifest=True, factors=FULL.as_dict())  # another config's run is ignored
    assert ra.run_state(tmp_path, cfg, expect) == ("resumable", partial)
    done = make("ablation_no_primer_3", 2, manifest=True)
    assert ra.run_state(tmp_path, cfg, expect) == ("finished", done)
    assert ra.run_state(tmp_path, cfg, {**expect, "registry_sha": "changed"}) == ("none", None)


def test_sweep_lock_refuses_a_second_sweep_and_clears_a_stale_one(tmp_path: Path) -> None:
    import run_ablation as ra

    with ra.sweep_lock(tmp_path):
        with pytest.raises(SystemExit, match="another sweep"):
            with ra.sweep_lock(tmp_path):
                pass
    assert not (tmp_path / ".ablation_sweep.lock").exists()
    (tmp_path / ".ablation_sweep.lock").write_text("999999999")  # a pid that cannot exist
    with ra.sweep_lock(tmp_path):
        pass
