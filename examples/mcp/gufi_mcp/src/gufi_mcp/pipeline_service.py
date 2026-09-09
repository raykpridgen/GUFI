"""
Module 6: orchestration for QueryPlan MCP tools (validate, cost, explain, execute).
"""

from __future__ import annotations

import re
from typing import Any

from gufi_mcp.gufi_vt_executor import (
    ExecuteResult,
    execute_plan,
    execute_result_to_dict,
    get_executor,
)
from gufi_mcp.query_plan import (
    QueryPlanPipeline,
    _has_aggregate_stage,
    _listing_stage,
    _parse_table_from_sql,
    explain_report_to_dict,
    plan_hash,
    validation_report_to_dict,
)
from gufi_util import SchemaRegistry, resolve_index_path


def _resolve_index_path_for_plan(pipeline: QueryPlanPipeline, indexes_root) -> str:
    index_name = pipeline.plan.index.strip() or "notes"
    return str(resolve_index_path(index_name, indexes_root)) + "/"


def validate_plan(
    plan: dict,
    *,
    schema_registry: SchemaRegistry,
) -> dict[str, Any]:
    """Validate plan and attach plan_hash when valid."""
    pipeline = QueryPlanPipeline(plan, schema_registry=schema_registry)
    report = pipeline.validate(schema_registry)
    result = validation_report_to_dict(report)
    if report.valid:
        result["plan_hash"] = plan_hash(report.normalized_plan)
    return result


def estimate_query_cost(
    plan: dict,
    *,
    schema_registry: SchemaRegistry,
) -> dict[str, Any]:
    """Static-only cost estimate (no index I/O)."""
    pipeline = QueryPlanPipeline(plan, schema_registry=schema_registry)
    validation = pipeline.validate(schema_registry)
    query_plan = pipeline.plan

    signals: list[str] = []
    score = 0

    has_prune = bool(query_plan.pipeline.tree_sql or query_plan.pipeline.summary_sql)
    has_entries = bool(query_plan.pipeline.entries_sql)

    listing = _listing_stage(query_plan)
    if has_entries and not has_prune and query_plan.scope.max_level is None:
        if _has_aggregate_stage(query_plan):
            score += 1
            signals.append("tree traversal with in-index aggregation (bounded by -G)")
        elif listing is not None and 0 < query_plan.output.row_limit <= 1000:
            score += 1
            signals.append(
                f"listing over full tree with row_limit={query_plan.output.row_limit}"
            )
        else:
            score += 3
            signals.append("full-tree file scan (no -T/-S prune, no max_level)")

    if listing is not None and not (
        0 < query_plan.output.row_limit <= 1000 and not _has_aggregate_stage(query_plan)
    ):
        score += 1
        signals.append("listing query streams rows through gufi_vt_*")

    entries_sql = query_plan.pipeline.entries_sql or ""
    if entries_sql and not re.search(r"\bWHERE\b", entries_sql, re.IGNORECASE):
        score += 2
        signals.append("entries stage has no WHERE clause")

    internal_warnings = [w for w in validation.warnings if "internal" in w.lower()]
    if internal_warnings:
        score += 2
        signals.extend(internal_warnings)

    if query_plan.output.row_limit > 10_000:
        score += 1
        signals.append(f"large row_limit ({query_plan.output.row_limit})")

    if score >= 4:
        cost_level = "HIGH"
    elif score >= 2:
        cost_level = "MEDIUM"
    else:
        cost_level = "LOW"

    tables_touched = sorted({
        table
        for stage_name, sql in pipeline.active_stages()
        if stage_name in ("tree_sql", "summary_sql", "entries_sql")
        and (table := _parse_table_from_sql(sql)) is not None
    })

    return {
        "cost_level": cost_level,
        "score": score,
        "signals": signals,
        "tables_touched": tables_touched,
        "valid": validation.valid,
        "validation_errors": validation.errors,
        "warnings": validation.warnings,
        "requires_approval": cost_level == "HIGH",
    }


def explain_plan(
    plan: dict,
    *,
    schema_registry: SchemaRegistry,
    indexes_root,
) -> dict[str, Any]:
    pipeline = QueryPlanPipeline(plan, schema_registry=schema_registry)
    try:
        index_path = _resolve_index_path_for_plan(pipeline, indexes_root)
    except FileNotFoundError as exc:
        base = pipeline.explain(schema_registry=schema_registry)
        return {
            "error": str(exc),
            **explain_report_to_dict(base),
        }

    report = pipeline.explain(index_path=index_path, schema_registry=schema_registry)
    return explain_report_to_dict(report)


def execute_query_plan(
    plan: dict,
    plan_hash_value: str,
    *,
    dry_run: bool = False,
    approved: bool = False,
    schema_registry: SchemaRegistry,
    indexes_root,
) -> dict[str, Any]:
    """Validate, verify plan_hash, and execute via gufi_vt (or dry-run)."""
    pipeline = QueryPlanPipeline(plan, schema_registry=schema_registry)
    validation = pipeline.validate(schema_registry)

    if not validation.valid:
        return {
            "success": False,
            "error": "validation_failed",
            "validation_errors": validation.errors,
            "warnings": validation.warnings,
            "compiled_sql": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "elapsed_ms": 0.0,
        }

    normalized = validation.normalized_plan
    computed_hash = plan_hash(normalized)
    if plan_hash_value != computed_hash:
        return {
            "success": False,
            "error": "plan_hash_mismatch",
            "message": (
                "plan_hash does not match the normalized plan. "
                "Re-run validate_query_plan and use the returned plan_hash."
            ),
            "expected_plan_hash": computed_hash,
            "compiled_sql": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "elapsed_ms": 0.0,
        }

    cost = estimate_query_cost(normalized, schema_registry=schema_registry)
    if cost["cost_level"] == "HIGH" and not dry_run and not approved:
        return {
            "success": False,
            "error": "high_cost_requires_approval",
            "cost_level": "HIGH",
            "signals": cost["signals"],
            "message": "HIGH cost plan blocked. Review signals and retry with dry_run or user approval.",
            "compiled_sql": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "elapsed_ms": 0.0,
        }

    try:
        index_path = _resolve_index_path_for_plan(pipeline, indexes_root)
    except FileNotFoundError as exc:
        return {
            "success": False,
            "error": "index_not_found",
            "message": str(exc),
            "compiled_sql": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "elapsed_ms": 0.0,
        }

    try:
        executor = get_executor()
    except FileNotFoundError as exc:
        return {
            "success": False,
            "error": "gufi_vt_unavailable",
            "message": str(exc),
            "compiled_sql": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "elapsed_ms": 0.0,
        }

    result: ExecuteResult = execute_plan(
        pipeline,
        index_path,
        dry_run=dry_run,
        executor=executor,
        table_suffix=computed_hash,
    )

    payload = execute_result_to_dict(result)
    payload["plan_hash"] = computed_hash
    payload["cost_level"] = cost["cost_level"]
    if not result.success and result.error:
        payload["error"] = "execution_failed"
        payload["message"] = result.error
    return payload
