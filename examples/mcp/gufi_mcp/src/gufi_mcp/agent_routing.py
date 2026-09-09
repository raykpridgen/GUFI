"""
Module 8: agent routing guidance and prompt text for GUFI MCP tools.

Encodes the routing gate (wrapper tools vs structured QueryPlan pipeline) and
builds the plan_gufi_query prompt body (design §3, §4.3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

RouteKind = Literal["wrapper", "pipeline"]

_PIPELINE_TOOL_NAMES = (
    "new_query_plan",
    "validate_query_plan",
    "estimate_query_cost",
    "explain_query_plan",
    "execute_query_plan",
)

_WRAPPER_RESOURCES = ("gufi://indexes",)
_PIPELINE_RESOURCES = (
    "gufi://indexes",
    "gufi://schemas/query_surfaces",
    "gufi://udfs",
    "gufi://query-flags",
    "gufi://rollup-semantics",
)


@dataclass(frozen=True)
class RoutingDecision:
    route: RouteKind
    reason: str
    suggested_tools: tuple[str, ...]


PROJECT_OVERVIEW = """\
## GUFI MCP — what this server does

GUFI indexes filesystem metadata (paths, sizes, owners, rollup-aware views) into
queryable SQLite databases. This MCP server lets you answer user questions about
indexed data **safely at scale**:

- **Two tiers:** cheap `gufi_client_*` wrapper tools for simple ops; structured
  QueryPlan pipeline for custom SQL, aggregation, and guardrailed execution.
- **Wrapper-first:** try one wrapper call when the ask fits; **stop** when it
  answers the user — do not draft a QueryPlan.
- **Pipeline when needed:** validate → cost → explain → dry-run → execute with
  `plan_hash` verification for SQL/aggregation workflows.
- **Banned for agents:** do not use `gufi_query_local_index`.
"""

WRAPPER_SATISFIED_RULES = """\
## Stop rule — wrapper satisfied (critical)

If a `gufi_client_*` tool returned output that **answers the user's question**:
- **STOP.** Respond to the user with that result.
- **Do NOT** call `new_query_plan`, `validate_query_plan`, `estimate_query_cost`,
  `explain_query_plan`, or `execute_query_plan`.
- **Do NOT** load `gufi://query-flags`, `gufi://udfs`, or schema resources unless
  you are escalating to the pipeline because wrapper output was insufficient.

Escalate to the QueryPlan pipeline only when the user needs custom SQL,
multi-stage aggregation, breakdowns by uid/gid, validated `compiled_sql`, or
wrapper tools cannot express the filter.
"""

RESOURCES_REFERENCE = """\
## MCP resources

| URI | When to read |
|-----|--------------|
| `gufi://indexes` | **Always first** — discover index names and paths |
| `gufi://schemas/query_surfaces` | Pipeline path — which tables/views to use |
| `gufi://schemas/{table}` | Pipeline path — column names and notes |
| `gufi://udfs` | Pipeline path — path helpers and listing_path_rules |
| `gufi://query-flags` | Pipeline path only — stage flags -I/-T/-S/-E/-K/-J/-G |
| `gufi://rollup-semantics` | Pipeline path — rollup columns and path rules |

Wrapper path usually needs only `gufi://indexes`. On rolled-up indexes, prefer
**vrpentries** / **vrsummary** when writing pipeline SQL (see gufi_vt path rules).
"""

GUFI_VT_PATH_RULES = """\
## gufi_vt path rules (critical for execute_query_plan)

Listing plans (single SELECT in entries_sql/summary_sql/tree_sql, no -I/-K/-J) compile
to `SELECT … FROM gufi_vt_vrpentries(…)` / `gufi_vt_vrsummary(…)`. The outer SQLite
connection does **not** have `rpath()` / `path()` / `level()` UDFs.

