"""
Phase 1 mockup: structured QueryPlan IR, validation, explain, and compile.

The agent builds a QueryPlan (JSON or Python dataclasses), validates it,
reads a human-readable explanation, and obtains compiled gufi_vt SQL (or
legacy gufi_query argv) without executing against an index.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from gufi_util import SchemaRegistry, is_valid_sql_query, validate_query_columns

INTERNAL_TABLES = frozenset({
    "entries",
    "summary",
    "pentries_rollup",
    "xattrs_pwd",
    "xattrs_rollup",
    "external_dbs_pwd",
})

STAGE_TO_FLAG: dict[str, str] = {
    "init": "-I",
    "tree_sql": "-T",
    "summary_sql": "-S",
    "entries_sql": "-E",
    "aggregate_create": "-K",
    "aggregate_insert": "-J",
    "final_select": "-G",
}

STAGE_DESCRIPTIONS: dict[str, str] = {
    "init": "Initialize intermediate SQLite tables (-I)",
    "tree_sql": "Prune or aggregate subtrees via treesummary (-T)",
    "summary_sql": "Filter or aggregate directory summaries (-S)",
    "entries_sql": "Scan file and symlink rows (-E)",
    "aggregate_create": "Create final aggregate table (-K)",
    "aggregate_insert": "Populate aggregate table from intermediate (-J)",
    "final_select": "Return final result rows (-G)",
}

STAGE_TO_VT_KEY: dict[str, str] = {
    "init": "I",
    "tree_sql": "T",
    "summary_sql": "S",
    "entries_sql": "E",
    "aggregate_create": "K",
    "aggregate_insert": "J",
    "final_select": "G",
}

LISTING_STAGES = ("tree_sql", "summary_sql", "entries_sql")

TABLE_TO_VT_FUNC: dict[str, str] = {
    "vrpentries": "gufi_vt_vrpentries",
    "vrsummary": "gufi_vt_vrsummary",
    "entries": "gufi_vt_entries",
    "summary": "gufi_vt_summary",
    "treesummary": "gufi_vt_treesummary",
    "pentries": "gufi_vt_pentries",
}


@dataclass
class TableRef:
    name: str
    role: str = "scan"
    columns: list[str] = field(default_factory=list)


@dataclass
class QueryScope:
    subpath: str = ""
    min_level: int = 0
    max_level: int | None = None
    use_treesummary: bool = False


@dataclass
class QueryOutput:
    delimiter: str = "\t"
    row_limit: int = 1000


@dataclass
class QueryExecution:
    threads: int = 4
    dry_run: bool = False


@dataclass
class QueryPipeline:
    init: str | None = None
    tree_sql: str | None = None
    summary_sql: str | None = None
    entries_sql: str | None = None
    aggregate_create: str | None = None
    aggregate_insert: str | None = None
    final_select: str | None = None


@dataclass
class QueryPlan:
    version: int = 1
    index: str = ""
    intent: str = ""
    scope: QueryScope = field(default_factory=QueryScope)
    tables_used: list[TableRef] = field(default_factory=list)
    pipeline: QueryPipeline = field(default_factory=QueryPipeline)
    output: QueryOutput = field(default_factory=QueryOutput)
    execution: QueryExecution = field(default_factory=QueryExecution)
    allow_internal_tables: bool = False


@dataclass
class ValidationReport:
    valid: bool
    errors: list[str]
    warnings: list[str]
    normalized_plan: dict[str, Any]


@dataclass
class ExplainReport:
    summary: str
    stages: list[dict[str, str]]
    compiled_argv: list[str]
    compiled_sql: list[str]
    assumptions: list[str]
    risks: list[str]


def _parse_table_from_sql(sql: str | None) -> str | None:
    if not sql:
        return None
    match = re.search(r"\bFROM\b\s+(\w+)", sql, re.IGNORECASE)
    return match.group(1) if match else None


def _looks_scalar_aggregate(sql: str | None) -> bool:
    if not sql:
        return False
    upper = sql.upper()
    return any(token in upper for token in ("SUM(", "COUNT(", "AVG(", "MIN(", "MAX("))


def plan_hash(plan: QueryPlan | dict[str, Any]) -> str:
    """Stable content hash for plan identity (used in temp virtual-table names)."""
    normalized = plan if isinstance(plan, dict) else asdict(_coerce_plan(plan))
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def escape_gufi_vt_module_value(sql: str) -> str:
    """
    Wrap a SQL fragment for gufi_vt key=value module arguments.

    Uses double-quoted values (GUFI regression / Module 1 spike style). Inner
    double quotes are doubled for SQLite's module-argument tokenizer.
    """
    return '"' + sql.replace('"', '""') + '"'


def _normalize_index_path(index_path: str) -> str:
    path = index_path.rstrip("/")
    return f"{path}/"


def _has_aggregate_stage(plan: QueryPlan) -> bool:
    pipeline = plan.pipeline
    return bool(
        pipeline.init
        and pipeline.aggregate_create
        and pipeline.aggregate_insert
    )


def _listing_stage(plan: QueryPlan) -> tuple[str, str] | None:
    """Return (stage_name, sql) when the plan is a single-table listing query."""
    if _has_aggregate_stage(plan):
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


def _ensure_limit(sql: str, row_limit: int) -> str:
    base = sql.strip().rstrip(";").strip()
    if row_limit <= 0 or re.search(r"\bLIMIT\b", base, re.IGNORECASE):
        return f"{base};"
    return f"{base} LIMIT {row_limit};"


def _vt_star_call_args(plan: QueryPlan, index_path: str) -> str:
    """Positional args for gufi_vt_* table-valued functions (minimal trailing args)."""
    args = [_normalize_index_path(index_path), str(plan.execution.threads)]
    if plan.scope.min_level:
        args.append(str(plan.scope.min_level))
    return ", ".join(f"'{arg}'" if i == 0 else arg for i, arg in enumerate(args))


def _rewrite_listing_from(sql: str, table: str, vt_call: str) -> str:
    pattern = re.compile(rf"(\bFROM\b\s+){re.escape(table)}\b", re.IGNORECASE)
    if not pattern.search(sql):
        raise ValueError(f"Listing SQL does not contain FROM {table}")
    return pattern.sub(rf"\1{vt_call}", sql, count=1)


def compile_listing_sql(plan: QueryPlan, index_path: str) -> str:
    """Compile a non-aggregating listing plan to a single gufi_vt_* SELECT."""
    listing = _listing_stage(plan)
    if listing is None:
        raise ValueError("Plan is not a single-stage listing query.")

    _stage_name, sql = listing
    table = _parse_table_from_sql(sql)
    if table is None:
        raise ValueError("Could not determine table for listing rewrite.")
    vt_func = TABLE_TO_VT_FUNC.get(table)
    if vt_func is None:
        raise ValueError(f"No gufi_vt_* wrapper for table '{table}'.")

    from gufi_mcp.path_semantics import rewrite_listing_path_expressions

    sql = rewrite_listing_path_expressions(sql)
    vt_call = f"{vt_func}({_vt_star_call_args(plan, index_path)})"
    rewritten = _rewrite_listing_from(sql, table, vt_call)
    return _ensure_limit(rewritten, plan.output.row_limit)


def compile_aggregate_sql(
    plan: QueryPlan,
    index_path: str,
    *,
    table_suffix: str | None = None,
) -> list[str]:
    """Compile an aggregating plan to CREATE / SELECT / DROP gufi_vt script."""
    if not _has_aggregate_stage(plan):
        raise ValueError("Plan does not have a complete aggregate pipeline.")

    suffix = table_suffix or plan_hash(plan)
    table_name = f"plan_{suffix}"
    module_args = [
        f"index='{_normalize_index_path(index_path)}'",
        f"threads={plan.execution.threads}",
    ]
    if plan.scope.min_level:
        module_args.append(f"min_level={plan.scope.min_level}")

    for stage_name in STAGE_TO_VT_KEY:
        sql = getattr(plan.pipeline, stage_name)
        if not sql or not sql.strip():
            continue
        fragment = sql.strip()
        if stage_name == "final_select":
            fragment = _ensure_limit(fragment, plan.output.row_limit).rstrip(";")
        vt_key = STAGE_TO_VT_KEY[stage_name]
        module_args.append(f"{vt_key}={escape_gufi_vt_module_value(fragment)}")

    create = (
        f"CREATE VIRTUAL TABLE temp.{table_name} USING gufi_vt("
        + ", ".join(module_args)
        + ");"
    )
    return [
        create,
        f"SELECT * FROM temp.{table_name};",
        f"DROP TABLE temp.{table_name};",
    ]


def _coerce_plan(data: QueryPlan | dict[str, Any]) -> QueryPlan:
    if isinstance(data, QueryPlan):
        return data

    scope_data = data.get("scope") or {}
    output_data = data.get("output") or {}
    execution_data = data.get("execution") or {}
    pipeline_data = data.get("pipeline") or {}

    tables_used = [
        TableRef(**item) if isinstance(item, dict) else item
        for item in data.get("tables_used") or []
    ]

    return QueryPlan(
        version=int(data.get("version", 1)),
        index=str(data.get("index", "")),
        intent=str(data.get("intent", "")),
        scope=QueryScope(**scope_data) if isinstance(scope_data, dict) else scope_data,
        tables_used=tables_used,
        pipeline=QueryPipeline(**pipeline_data) if isinstance(pipeline_data, dict) else pipeline_data,
        output=QueryOutput(**output_data) if isinstance(output_data, dict) else output_data,
        execution=QueryExecution(**execution_data) if isinstance(execution_data, dict) else execution_data,
        allow_internal_tables=bool(data.get("allow_internal_tables", False)),
    )


class QueryPlanPipeline:
    """
    Working Phase 1 flow: hold pipeline steps, validate, explain, compile argv.

    Example:
        plan = (
            QueryPlanPipeline.skeleton("notes", "Total regular-file bytes")
            .set_stage("init", "CREATE TABLE intermediate(size INT64);")
            .set_stage("entries_sql", "INSERT INTO intermediate SELECT size FROM vrpentries WHERE type = 'f';")
            .set_stage("aggregate_create", "CREATE TABLE aggregate(total INT64);")
            .set_stage("aggregate_insert", "INSERT INTO aggregate SELECT SUM(size) FROM intermediate;")
            .set_stage("final_select", "SELECT SUM(total) FROM aggregate;")
        )
        report = plan.validate(registry)
        explanation = plan.explain(index_path="/path/to/index/")
    """

    def __init__(
        self,
        plan: QueryPlan | dict[str, Any] | None = None,
        *,
        schema_registry: SchemaRegistry | None = None,
    ) -> None:
        self.plan = _coerce_plan(plan or QueryPlan())
        self.schema_registry = schema_registry

    @classmethod
    def skeleton(cls, index: str, intent: str = "") -> QueryPlanPipeline:
        """Return an empty plan the agent can fill stage by stage."""
        return cls(QueryPlan(index=index, intent=intent))

    @classmethod
    def total_file_size(cls, index: str) -> QueryPlanPipeline:
        """Known-good aggregation template from the design doc."""
        return cls(
            QueryPlan(
                index=index,
                intent="Total byte size of regular files under index",
                tables_used=[TableRef(name="vrpentries", role="scan", columns=["size", "type"])],
                pipeline=QueryPipeline(
                    init="CREATE TABLE intermediate(size INT64);",
                    entries_sql="INSERT INTO intermediate SELECT size FROM vrpentries WHERE type = 'f';",
                    aggregate_create="CREATE TABLE aggregate(total INT64);",
                    aggregate_insert="INSERT INTO aggregate SELECT SUM(size) FROM intermediate;",
                    final_select="SELECT SUM(total) FROM aggregate;",
                ),
            )
        )

    def set_stage(self, stage: str, sql: str | None) -> QueryPlanPipeline:
        if stage not in STAGE_TO_FLAG:
            raise ValueError(f"Unknown pipeline stage '{stage}'. Expected one of: {sorted(STAGE_TO_FLAG)}")
        setattr(self.plan.pipeline, stage, sql)
        return self

    def update(self, **fields: Any) -> QueryPlanPipeline:
        for key, value in fields.items():
            if key == "scope" and isinstance(value, dict):
                self.plan.scope = QueryScope(**value)
            elif key == "output" and isinstance(value, dict):
                self.plan.output = QueryOutput(**value)
            elif key == "execution" and isinstance(value, dict):
                self.plan.execution = QueryExecution(**value)
            elif hasattr(self.plan, key):
                setattr(self.plan, key, value)
            else:
                raise ValueError(f"Unknown QueryPlan field '{key}'")
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.plan)

    def active_stages(self) -> list[tuple[str, str]]:
        stages: list[tuple[str, str]] = []
        for stage_name in STAGE_TO_FLAG:
            sql = getattr(self.plan.pipeline, stage_name)
            if sql and sql.strip():
                stages.append((stage_name, sql.strip()))
        return stages

    def validate(self, schema_registry: SchemaRegistry | None = None) -> ValidationReport:
        registry = schema_registry or self.schema_registry
        errors: list[str] = []
        warnings: list[str] = []

        if not self.plan.index.strip():
            errors.append("QueryPlan.index is required.")

        stages = self.active_stages()
        if not stages:
            errors.append("At least one pipeline stage with SQL is required.")

        has_prune = bool(self.plan.pipeline.tree_sql or self.plan.pipeline.summary_sql)
        has_entries = bool(self.plan.pipeline.entries_sql)

        for stage_name, sql in stages:
            flag = STAGE_TO_FLAG[stage_name]

            if not is_valid_sql_query(sql):
                errors.append(f"{stage_name}: invalid SQL syntax.")

            table = _parse_table_from_sql(sql)
            validate_against_registry = stage_name in ("tree_sql", "summary_sql", "entries_sql")

            if table is None and validate_against_registry:
                warnings.append(f"{stage_name}: could not determine table from SQL.")

            if table and registry is not None and validate_against_registry:
                if table not in registry:
                    errors.append(f"{stage_name}: unknown table '{table}'.")
                else:
                    from gufi_mcp.path_semantics import GUFI_VT_LISTING_COLUMNS, listing_stage_sql

                    bad_cols = validate_query_columns(sql, table, registry)
                    if listing_stage_sql(self.plan) and stage_name in LISTING_STAGES:
                        bad_cols = [
                            col
                            for col in bad_cols
                            if col.lower() not in GUFI_VT_LISTING_COLUMNS
                        ]
                    if bad_cols:
                        errors.append(f"{stage_name}: unknown column(s) {bad_cols} for table '{table}'.")

                if table in INTERNAL_TABLES and not self.plan.allow_internal_tables:
                    warnings.append(
                        f"{stage_name}: table '{table}' is internal; user approval required before execute."
                    )

            if stage_name == "summary_sql" and table == "vrpentries":
                errors.append("summary_sql (-S): vrpentries cannot be used in the summary stage.")

            if stage_name == "entries_sql" and table == "vrsummary":
                warnings.append("entries_sql (-E): vrsummary is directory-level; prefer vrpentries for file scans.")

        if self.plan.pipeline.tree_sql and self.plan.pipeline.tree_sql.strip():
            warnings.append(
                "tree_sql (-T) requires treesummary on the index; call gufi_check_treesummary "
                "before execute, or use scope.max_level when treesummary is absent."
            )

        if has_entries and not has_prune and self.plan.scope.max_level is None:
            warnings.append("No -T or -S prune and no max_level: query may scan all directories in the index.")

        final_sql = self.plan.pipeline.final_select
        entries_sql = self.plan.pipeline.entries_sql
        is_listing = (
            entries_sql
            and entries_sql.lstrip().upper().startswith("SELECT")
            and not final_sql
        )
        if is_listing and self.plan.output.row_limit <= 0:
            errors.append("Listing queries require output.row_limit > 0.")
        elif final_sql and not _looks_scalar_aggregate(final_sql) and self.plan.output.row_limit <= 0:
            errors.append("Listing queries require output.row_limit > 0 unless final_select is a scalar aggregate.")

        if self.plan.tables_used:
            declared = {ref.name for ref in self.plan.tables_used}
            referenced = {
                table
                for stage_name, sql in stages
                if stage_name in ("tree_sql", "summary_sql", "entries_sql")
                and (table := _parse_table_from_sql(sql)) is not None
            }
            undeclared = referenced - declared
            if undeclared:
                warnings.append(f"tables_used omits referenced table(s): {sorted(undeclared)}")

        from gufi_mcp.path_semantics import validate_listing_path_semantics
        from gufi_mcp.validation_engine import validate_pipeline_consistency

        path_errors, path_warnings = validate_listing_path_semantics(self.plan)
        errors.extend(path_errors)
        warnings.extend(path_warnings)
        errors.extend(validate_pipeline_consistency(self.plan))

        return ValidationReport(
            valid=not errors,
            errors=errors,
            warnings=warnings,
            normalized_plan=self.to_dict(),
        )

    def explain(
        self,
        *,
        index_path: str | None = None,
        schema_registry: SchemaRegistry | None = None,
    ) -> ExplainReport:
        validation = self.validate(schema_registry)
        stages: list[dict[str, str]] = []
        for stage_name, sql in self.active_stages():
            table = _parse_table_from_sql(sql) or "(none)"
            stages.append({
                "stage": stage_name,
                "flag": STAGE_TO_FLAG[stage_name],
                "description": STAGE_DESCRIPTIONS[stage_name],
                "table": table,
                "sql": sql,
            })

        intent = self.plan.intent.strip() or "unspecified intent"
        stage_count = len(stages)
        summary = (
            f"Query against index '{self.plan.index}' for: {intent}. "
            f"The plan runs {stage_count} gufi_query stage(s) in order "
            f"({', '.join(s['flag'] for s in stages) or 'none'})."
        )

        assumptions = [
            f"Index name: {self.plan.index}",
            f"Scope subpath: {self.plan.scope.subpath or '(index root)'}",
            f"Thread count: {self.plan.execution.threads}",
            f"Output delimiter: {repr(self.plan.output.delimiter)}",
            f"Row limit: {self.plan.output.row_limit}",
        ]
        if self.plan.scope.use_treesummary:
            assumptions.append("Plan assumes treesummary tables are present on the index.")
        if self.plan.scope.max_level is not None:
            assumptions.append(f"Directory depth capped at level {self.plan.scope.max_level}.")

        compiled_argv: list[str] = []
        compiled_sql: list[str] = []
        if index_path:
            compiled_argv = self.compile_argv(index_path)
            try:
                compiled_sql = self.compile_sql(index_path)
            except ValueError:
                compiled_sql = []

        return ExplainReport(
            summary=summary,
            stages=stages,
            compiled_argv=compiled_argv,
            compiled_sql=compiled_sql,
            assumptions=assumptions,
            risks=list(validation.warnings),
        )

    def compile_sql(self, index_path: str, *, table_suffix: str | None = None) -> list[str]:
        """Compile the plan to gufi_vt SQL statements (does not execute)."""
        if _has_aggregate_stage(self.plan):
            return compile_aggregate_sql(
                self.plan,
                index_path,
                table_suffix=table_suffix,
            )
        if _listing_stage(self.plan) is not None:
            return [compile_listing_sql(self.plan, index_path)]
        raise ValueError("Plan cannot be compiled to gufi_vt SQL.")

    def compile_argv(self, index_path: str) -> list[str]:
        """Compile the plan to a gufi_query argv list (does not execute)."""
        argv = ["gufi_query", "-d", self.plan.output.delimiter, "-n", str(self.plan.execution.threads)]

        if self.plan.scope.min_level:
            argv.extend(["--min-level", str(self.plan.scope.min_level)])
        if self.plan.scope.max_level is not None:
            argv.extend(["--max-level", str(self.plan.scope.max_level)])

        for stage_name, sql in self.active_stages():
            argv.extend([STAGE_TO_FLAG[stage_name], sql])

        path = index_path if index_path.endswith("/") else index_path + "/"
        argv.append(path)
        return argv


def validation_report_to_dict(report: ValidationReport) -> dict[str, Any]:
    return asdict(report)


def explain_report_to_dict(report: ExplainReport) -> dict[str, Any]:
    return asdict(report)


if __name__ == "__main__":
    import json
    from pathlib import Path

    from gufi_util import get_settings, parse_schema_registry, resolve_view_types

    settings = get_settings()
    registry = resolve_view_types(parse_schema_registry(str(settings.schema_file)))

    demo = QueryPlanPipeline.total_file_size("notes")
    validation = demo.validate(registry)
    index_path = str((settings.indexes_root / "notes").resolve()) + "/"
    explanation = demo.explain(index_path=index_path, schema_registry=registry)

    print("=== Validation ===")
    print(json.dumps(validation_report_to_dict(validation), indent=2))
    print("\n=== Explain ===")
    print(json.dumps(explain_report_to_dict(explanation), indent=2))
    print("\n=== Compiled SQL ===")
    print(json.dumps(demo.compile_sql(index_path), indent=2))
