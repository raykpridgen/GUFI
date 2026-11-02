"""Tests for gufi_eval.py and eval_lib. Offline except the replay end-to-end test, which needs eval_master."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import gufi_eval
from eval_lib import comparator, driver, metrics, registry, reporter
from eval_lib.agent import AgentResult, AggregateCall
from eval_lib.mcp_session import TraceEvent

HERE = Path(__file__).resolve().parent
FIXED_NOW = 1791763200  # 2026-10-12T00:00:00Z


def _manifest(**overrides: object) -> registry.IndexManifest:
    data = {"manifest_id": "abc", "index_name": "eval_master", "seed": 1, "fixed_now": FIXED_NOW, **overrides}
    return registry.IndexManifest.model_validate(data)


def _scenario(**overrides: object) -> dict:
    base = {
        "id": "s1", "prompt": "Count files in {index}", "tags": [], "enabled": True, "index": "eval_master",
        "parameters": {}, "ground_truth": {"per_directory_sql": ["SELECT size FROM vrpentries"]}, "notes": "",
    }
    base.update(overrides)
    return base


def _write_registry(tmp_path: Path, items: list[dict]) -> Path:
    path = tmp_path / "reg.yaml"
    path.write_text(json.dumps(items))
    return path


# Registry

def test_shipped_registries_load() -> None:
    single = registry.load_registry(HERE / "scenarios.single.yaml", "single")
    general = registry.load_registry(HERE / "scenarios.general.yaml", "general")
    assert len(single) >= 12 and all(s.ground_truth for s in single)
    assert len(general) >= 6 and all(s.ground_truth is None for s in general)
    assert set(single[0].model_dump()) == set(general[0].model_dump())


def test_registry_mode_rules(tmp_path: Path) -> None:
    with pytest.raises(registry.RegistryError, match="need a ground_truth"):
        registry.load_registry(_write_registry(tmp_path, [_scenario(ground_truth=None)]), "single")
    with pytest.raises(registry.RegistryError, match="must have ground_truth: null"):
        registry.load_registry(_write_registry(tmp_path, [_scenario()]), "general")


@pytest.mark.parametrize("item, message", [
    (_scenario(extra=1), "Extra inputs"),
    ({k: v for k, v in _scenario().items() if k != "notes"}, "notes"),
    (_scenario(id="bad id"), "id"),
    (_scenario(ground_truth={"index": "x", "per_directory_sql": []}), "must not set index"),
])
def test_registry_rejects_bad_entries(tmp_path: Path, item: dict, message: str) -> None:
    with pytest.raises(registry.RegistryError, match=message):
        registry.load_registry(_write_registry(tmp_path, [item]), "single")


def test_registry_rejects_duplicate_ids(tmp_path: Path) -> None:
    with pytest.raises(registry.RegistryError, match="duplicate"):
        registry.load_registry(_write_registry(tmp_path, [_scenario(), _scenario()]), "single")


def test_render_fills_placeholders() -> None:
    scenario = registry.Scenario.model_validate(_scenario(
        prompt="Today is {fixed_now_date}; last {parameters.days} days of {index}",
        parameters={"days": 60},
        ground_truth={"per_directory_sql": ["SELECT mtime FROM vrpentries WHERE mtime < {fixed_now} - {parameters.days} * 86400"],
                      "final_sql": "SELECT SUM(n) FROM partial"},
    ))
    item = registry.render_scenario(scenario, "single", _manifest())
    assert item.prompt == "Today is 2026-10-12; last 60 days of eval_master"
    assert item.ground_truth["index"] == "eval_master"
    assert item.ground_truth["per_directory_sql"] == [f"SELECT mtime FROM vrpentries WHERE mtime < {FIXED_NOW} - 60 * 86400"]


def test_render_reports_missing_parameter() -> None:
    scenario = registry.Scenario.model_validate(_scenario(prompt="{parameters.k} files"))
    with pytest.raises(registry.RegistryError, match="placeholder"):
        registry.render_scenario(scenario, "single", _manifest())


# Comparator

def test_compare_unordered_ignores_row_order_and_types() -> None:
    result = comparator.compare_rows([["a", 1], ["b", 2.0]], [["b", "2"], ["a", 1]], ordered=False)
    assert result.passed and not result.column_permuted


def test_compare_ordered_requires_order() -> None:
    assert not comparator.compare_rows([[1], [2]], [[2], [1]], ordered=True).passed
    assert comparator.compare_rows([[1], [2]], [[1], [2]], ordered=True).passed


def test_compare_accepts_column_permutation() -> None:
    result = comparator.compare_rows([["/a", 10], ["/b", 5]], [[10, "/a"], [5, "/b"]], ordered=True)
    assert result.passed and result.column_permuted and result.column_permutation == [1, 0]


@pytest.mark.parametrize("expected, actual, reason", [
    ([[1]], [[1], [2]], "row count"),
    ([[1, 2]], [[1]], "column counts"),
    ([[1.0000001]], [[1.1]], "values differ"),
])
def test_compare_mismatches(expected: list, actual: list, reason: str) -> None:
    result = comparator.compare_rows(expected, actual, ordered=False)
    assert not result.passed and reason in result.reason


def test_compare_reports_order_only_difference() -> None:
    result = comparator.compare_rows([[1], [2]], [[2], [1]], ordered=True)
    assert result.reason == "same rows, different order" and not result.missing and not result.extra


def test_compare_returns_sorted_sides_and_diff() -> None:
    result = comparator.compare_rows([["csv", 50], ["txt", 9]], [[10, "csv"], [9, "txt"]], ordered=False)
    assert not result.passed
    assert result.expected == [("csv", 50), ("txt", 9)]
    assert result.actual == [("csv", 10), ("txt", 9)]
    assert result.missing == [("csv", 50)] and result.extra == [("csv", 10)]
    assert "1 expected row(s) missing" in result.reason


def test_float_rounding_and_none() -> None:
    assert comparator.compare_rows([[0.1 + 0.2, None]], [[0.3, None]], ordered=False).passed


def test_is_ordered_top_level_only() -> None:
    assert comparator.is_ordered("SELECT a FROM partial ORDER BY a DESC LIMIT 10")
    assert not comparator.is_ordered(
        "SELECT u FROM (SELECT u, ROW_NUMBER() OVER (PARTITION BY u ORDER BY s) AS rn FROM partial) WHERE rn <= 10"
    )


# Metrics

def _event(kind: str, name: str, ok: bool = True) -> TraceEvent:
    return TraceEvent(t_ms=0, kind=kind, name=name, args_summary="", ok=ok, duration_ms=1)


def _result(events: list[TraceEvent], calls: list[AggregateCall], stop: str = "final") -> AgentResult:
    return AgentResult(final_text="done", aggregate_calls=calls, events=events, started_at="", finished_at="",
                       duration_ms=100, stop_reason=stop)


def _call(rows: list, ok: bool = True, index: str = "eval_master", duration: float = 30) -> AggregateCall:
    return AggregateCall(t_ms=0, query={"index": index}, ok=ok, duration_ms=duration, rows=rows, row_count=len(rows))


def test_single_passes_on_a_later_attempt() -> None:
    events = [_event("resource", "gufi://schemas/all"),
              _event("resource", "gufi://schemas/all"), _event("tool", "aggregate_sql_query"),
              _event("resource", "gufi://indexes"), _event("tool", "aggregate_sql_query", ok=False),
              _event("tool", "aggregate_sql_query")]
    calls = [_call([[1]]), _call([], ok=False), _call([[42]], duration=50)]
    score = metrics.score_single(_result(events, calls), "eval_master", [[42]], "SELECT x FROM partial", 20)
    assert score.passed and score.passed_on_attempt == 3 and not score.one_shot_pass
    assert [a.passed for a in score.attempts] == [False, False, True]
    assert score.resources_consumed == 2
    assert score.aggregate_revision_count == 2
    assert score.timing_delta_ms == 30
    assert score.failure_tags == ["multiple_aggregate"]


def test_single_later_successful_call_replaces_a_match() -> None:
    calls = [_call([[42]], duration=40), _call([[1]])]
    score = metrics.score_single(_result([], calls), "eval_master", [[42]], "SELECT x FROM partial", 0)
    assert not score.passed and score.passed_on_attempt is None and not score.one_shot_pass
    assert score.scored is calls[1]
    assert score.failure_tags == ["wrong_result", "replaced_match", "multiple_aggregate"]


def test_single_failed_call_does_not_replace_a_match() -> None:
    calls = [_call([[42]]), _call([], ok=False)]
    score = metrics.score_single(_result([], calls), "eval_master", [[42]], "SELECT x FROM partial", 0)
    assert score.passed and score.passed_on_attempt == 1
    assert not score.one_shot_pass


def test_single_one_shot_is_one_matching_call() -> None:
    score = metrics.score_single(_result([], [_call([[42]])]), "eval_master", [[42]], "SELECT x FROM partial", 0)
    assert score.passed and score.passed_on_attempt == 1 and score.one_shot_pass


def test_single_only_grades_up_to_max_attempts() -> None:
    calls = [_call([[1]]), _call([[2]]), _call([[42]])]
    score = metrics.score_single(_result([], calls, "max_attempts"), "eval_master", [[42]],
                                 "SELECT x FROM partial", 0, max_attempts=2)
    assert not score.passed and score.passed_on_attempt is None
    assert len(score.attempts) == 2 and score.scored is calls[1]
    assert score.failure_tags == ["wrong_result", "max_attempts", "multiple_aggregate"]
    assert "attempt 2 of 2" in score.reason


def test_single_wrong_index_attempt_can_be_corrected() -> None:
    calls = [_call([[42]], index="vault"), _call([[42]])]
    score = metrics.score_single(_result([], calls), "eval_master", [[42]], "SELECT x FROM partial", 0)
    assert score.passed and score.passed_on_attempt == 2
    assert "wrong_index" not in score.failure_tags


@pytest.mark.parametrize("calls, stop, tags", [
    ([], "final", ["no_gradable_output"]),
    ([_call([], ok=False)], "final", ["tool_error"]),
    ([_call([[1]])], "final", ["wrong_result"]),
    ([_call([[42]], index="vault")], "final", ["wrong_index"]),
    ([], "timeout", ["timeout", "no_gradable_output"]),
    ([_call([[42]])], "max_tool_calls", ["max_tool_calls"]),
])
def test_single_failure_tags(calls: list, stop: str, tags: list) -> None:
    score = metrics.score_single(_result([], calls, stop), "eval_master", [[42]], "SELECT x FROM partial", 0)
    assert score.failure_tags == tags
    assert score.passed == (tags == ["max_tool_calls"])


def test_truncated_result_fails() -> None:
    call = _call([[42]])
    call.truncated = True
    score = metrics.score_single(_result([], [call]), "eval_master", [[42]], "SELECT x FROM partial", 0)
    assert not score.passed and "truncated" in score.reason


def test_general_metrics() -> None:
    events = [_event("resource", "gufi://indexes"), _event("resource", "gufi://indexes"),
              _event("tool", "gufi_find"), _event("tool", "gufi_find"), _event("tool", "gufi_find"),
              _event("tool", "gufi_du")]
    m = metrics.general_metrics(_result(events, []))
    assert m.executions == [2, 4]
    assert m.redundancies == [["gufi://indexes", 1], ["gufi_find", 2]]
    assert m.execution_time_ms == 100


# Driver helpers

def test_inline_refs() -> None:
    schema = {
        "type": "object",
        "properties": {"args": {"$ref": "#/$defs/Args", "description": "outer"}},
        "$defs": {"Args": {"type": "object", "properties": {"inner": {"$ref": "#/$defs/Inner"}}},
                  "Inner": {"type": "string"}},
    }
    out = driver.inline_refs(schema)
    assert "$defs" not in json.dumps(out) and "$ref" not in json.dumps(out)
    assert out["properties"]["args"]["description"] == "outer"
    assert out["properties"]["args"]["properties"]["inner"] == {"type": "string"}


def test_inline_refs_rejects_recursion() -> None:
    with pytest.raises(ValueError, match="recursive"):
        driver.inline_refs({"$ref": "#/$defs/A", "$defs": {"A": {"properties": {"a": {"$ref": "#/$defs/A"}}}}})


def test_openai_tools_adds_read_resource() -> None:
    tool = SimpleNamespace(name="aggregate_sql_query", description="agg", input_schema={"type": "object"})
    tools = driver.openai_tools([tool], ["gufi://indexes"], ["gufi://schemas/{schema}"])
    names = [t["function"]["name"] for t in tools]
    assert names == ["aggregate_sql_query", "read_resource"]
    assert "gufi://schemas/{schema}" in tools[1]["function"]["description"]


def test_single_system_prompt_mentions_grading() -> None:
    from eval_lib.agent import EvalConstraints, TrialContext
    context = TrialContext(server=None, index="eval_master", fixed_now=FIXED_NOW, fixed_now_iso="2026-10-12T00:00:00+00:00")
    text = driver.system_prompt(context, EvalConstraints(mode="single", allowed_tools=["aggregate_sql_query"],
                                                         max_aggregate_calls=3))
    assert "up to 3 times" in text and "call it again" in text and "2026-10-12" in text


def test_add_usage_reads_openrouter_cost() -> None:
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.0, "requests": 0}
    response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15, cost=0.0002))
    driver._add_usage(usage, response)
    driver._add_usage(usage, response)
    assert usage == {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30, "cost": 0.0004, "requests": 2}


# Reporter

def test_summary_lines_have_identical_keys(tmp_path: Path) -> None:
    writer = reporter.RunWriter(tmp_path, "run")
    result = _result([_event("tool", "aggregate_sql_query")], [_call([[1]])])
    score = metrics.score_single(result, "eval_master", [[1]], "SELECT x FROM partial", 1)
    single = reporter.record_single(writer, "a", 1, result, score, {"index": "eval_master"}, [[1]], 1, {"agent": "replay"})
    general = reporter.record_general(writer, "b", 1, result, metrics.general_metrics(result), {"agent": "replay"})
    assert list(single) == list(general) == list(reporter.SUMMARY_KEYS)
    assert general["pass"] is None and general["similarity_path"] == "" and general["failure_tags"] == []
    assert general["passed_on_attempt"] is None and general["one_shot_pass"] is None
    assert single["passed_on_attempt"] == 1 and single["one_shot_pass"] is True
    assert single["executions"] == [0, 1] and single["final_response_path"] == ""
    lines = [json.loads(line) for line in (tmp_path / "run" / "summary.jsonl").read_text().splitlines()]
    assert [list(line) for line in lines] == [list(reporter.SUMMARY_KEYS)] * 2
    for rel in (single["similarity_path"], single["trace_path"], general["final_response_path"]):
        assert (tmp_path / "run" / rel).is_file()


def test_summarize_reports_one_shot_and_cumulative_rates() -> None:
    def line(sid: str, attempt: object, tool_calls: int) -> dict:
        return {"mode": "single", "scenario_id": sid, "pass": attempt is not None, "passed_on_attempt": attempt,
                "one_shot_pass": attempt == 1, "executions": [0, tool_calls], "usage": {}}

    results = gufi_eval.summarize([line("a", 1, 1), line("a", 2, 2), line("b", None, 3), line("b", 3, 3)], 3)
    assert results["single_pass_rate"] == 0.75
    assert results["single_one_shot_pass_rate"] == 0.25
    assert results["single_pass_rate_by_attempt"] == {1: 0.25, 2: 0.5, 3: 0.75}
    assert results["single_mean_tool_calls"] == 2.25
    assert results["single_one_shot_rate_by_scenario"] == {"a": 0.5, "b": 0.0}


def test_summary_rejects_wrong_keys(tmp_path: Path) -> None:
    writer = reporter.RunWriter(tmp_path, "run")
    with pytest.raises(ValueError, match="keys"):
        writer.append_summary({"scenario_id": "x"})


# Leakage and guidance

def _rendered_single() -> list[registry.RenderedScenario]:
    return [registry.render_scenario(s, "single", _manifest())
            for s in registry.load_registry(HERE / "scenarios.single.yaml", "single")]


def _agent_visible_text() -> str:
    """Everything the server shows an agent before it runs a query: instructions, tool listings, resource listings."""
    import asyncio

    import gufi_mcp_server

    async def gather() -> list:
        return [await gufi_mcp_server.mcp.list_tools(), await gufi_mcp_server.mcp.list_resources(),
                await gufi_mcp_server.mcp.list_resource_templates()]

    tools, resources, templates = asyncio.run(gather())
    parts = [gufi_mcp_server.PRIMER]
    parts += [json.dumps(t.model_dump(mode="json")) for t in tools]
    parts += [json.dumps(r.model_dump(mode="json")) for r in [*resources, *templates]]
    return " ".join(" ".join(parts).lower().split())


def test_no_ground_truth_statement_is_shown_to_agents() -> None:
    visible = _agent_visible_text()
    for item in _rendered_single():
        for sql in [*item.ground_truth["per_directory_sql"], item.ground_truth["final_sql"]]:
            assert " ".join(sql.lower().split()) not in visible, f"{item.scenario.id} leaks: {sql}"


def test_primer_covers_core_guidance() -> None:
    import gufi_mcp_server
    import gufi_mcp_util as util

    os.environ.setdefault("SCHEMAFILE", str(HERE / "schema_hints.json"))
    util.get_static_schema.cache_clear()
    util.path_column_hints.cache_clear()

    primer = gufi_mcp_server.PRIMER
    assert gufi_mcp_server.mcp.instructions == primer
    for needle in ("gufi://indexes", "gufi://schemas/all", "vrpentries",
                   "per_directory_sql", "partial_sql", "FROM partial", "rollup", "ORDER BY", "treesummary",
                   "SUM counts"):
        assert needle in primer, needle

    schema = util.get_static_schema()
    vrp = schema["vrpentries"]["columns"]
    assert "86400" in vrp["mtime"]["hint"]
    assert "1024" in vrp["mode"]["hint"]
    assert not {"path", "dirpath", "spath"} & set(vrp)
    paths = util.path_columns("vrpentries")
    assert "myindex/dir/file.txt" in paths["path"] and "LIKE" in paths["dirpath"]


PRIMER_BUDGET_CHARS = 4000


def test_primer_points_to_functions_within_budget() -> None:
    import gufi_mcp_server

    assert "gufi://functions" in gufi_mcp_server.PRIMER
    assert len(gufi_mcp_server.PRIMER) < PRIMER_BUDGET_CHARS
    for name in ("bytecat", "agecat", "uidtouser"):
        assert name not in gufi_mcp_server.PRIMER, name


def _fresh_catalog() -> None:
    import gufi_mcp_util as util

    os.environ.setdefault("SCHEMAFILE", str(HERE / "schema_hints.json"))
    for cached in (util.get_static_schema, util.path_column_hints, util.function_catalog, util._column_tags):
        cached.cache_clear()


def test_column_tags_name_the_matching_functions() -> None:
    import gufi_mcp_util as util

    _fresh_catalog()
    assert any(tag.startswith("bytecat(size)") for tag in util.functions_for_column("size"))
    assert any(tag.startswith("agecat(mtime)") for tag in util.functions_for_column("mtime"))
    assert any(tag.startswith("ext(name)") for tag in util.functions_for_column("name"))
    assert any(tag.startswith("uidtouser(duid)") for tag in util.functions_for_column("duid"))
    assert len([t for t in util.functions_for_column("size") if "stdevs" in t]) == 1
    assert util.functions_for_column("inode") == []
    for hidden in ("blocksize", "hostname", "rpad"):
        assert not any(hidden in tag for column in util._column_tags() for tag in util.functions_for_column(column))


def test_describe_schema_object_appends_function_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    import gufi_mcp_util as util

    _fresh_catalog()
    monkeypatch.setattr(util, "list_schema_objects", lambda: {"vrpentries": "view"})
    rows = [["name", "TEXT"], ["size", "INT64"], ["mtime", "INT64"], ["inode", "TEXT"], ["sname", "TEXT"]]
    monkeypatch.setattr(util, "table_columns", lambda name: rows)
    described = util.describe_schema_object("vrpentries")
    assert "Functions: bytecat(size)" in described["size"]["hint"]
    assert "agecat(mtime)" in described["mtime"]["hint"]
    assert "ext(name)" in described["name"]["hint"] and "basename(path)" in described["path"]["hint"]
    assert "Functions:" not in described["inode"]["hint"]
    assert "sname" not in described


def test_function_catalog_matches_schema_and_hides_shell_functions() -> None:
    import gufi_mcp_util as util

    _fresh_catalog()
    schema = util.get_static_schema()
    known = {column for table in schema.values() for column in table["columns"]} | {"path", "dirpath", "spath"}
    for name, entry in util.function_catalog().items():
        assert entry["signature"].startswith(f"{name}("), name
        assert set(entry.get("columns", [])) <= known, name
    listed = util.list_functions()
    assert {"bytecat", "agecat", "ext", "uidtouser", "stdev_from_parts"} <= set(listed)
    assert not {"blocksize", "hostname", "rpad"} & set(listed)
    assert not util.BLOCKED_FUNCTIONS & set(util.function_catalog())
    assert "Zero" in util.describe_function("bytecat")["detail"]
    with pytest.raises(util.GufiError, match="shell commands"):
        util.describe_function("strop")
    with pytest.raises(util.GufiError, match="Unknown function"):
        util.describe_function("nosuch")
    for hint in (c["hint"] for table in schema.values() for c in table["columns"].values()):
        assert "bytecat" not in hint and "agecat" not in hint


def test_function_catalog_leaks_no_ground_truth() -> None:
    _fresh_catalog()
    catalog = json.loads((HERE / "schema_hints.json").read_text())["functions"]
    text = " ".join(json.dumps(catalog).lower().split())
    for item in _rendered_single():
        for sql in [*item.ground_truth["per_directory_sql"], item.ground_truth["final_sql"]]:
            assert " ".join(sql.lower().split()) not in text, f"{item.scenario.id} leaks: {sql}"


def test_agents_never_see_rollup_path_internals() -> None:
    visible = _agent_visible_text()
    for needle in ("sname", "sroll", "rpath("):
        assert needle not in visible, needle


def test_session_brief_resource_is_gone() -> None:
    assert "session-brief" not in _agent_visible_text()


def test_general_mirrors_every_single_prompt() -> None:
    single = {s.id: s for s in registry.load_registry(HERE / "scenarios.single.yaml", "single")}
    general = {s.id: s for s in registry.load_registry(HERE / "scenarios.general.yaml", "general")}
    for sid, scenario in single.items():
        assert sid in general, f"{sid} is not mirrored in scenarios.general.yaml"
        mirror = general[sid]
        assert (mirror.prompt, mirror.parameters, mirror.index) == (scenario.prompt, scenario.parameters, scenario.index)
        assert "mirrored" in mirror.tags


PROMPT_GIVEAWAYS = ("bytecat", "agecat", "ext(", "rpath", "type '", "86400", "{fixed_now", "unix time", "mtime")


def _all_scenarios() -> list:
    return [*registry.load_registry(HERE / "scenarios.single.yaml", "single"),
            *registry.load_registry(HERE / "scenarios.general.yaml", "general")]


@pytest.mark.parametrize("scenario", _all_scenarios(), ids=lambda s: s.id)
def test_prompts_use_natural_wording(scenario) -> None:
    prompt = scenario.prompt.lower()
    assert not [g for g in PROMPT_GIVEAWAYS if g in prompt], scenario.prompt
    assert "{index}" in scenario.prompt


def test_general_prompts_are_unique() -> None:
    general = registry.load_registry(HERE / "scenarios.general.yaml", "general")
    normalized = [" ".join(s.prompt.lower().split()) for s in general]
    assert len(normalized) == len(set(normalized))


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(json.loads(json.dumps(kwargs)))
        message = SimpleNamespace(content="final answer", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


class _FakeServer:
    instructions = "PRIMER TEXT"

    async def list_tools(self) -> list:
        return [SimpleNamespace(name="aggregate_sql_query", description="agg", input_schema={"type": "object"})]

    async def list_resources(self) -> tuple[list, list]:
        return ["gufi://indexes"], ["gufi://schemas/{schema}"]


def test_driver_sends_only_prompt_and_primer() -> None:
    import asyncio

    from eval_lib.agent import EvalConstraints, TrialContext

    scenario = registry.Scenario.model_validate(_scenario(notes="SECRET NOTE: use isroot = 1", tags=["SECRET_TAG"]))
    item = registry.render_scenario(scenario, "single", _manifest())
    completions = _FakeCompletions()
    agent = object.__new__(driver.OpenRouterDriver)
    agent.model, agent.temperature, agent.max_completion_tokens = "m", None, None
    agent._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    context = TrialContext(server=_FakeServer(), index="eval_master", fixed_now=FIXED_NOW,
                           fixed_now_iso="2026-10-12T00:00:00+00:00", ground_truth=item.ground_truth)
    result = asyncio.run(agent.run(item.prompt, context, EvalConstraints("single", ["aggregate_sql_query"])))

    messages = completions.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[1]["content"] == item.prompt
    assert "PRIMER TEXT" in messages[0]["content"]
    sent = json.dumps(completions.calls[0])
    for secret in ("SECRET NOTE", "SECRET_TAG", "SELECT size FROM vrpentries"):
        assert secret not in sent
    assert result.final_text == "final answer"


# End to end (real MCP server, no network)

_MANIFEST = Path(os.getenv("GUFI_INDEX_ROOT", "/nonexistent")) / "eval_master.manifest.json"


@pytest.mark.skipif(not _MANIFEST.is_file(), reason="needs the eval_master index (gen_index.py)")
def test_replay_run_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = gufi_eval.main(["run", "--agent", "replay", "--mode", "both", "--output", str(tmp_path),
                           "--scenario", "total_file_size", "--scenario", "top10_size_per_uid",
                           "--scenario", "largest_directories"])
    assert code == 0
    (run,) = tmp_path.iterdir()
    lines = [json.loads(line) for line in (run / "summary.jsonl").read_text().splitlines()]
    assert sorted((line["mode"], line["scenario_id"], line["pass"]) for line in lines) == [
        ("general", "largest_directories", None), ("general", "top10_size_per_uid", None),
        ("general", "total_file_size", None),
        ("single", "top10_size_per_uid", True), ("single", "total_file_size", True)]
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["results"]["single_pass_rate"] == 1.0
    assert manifest["index_manifests"]["eval_master"]
    assert "OPENROUTER" not in (run / "config.json").read_text()


def test_unknown_scenario_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    if not _MANIFEST.is_file():
        pytest.skip("needs the eval_master index")
    assert gufi_eval.main(["check", "--scenario", "nope"]) == 2
    assert "unknown scenario" in capsys.readouterr().err