| Need | Listing SQL (write in QueryPlan) | Notes |
|------|----------------------------------|-------|
| File path | `(rpath || '/' || name) AS path` | Uses VT columns on gufi_vt_vrpentries |
| Directory path | `rpath` | VT column on gufi_vt_vrsummary |
| uid → name | `uidtouser(uid)` | OK — general UDF, not context-bound |

**Do not** use `rpath(sname, sroll, name)` in listing SELECT — validate warns and
execute fails (see session logs). The compiler auto-rewrites the common forms when
possible; prefer writing column form directly.

**Aggregate pipeline** (-I + -E/-S + -K + -J + -G): SQL runs inside gufi_query —
`rpath(sname, sroll[, name])`, `path()`, `level()` work in -E/-S INSERT/SELECT stages.

If you need listing-style output with rpath UDFs in SQL, use an aggregate plan or
escalate to `gufi_client_find` for simple largest-files asks.
"""

APPROACH_WRAPPER = """\
## Wrapper path (simple asks — prefer this)

1. Read `gufi://indexes`; confirm the target index name.
2. Call **`gufi_route_query(question)`** or `plan_gufi_query(index, question)` for a hint.
3. One `gufi_client_*` call (`du`, `ls`, `find`, `stat`, `stats`, `getfattr`).
4. If output answers the user → **STOP** (see stop rule above).
5. Escalate to pipeline only if wrapper output is insufficient.
"""

APPROACH_PIPELINE = """\
## Pipeline path (SQL / aggregation)

1. Read `gufi://indexes`; confirm the target index.
2. Load pipeline resources: `gufi://schemas/query_surfaces`, relevant
   `gufi://schemas/{table}`, `gufi://udfs`, `gufi://rollup-semantics` as needed.
3. `new_query_plan` → fill stages → `validate_query_plan` (save `plan_hash`).
4. `estimate_query_cost` → `explain_query_plan` (show user `compiled_sql`).
5. `execute_query_plan(dry_run=True)` → `execute_query_plan(dry_run=False)`.
6. Before `-T` / `tree_sql`: call **`gufi_check_treesummary(index)`** — use
   `scope.max_level` when treesummary is absent.
7. **HIGH cost** — stop and get user approval unless dry-run only.
"""

APPROACH_WHEN_USER_ASKS = """\
## When the user requests something — approach

1. **Clarify the index** — read `gufi://indexes`.
2. **Classify the ask** — call `gufi_route_query(question)` or apply routing gate.
3. **Wrapper path** (most list/du/find/stat/size asks) — follow wrapper steps above.
4. **Pipeline path** (SQL, GROUP BY, breakdowns, validated execute) — follow pipeline steps.
5. **Prompts** — `plan_gufi_query(index, question)` for per-ask briefing;
   `find_biggest_files(index)` for largest-files; `gufi_simple_query(index, question)`
   for du/ls/find/stat asks.
"""

WRAPPER_TOOLS_REFERENCE = """\
## Wrapper tools (cheap path — use when the ask is simple)

These SSH-backed client wrappers map to familiar GUFI CLI utilities. Prefer them
when the user wants path listing, disk usage, find-by-name, stat, canned stats,
or xattrs — without custom SQL or multi-stage aggregation.

| Tool | Maps to | Use when |
|------|---------|----------|
| `gufi_client_ls` | gufi_ls | List entries under a path |
| `gufi_client_du` | gufi_du | Directory disk-usage summary |
| `gufi_client_find` | gufi_find | Find paths by name/type (incl. largest files) |
| `gufi_client_stat` | gufi_stat | Metadata for specific paths |
| `gufi_client_stats` | gufi_stats | Canned index statistics |
| `gufi_client_getfattr` | gufi_getfattr | Extended attributes |

Pass index name plus optional client flags in `arguments` (shell-style string).

Diagnostics: `gufi_route_query`, `gufi_version`, `gufi_location`,
`gufi_check_treesummary`, resource `gufi://indexes`.
"""

PIPELINE_TOOLS_REFERENCE = """\
## Structured QueryPlan pipeline (complex / SQL / aggregation)

