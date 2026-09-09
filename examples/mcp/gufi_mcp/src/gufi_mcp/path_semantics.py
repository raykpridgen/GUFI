"""
gufi_vt path semantics: listing vs aggregate plans and rpath availability.

Listing queries compile to outer SQLite over gufi_vt_* table functions. Context UDFs
(rpath, path, level, …) are not registered there — use VT columns instead. Aggregate
pipeline SQL runs inside gufi_query and may call rpath() freely.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gufi_mcp.query_plan import QueryPlan

LISTING_STAGES = ("tree_sql", "summary_sql", "entries_sql")

# Extra columns materialized by gufi_vt_* table functions (not in schemas.json IR tables)
GUFI_VT_LISTING_COLUMNS = frozenset({"path", "epath", "fpath", "rpath", "level"})

# gufi_query context UDFs — unavailable on outer SQLite for listing plans
_CONTEXT_UDF_NAMES = (
    "rpath",
    "spath",
    "path",
    "epath",
    "fpath",
    "level",
    "starting_point",
    "subdirs",
    "pinode",
    "thread_id",
    "setstr",
)

_CONTEXT_UDF_CALL = re.compile(
    r"\b(" + "|".join(_CONTEXT_UDF_NAMES) + r")\s*\(",
    re.IGNORECASE,
)

_RPATH_THREE_ARG = re.compile(
    r"\brpath\s*\(\s*sname\s*,\s*sroll\s*,\s*name\s*\)",
    re.IGNORECASE,
)
_RPATH_TWO_ARG = re.compile(
    r"\brpath\s*\(\s*sname\s*,\s*sroll\s*\)",
    re.IGNORECASE,
)
_ZERO_ARG_UDF_TO_COLUMN = (
    (re.compile(r"\bpath\s*\(\s*\)", re.IGNORECASE), "path"),
    (re.compile(r"\bepath\s*\(\s*\)", re.IGNORECASE), "epath"),
    (re.compile(r"\bfpath\s*\(\s*\)", re.IGNORECASE), "fpath"),
    (re.compile(r"\blevel\s*\(\s*\)", re.IGNORECASE), "level"),
)

LISTING_PATH_GUIDANCE = (
    "gufi_vt listing queries run in outer SQLite: use gufi_vt_* **columns** "
    "`path`, `epath`, `fpath`, `rpath`, `level` — not rpath()/path()/level() UDF calls. "
    "For file paths: `(rpath || '/' || name)` or `rpath || '/' || name`. "
    "For directory paths: `rpath`. "
    "To use rpath(sname, sroll[, name]) in SQL, switch to an aggregate pipeline (-I/-E/-K/-J/-G)."
)


def _has_aggregate_pipeline(plan: QueryPlan) -> bool:
    pipeline = plan.pipeline
    return bool(
        pipeline.init
        and pipeline.aggregate_create
        and pipeline.aggregate_insert
    )


def listing_stage_sql(plan: QueryPlan) -> tuple[str, str] | None:
    """Return (stage_name, sql) for a single-stage SELECT listing plan."""
    if _has_aggregate_pipeline(plan):
        return None

    active = [
        (stage, getattr(plan.pipeline, stage).strip())
        for stage in LISTING_STAGES
        if getattr(plan.pipeline, stage) and getattr(plan.pipeline, stage).strip()
    ]
    if len(active) != 1:
        return None

    stage_name, sql = active[0]
    if not sql.lstrip().upper().startswith("SELECT"):
        return None
    return stage_name, sql


def uses_context_udf(sql: str) -> bool:
    return _CONTEXT_UDF_CALL.search(sql) is not None


def rewrite_listing_path_expressions(sql: str) -> str:
    """Rewrite common gufi_query UDF calls to gufi_vt_* column expressions."""
    rewritten = _RPATH_THREE_ARG.sub("(rpath || '/' || name)", sql)
    rewritten = _RPATH_TWO_ARG.sub("rpath", rewritten)
    for pattern, column in _ZERO_ARG_UDF_TO_COLUMN:
        rewritten = pattern.sub(column, rewritten)
    return rewritten


def validate_listing_path_semantics(plan: QueryPlan) -> tuple[list[str], list[str]]:
    """
    Return (errors, warnings) for path UDF usage on gufi_vt listing plans.
    """
    listing = listing_stage_sql(plan)
    if listing is None:
        return [], []

    stage_name, sql = listing
    errors: list[str] = []
    warnings: list[str] = []

    if not uses_context_udf(sql):
        return errors, warnings

    rewritten = rewrite_listing_path_expressions(sql)
    if rewritten != sql:
        warnings.append(
            f"{stage_name}: context UDF(s) in listing SQL will be rewritten at compile time "
            f"to gufi_vt column form. Prefer writing `(rpath || '/' || name)` instead of "
            f"rpath(sname, sroll, name). See gufi://udfs listing_path_rules."
        )
        if uses_context_udf(rewritten):
            errors.append(
                f"{stage_name}: listing SQL still uses unsupported context UDF(s) after "
                f"rewrite. {LISTING_PATH_GUIDANCE}"
            )
    else:
        errors.append(
            f"{stage_name}: listing SQL uses gufi_query context UDF(s) (e.g. rpath(...)) "
            f"that gufi_vt does not expose on the outer connection. {LISTING_PATH_GUIDANCE}"
        )

    return errors, warnings
