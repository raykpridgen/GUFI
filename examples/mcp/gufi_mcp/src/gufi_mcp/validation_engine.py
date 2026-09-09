"""
Module 4: QueryPlan pipeline validation rules (pure function, no I/O).

Presence/consistency checks for the canonical -I/-K/-J/-G aggregate shape plus
table-name cross-checks between init, aggregate_create, aggregate_insert, and
final_select (design §6.4).
"""

from __future__ import annotations

import re
from typing import Any

from gufi_mcp.query_plan import QueryPlan

_CREATE_TABLE_RE = re.compile(r"\bCREATE\s+TABLE\s+(\w+)", re.IGNORECASE)
_INSERT_INTO_RE = re.compile(r"\bINSERT\s+INTO\s+(\w+)", re.IGNORECASE)
_FROM_TABLE_RE = re.compile(r"\bFROM\s+(\w+)", re.IGNORECASE)


def _parse_create_table_name(sql: str | None) -> str | None:
    if not sql:
        return None
    match = _CREATE_TABLE_RE.search(sql.strip())
    return match.group(1).lower() if match else None


def _parse_insert_target(sql: str | None) -> str | None:
    if not sql:
        return None
    match = _INSERT_INTO_RE.search(sql.strip())
    return match.group(1).lower() if match else None


def _parse_from_table(sql: str | None) -> str | None:
    if not sql:
        return None
    match = _FROM_TABLE_RE.search(sql.strip())
    return match.group(1).lower() if match else None


def _stage_set(pipeline: QueryPlan) -> dict[str, bool]:
    p = pipeline.pipeline
    return {
        "init": bool(p.init and p.init.strip()),
        "aggregate_create": bool(p.aggregate_create and p.aggregate_create.strip()),
        "aggregate_insert": bool(p.aggregate_insert and p.aggregate_insert.strip()),
        "final_select": bool(p.final_select and p.final_select.strip()),
    }


def validate_pipeline_consistency(plan: QueryPlan | dict[str, Any]) -> list[str]:
    """
    Return validation errors for aggregate pipeline presence and table-name consistency.
    """
    if not isinstance(plan, QueryPlan):
        from gufi_mcp.query_plan import _coerce_plan

        pipeline = _coerce_plan(plan)
    else:
        pipeline = plan
    stages = _stage_set(pipeline)
    errors: list[str] = []

    has_init = stages["init"]
    has_k = stages["aggregate_create"]
    has_j = stages["aggregate_insert"]
    has_g = stages["final_select"]

    if has_k and not has_init:
        errors.append("aggregate_create (-K): requires init (-I) to be set.")
    if has_j and not has_init:
        errors.append("aggregate_insert (-J): requires init (-I) to be set.")
    if has_j and not has_k:
        errors.append("aggregate_insert (-J): requires aggregate_create (-K) to be set.")

    if has_init:
        if not has_k:
            errors.append("init (-I): aggregate_create (-K) must also be set.")
        if not has_j:
            errors.append("init (-I): aggregate_insert (-J) must also be set.")
        if not has_g:
            errors.append("init (-I): final_select (-G) must also be set.")

    if not has_init:
        return errors

    p = pipeline.pipeline
    intermediate = _parse_create_table_name(p.init)
    aggregate = _parse_create_table_name(p.aggregate_create)

    if intermediate is None:
        errors.append("init (-I): could not parse CREATE TABLE name for intermediate table.")
    if aggregate is None:
        errors.append(
            "aggregate_create (-K): could not parse CREATE TABLE name for aggregate table."
        )

    if aggregate and has_j:
        insert_target = _parse_insert_target(p.aggregate_insert)
        from_table = _parse_from_table(p.aggregate_insert)
        if insert_target is None:
            errors.append("aggregate_insert (-J): could not parse INSERT INTO target table.")
        elif aggregate and insert_target != aggregate:
            errors.append(
                f"aggregate_insert (-J): INSERT INTO '{insert_target}' "
                f"does not match aggregate table '{aggregate}' from aggregate_create (-K)."
            )
        if intermediate and from_table and from_table != intermediate:
            errors.append(
                f"aggregate_insert (-J): FROM '{from_table}' "
                f"does not match intermediate table '{intermediate}' from init (-I)."
            )

    if aggregate and has_g:
        final_from = _parse_from_table(p.final_select)
        if final_from is None:
            errors.append("final_select (-G): could not parse FROM table.")
        elif aggregate and final_from != aggregate:
            errors.append(
                f"final_select (-G): FROM '{final_from}' "
                f"does not match aggregate table '{aggregate}' from aggregate_create (-K)."
            )

    if intermediate:
        for stage_name in ("entries_sql", "summary_sql", "tree_sql"):
            sql = getattr(p, stage_name)
            if not sql or not sql.strip():
                continue
            if not sql.lstrip().upper().startswith("INSERT"):
                continue
            target = _parse_insert_target(sql)
            if target and target != intermediate:
                errors.append(
                    f"{stage_name}: INSERT INTO '{target}' "
                    f"does not match intermediate table '{intermediate}' from init (-I)."
                )

    return errors