Use when the ask needs custom SQL, aggregation (`GROUP BY`, `SUM`, top-N per key),
rollup-aware scans, multi-stage `-I/-E/-K/-J/-G`, or validated execution with
cost limits. **Do not** start here if a wrapper already answered the question.

| Step | Tool |
|------|------|
| 0. Preflight | `gufi_check_treesummary(index)` when using `-T` / `tree_sql` |
| 1. Draft | `new_query_plan` (or `template='total_file_size'` when appropriate) |
| 2. Validate | `validate_query_plan` → save `plan_hash` |
| 3. Cost | `estimate_query_cost` |
| 4. Explain | `explain_query_plan` → show user `compiled_sql` |
| 5. Execute | `execute_query_plan(plan, plan_hash)` — `dry_run=True` first |
"""

PIPELINE_WORKFLOW_STEPS = """\
Workflow (pipeline path):
1. Read pipeline resources listed above for this ask.
2. Call new_query_plan (or template='total_file_size' when appropriate).
3. Fill pipeline stages (-I/-T/-S/-E/-K/-J/-G); prefer vrpentries / vrsummary.
4. validate_query_plan → save plan_hash.
5. estimate_query_cost → review cost_level (HIGH needs user approval).
6. explain_query_plan → show the user the explanation and compiled_sql.
7. execute_query_plan with the same plan and plan_hash (dry_run=True first).
8. Never use gufi_query_local_index for agent queries.
"""

WRAPPER_ESCALATION_NOTE = """\
If wrapper tools cannot answer the ask, escalate to the structured QueryPlan pipeline
(see pipeline reference below). Do not use gufi_query_local_index.
"""

ROUTING_GATE_TEXT = """\
## Routing gate (decide before calling tools)

1. **Wrapper path** if ALL are true:
   - Single-purpose filesystem operation (list, du, find, stat, stats, xattr)
   - No `GROUP BY`, no per-user/per-group breakdown, no custom multi-table SQL
   - Result fits a CLI wrapper without a QueryPlan IR

2. **Pipeline path** if ANY are true:
   - Aggregation, ranking by computed totals, or top-N per uid/gid/path
   - Custom filters across many rows with server-enforced `row_limit`
   - User needs validation, cost estimate, or `compiled_sql` preview before run

3. **When unsure:** call `gufi_route_query(question)` — default is **wrapper first**;
   escalate to pipeline only if wrapper output is insufficient.
