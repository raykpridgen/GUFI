"""
Module 9: eval harness comparing raw gufi_vt SQL vs structured QueryPlan pipeline.

Scores wall time, pipeline step count, and answer correctness on fixture indexes.
"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from gufi_mcp.event_logger import EventLogger, log_pipeline_step
from gufi_mcp.gufi_vt_executor import GufiVtExecutor, reset_executor
from gufi_mcp.pipeline_service import (
    estimate_query_cost,
    execute_query_plan,
    explain_plan,
    validate_plan,
)
from gufi_mcp.query_plan import QueryPlanPipeline
from gufi_util import SchemaRegistry, resolve_index_path


@dataclass
class EvalCase:
    case_id: str
    index: str
    question: str
    expected_route: str
    raw_sql: list[str]
    build_plan: Callable[[], dict[str, Any]]
    check_answer: Callable[[Any], bool]
    description: str = ""


@dataclass
class ArmResult:
    success: bool
    elapsed_ms: float
    answer: Any
    error: str | None = None
    step_count: int = 1


@dataclass
class CaseComparison:
    case_id: str
    question: str
    expected_route: str
    raw: ArmResult
    structured: ArmResult
    structured_tool_steps: int
    structured_gap_ms_total: float
    correctness_raw: bool
    correctness_structured: bool
    raw_faster: bool


@dataclass
class EvalReport:
    index: str
    cases: list[CaseComparison] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _notes_total_size_plan() -> dict[str, Any]:
    return QueryPlanPipeline.total_file_size("notes").to_dict()


def _notes_top_files_plan(limit: int = 3) -> dict[str, Any]:
    return (
        QueryPlanPipeline.skeleton("notes", f"top {limit} files by size")
        .set_stage(
            "entries_sql",
            "SELECT name, size FROM vrpentries WHERE type = 'f' ORDER BY size DESC",
        )
        .update(output={"row_limit": limit})
        .to_dict()
    )


def default_eval_cases() -> list[EvalCase]:
    return [
        EvalCase(
            case_id="notes_total_file_bytes",
            index="notes",
            question="What is the total size of regular files in the notes index?",
            expected_route="pipeline",
            raw_sql=[
                (
                    "CREATE VIRTUAL TABLE gufi USING gufi_vt("
                    "index='{index_path}', threads=4, "
                    'I="CREATE TABLE intermediate(size INT64);", '
                    "E=\"INSERT INTO intermediate SELECT size FROM vrpentries WHERE type = 'f';\", "
                    'K="CREATE TABLE aggregate(total INT64);", '
                    'J="INSERT INTO aggregate SELECT SUM(size) FROM intermediate;", '
                    'G="SELECT SUM(total) FROM aggregate;"'
                    ");"
                ),
                "SELECT * FROM gufi;",
                "DROP TABLE gufi;",
            ],
            build_plan=_notes_total_size_plan,
            check_answer=lambda ans: ans is not None and int(str(ans)) == 27395,
            description="Aggregate total bytes (§14-style)",
        ),
        EvalCase(
            case_id="notes_top3_files",
            index="notes",
            question="Show the top 3 largest regular files in notes",
            expected_route="pipeline",
            raw_sql=[
                (
                    "SELECT name, size FROM gufi_vt_vrpentries('{index_path}', 4) "
                    "WHERE type = 'f' ORDER BY size DESC LIMIT 3;"
                ),
            ],
            build_plan=lambda: _notes_top_files_plan(3),
            check_answer=lambda ans: isinstance(ans, list) and len(ans) == 3,
            description="Listing with LIMIT",
        ),
    ]


def run_raw_arm(
    *,
    index: str,
    sql_statements: list[str],
    indexes_root: Path,
    executor: GufiVtExecutor,
) -> ArmResult:
    index_path = str(resolve_index_path(index, indexes_root)) + "/"
    formatted = [stmt.format(index_path=index_path) for stmt in sql_statements]
    started = time.perf_counter()
    result = executor.execute_sql(formatted)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if not result.success:
        return ArmResult(
            success=False,
            elapsed_ms=elapsed_ms,
            answer=None,
            error=result.error,
        )

    if len(result.rows) == 1 and len(result.rows[0]) == 1:
        answer: Any = result.rows[0][0]
    else:
        answer = result.rows
    return ArmResult(success=True, elapsed_ms=elapsed_ms, answer=answer)


def run_structured_arm(
    *,
    plan: dict[str, Any],
    schema_registry: SchemaRegistry,
    indexes_root: Path,
    logger: EventLogger,
) -> tuple[ArmResult, int, float]:
    validated = log_pipeline_step(
        logger,
        "validate_query_plan",
        {"plan": plan},
        lambda plan: validate_plan(plan, schema_registry=schema_registry),
    )
    plan_hash_value = validated["plan_hash"]

    log_pipeline_step(
        logger,
        "estimate_query_cost",
        {"plan": plan},
        lambda plan: estimate_query_cost(plan, schema_registry=schema_registry),
    )
    log_pipeline_step(
        logger,
        "explain_query_plan",
        {"plan": plan},
        lambda plan: explain_plan(
            plan,
            schema_registry=schema_registry,
            indexes_root=indexes_root,
        ),
    )
    log_pipeline_step(
        logger,
        "execute_query_plan",
        {"plan": plan, "plan_hash": plan_hash_value, "dry_run": True},
        lambda plan, plan_hash, dry_run=False, approved=False: execute_query_plan(
            plan,
            plan_hash,
            dry_run=dry_run,
            approved=approved,
            schema_registry=schema_registry,
            indexes_root=indexes_root,
        ),
    )
    started = time.perf_counter()
    live = log_pipeline_step(
        logger,
        "execute_query_plan",
        {"plan": plan, "plan_hash": plan_hash_value, "dry_run": False},
        lambda plan, plan_hash, dry_run=False, approved=False: execute_query_plan(
            plan,
            plan_hash,
            dry_run=dry_run,
            approved=approved,
            schema_registry=schema_registry,
            indexes_root=indexes_root,
        ),
    )
    elapsed_ms = (time.perf_counter() - started) * 1000

    all_events = logger.read_events()
    gap_total = sum(event.get("gap_since_prev_ms", 0) for event in all_events)

    if not live.get("success"):
        return (
            ArmResult(
                success=False,
                elapsed_ms=elapsed_ms,
                answer=None,
                error=str(live.get("message") or live.get("error")),
                step_count=len(all_events),
            ),
            len(all_events),
            gap_total,
        )

    rows = live.get("rows") or []
    if len(rows) == 1 and len(rows[0]) == 1:
        answer: Any = rows[0][0]
    else:
        answer = rows

    return (
        ArmResult(
            success=True,
            elapsed_ms=elapsed_ms,
            answer=answer,
            step_count=len(all_events),
        ),
        len(all_events),
        gap_total,
    )


def run_eval_suite(
    *,
    schema_registry: SchemaRegistry,
    indexes_root: Path,
    vt_lib: Path,
    cases: list[EvalCase] | None = None,
    log_path: Path | None = None,
) -> EvalReport:
    reset_executor()
    cases = cases or default_eval_cases()
    executor = GufiVtExecutor(vt_lib)
    report = EvalReport(index="notes")

    raw_times: list[float] = []
    structured_times: list[float] = []

    with tempfile.TemporaryDirectory(prefix="gufi_eval_") as tmp:
        for case in cases:
            case_log = log_path or Path(tmp) / f"{case.case_id}.jsonl"
            logger = EventLogger(case_log)

            raw = run_raw_arm(
                index=case.index,
                sql_statements=case.raw_sql,
                indexes_root=indexes_root,
                executor=executor,
            )
            structured, tool_steps, gap_total = run_structured_arm(
                plan=case.build_plan(),
                schema_registry=schema_registry,
                indexes_root=indexes_root,
                logger=logger,
            )

            correctness_raw = raw.success and case.check_answer(raw.answer)
            correctness_structured = structured.success and case.check_answer(structured.answer)
            raw_faster = raw.elapsed_ms < structured.elapsed_ms

            raw_times.append(raw.elapsed_ms)
            structured_times.append(structured.elapsed_ms)

            report.cases.append(
                CaseComparison(
                    case_id=case.case_id,
                    question=case.question,
                    expected_route=case.expected_route,
                    raw=raw,
                    structured=structured,
                    structured_tool_steps=tool_steps,
                    structured_gap_ms_total=gap_total,
                    correctness_raw=correctness_raw,
                    correctness_structured=correctness_structured,
                    raw_faster=raw_faster,
                )
            )

    report.summary = {
        "case_count": len(cases),
        "raw_avg_ms": round(sum(raw_times) / len(raw_times), 2) if raw_times else 0,
        "structured_execute_avg_ms": round(
            sum(structured_times) / len(structured_times), 2
        )
        if structured_times
        else 0,
        "structured_correct": sum(1 for c in report.cases if c.correctness_structured),
        "raw_correct": sum(1 for c in report.cases if c.correctness_raw),
        "raw_faster_count": sum(1 for c in report.cases if c.raw_faster),
        "note": (
            "Structured arm includes validate/cost/explain/dry-run/execute steps; "
            "raw arm is single gufi_vt SQL. Expect raw faster on tiny indexes; "
            "structured adds guardrails and audit trail via JSONL."
        ),
    }
    return report


def write_eval_report(report: EvalReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