"""

_BREAKDOWN_HINT = re.compile(
    r"\b(by uid|by user|by gid|by group|group by|breakdown|break down|"
    r"per uid|per user|per gid|top \d+.*\b(by uid|by user|by gid))\b",
    re.I,
)

_PIPELINE_PATTERNS: tuple[tuple[re.Pattern[str], str, tuple[str, ...]], ...] = (
    (
        re.compile(
            r"\b(group by|aggregate|aggregation|sum\(|count\(|avg\()\b",
            re.I,
        ),
        "aggregation or analytic SQL implied",
        _PIPELINE_TOOL_NAMES,
    ),
    (
        re.compile(r"\b(custom sql|query plan|pipeline|validate.*plan|compiled_sql)\b", re.I),
        "user explicitly requested structured pipeline",
        _PIPELINE_TOOL_NAMES,
    ),
)

_WRAPPER_PATTERNS: tuple[tuple[re.Pattern[str], str, tuple[str, ...]], ...] = (
    (
        re.compile(
            r"\b(largest files?|biggest files?|find .*files?|list files?|"
            r"files?\s+(larger|bigger|greater|over|above)|larger than|bigger than)\b",
            re.I,
        ),
        "simple file listing or find via client wrapper",
        ("gufi_client_find", "gufi_client_ls"),
    ),
    (
        re.compile(
            r"\b(total size|total bytes|how much space|sum of|combined size|"
            r"how big|disk usage|du\b)\b",
            re.I,
        ),
        "simple disk-usage total via gufi_du",
        ("gufi_client_du",),
    ),
    (
        re.compile(r"\b(largest files?|biggest files?|find .*files?|list files?|ls\b)\b", re.I),
        "simple listing or find via client wrapper",
        ("gufi_client_find", "gufi_client_du", "gufi_client_ls"),
    ),
    (
        re.compile(r"\bgufi_(ls|du|find|stat|stats|getfattr)\b", re.I),
        "named wrapper utility",
        ("gufi_client_find", "gufi_client_du", "gufi_client_ls"),
    ),
    (
        re.compile(r"\b(stat|extended attribute|xattr|getfattr)\b", re.I),
        "path stat or xattr lookup",
        ("gufi_client_stat", "gufi_client_getfattr"),
    ),
    (
        re.compile(r"\b(index stats|canned stats|summary stats|files-per-level)\b", re.I),
        "canned statistics",
        ("gufi_client_stats",),
    ),
)


def _has_breakdown(text: str) -> bool:
    return _BREAKDOWN_HINT.search(text) is not None


def route_query(question: str) -> RoutingDecision:
    """Classify a natural-language GUFI question into wrapper vs pipeline route."""
    text = question.strip()
    if not text:
        return RoutingDecision(
            route="wrapper",
            reason="empty question; try wrapper first",
            suggested_tools=("gufi_client_ls", "gufi_route_query"),
        )

    if _has_breakdown(text):
        return RoutingDecision(
            route="pipeline",
            reason="breakdown or group-by aggregation implied",
            suggested_tools=_PIPELINE_TOOL_NAMES,
        )

    for pattern, reason, tools in _PIPELINE_PATTERNS:
        if pattern.search(text):
            return RoutingDecision(route="pipeline", reason=reason, suggested_tools=tools)

    for pattern, reason, tools in _WRAPPER_PATTERNS:
        if pattern.search(text):
            return RoutingDecision(route="wrapper", reason=reason, suggested_tools=tools)

    if re.search(r"\b(how many|count|number of)\b", text, re.I):
        return RoutingDecision(
            route="pipeline",
            reason="counting queries need SQL or aggregation guardrails",
            suggested_tools=_PIPELINE_TOOL_NAMES,
        )

    return RoutingDecision(
        route="wrapper",
        reason="no clear pipeline match; try wrapper first and escalate if insufficient",
        suggested_tools=("gufi_client_du", "gufi_client_find", "gufi_client_ls"),
    )


def build_routing_payload(question: str) -> dict[str, Any]:
    """Structured routing result for gufi_route_query MCP tool."""
    decision = route_query(question)
    if decision.route == "wrapper":
        return {
            "route": decision.route,
            "reason": decision.reason,
            "recommended_tools": list(decision.suggested_tools),
            "avoid_tools": list(_PIPELINE_TOOL_NAMES),
            "resources_needed": list(_WRAPPER_RESOURCES),
            "stop_if_satisfied": (
                "If a gufi_client_* call answers the user, STOP — do not call "
                "new_query_plan or other pipeline tools."
            ),
        }
    return {
        "route": decision.route,
        "reason": decision.reason,
        "recommended_tools": list(decision.suggested_tools),
        "avoid_tools": [],
        "resources_needed": list(_PIPELINE_RESOURCES),
        "stop_if_satisfied": (
            "Complete the pipeline workflow; wrapper tools are unlikely to suffice."
        ),
    }


def build_session_briefing_prompt(*, default_index: str = "notes") -> str:
    """General session-start briefing: project context, resources, and approach."""
    return (
        "You are connected to the GUFI MCP server for indexed filesystem queries.\n\n"
        f"{PROJECT_OVERVIEW}\n"
        f"{WRAPPER_SATISFIED_RULES}\n"
        f"{RESOURCES_REFERENCE}\n"
        f"{GUFI_VT_PATH_RULES}\n"
        f"{APPROACH_WHEN_USER_ASKS}\n"
        f"{APPROACH_WRAPPER}\n"
        f"{APPROACH_PIPELINE}\n"
        f"{ROUTING_GATE_TEXT}\n"
        f"{WRAPPER_TOOLS_REFERENCE}\n"
        f"{PIPELINE_TOOLS_REFERENCE}\n"
        f"Default index when unspecified: `{default_index}`. "
        "For each user question, call `gufi_route_query(question)` or "
        "`plan_gufi_query(index, question)` for a routing hint.\n"
    )


def build_plan_gufi_query_prompt(index: str, question: str, *, default_index: str = "notes") -> str:
    """Build the full plan_gufi_query MCP prompt (Module 8)."""
    target = index.strip() or default_index
    decision = route_query(question)

    header = (
        f"Answer this GUFI question for index '{target}':\n{question.strip()}\n\n"
        f"Routing hint: **{decision.route}** — {decision.reason}. "
        f"Suggested tools: {', '.join(decision.suggested_tools)}.\n\n"
    )

    shared = (
        f"{WRAPPER_SATISFIED_RULES}\n"
        f"{ROUTING_GATE_TEXT}\n"
        f"{WRAPPER_TOOLS_REFERENCE}\n"
    )

    if decision.route == "wrapper":
        return (
            header
            + f"{APPROACH_WRAPPER}\n"
            + shared
            + f"{WRAPPER_ESCALATION_NOTE}\n\n"
            + f"{PIPELINE_TOOLS_REFERENCE}\n"
        )

    return (
        header
        + f"{APPROACH_PIPELINE}\n"
        + f"{GUFI_VT_PATH_RULES}\n"
        + f"{RESOURCES_REFERENCE}\n"
        + f"{ROUTING_GATE_TEXT}\n"
        + f"{WRAPPER_TOOLS_REFERENCE}\n"
        + f"{PIPELINE_TOOLS_REFERENCE}\n"
        + f"{PIPELINE_WORKFLOW_STEPS}\n"
    )


def build_simple_query_prompt(
    index: str, question: str, *, default_index: str = "notes"
) -> str:
    """Wrapper-only prompt for du/ls/find/stat asks."""
    target = index.strip() or default_index
    decision = route_query(question)
    return (
        f"Answer this GUFI question for index '{target}' using wrapper tools:\n"
        f"{question.strip()}\n\n"
        f"Routing hint: **{decision.route}** — {decision.reason}. "
        f"Suggested tools: {', '.join(decision.suggested_tools)}.\n\n"
        f"{WRAPPER_SATISFIED_RULES}\n"
        f"{APPROACH_WRAPPER}\n"
        f"{WRAPPER_TOOLS_REFERENCE}\n"
        "Do not call new_query_plan unless wrapper output is insufficient.\n"
        "Do not use gufi_query_local_index.\n"
    )


def build_find_biggest_files_prompt(index: str, *, default_index: str = "notes") -> str:
    """Prompt for find-biggest-files — routes to wrapper tier first."""
    target = index.strip() or default_index
    return (
        f"Find the largest files in GUFI index '{target}'.\n\n"
        f"{WRAPPER_SATISFIED_RULES}\n"
        f"{APPROACH_WRAPPER}\n"
        f"{ROUTING_GATE_TEXT}\n"
        "This ask matches the **wrapper** path: start with `gufi_client_find`. "
        "Only escalate to the structured QueryPlan pipeline if the user needs "
        "custom SQL, aggregation, or validated execution.\n\n"
        f"{WRAPPER_TOOLS_REFERENCE}\n"
        f"{WRAPPER_ESCALATION_NOTE}\n\n"
        f"{PIPELINE_TOOLS_REFERENCE}\n"
        "Do not use gufi_query_local_index."
    )
