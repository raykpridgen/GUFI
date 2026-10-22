"""Validation models and execution helpers shared by the GUFI MCP server.

Every function that can fail for a reason the agent should see raises
``GufiError``. It subclasses MCP's ``ToolError``, so when it escapes a tool the
agent receives the message instead of a generic "Error executing tool".
"""

import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal, Optional

import sqlglot
from dotenv import load_dotenv
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, PrivateAttr, field_validator, model_validator
from sqlglot import exp
from sqlglot.errors import SqlglotError

logger = logging.getLogger(__name__)

load_dotenv()


class GufiError(ToolError):
    """An anticipated failure whose message is safe to return to the agent."""


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def _resolve_relative_to_module(path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(__file__).resolve().parent / candidate
    return candidate.resolve()


def _optional_source_root() -> Optional[str]:
    value = os.getenv("GUFI_SOURCE_ROOT", "").strip()
    if not value:
        return None
    if not value.startswith("/"):
        raise RuntimeError("GUFI_SOURCE_ROOT must be an absolute path")
    if any(ch in value for ch in "'\"\0"):
        raise RuntimeError("GUFI_SOURCE_ROOT must not contain quote or NUL characters")
    return value.rstrip("/")


SCHEMAFILE = _resolve_relative_to_module(_require_env("SCHEMAFILE"))
GUFIVTLIB = _require_env("GUFIVTLIB")
GUFI_INDEX_ROOT = Path(_require_env("GUFI_INDEX_ROOT")).resolve()
REMOTEHOST = os.getenv("REMOTEHOST", "").strip() or None
# Parent of the indexed source trees, as GUFI_INDEX_ROOT is the parent of the indexes:
# index <GUFI_INDEX_ROOT>/<name> was built from <GUFI_SOURCE_ROOT>/<name>. Enables spath.
GUFI_SOURCE_ROOT = _optional_source_root()
THREADS = os.cpu_count() or 1
TOOL_TIMEOUT = int(os.getenv("GUFI_MCP_TOOL_TIMEOUT", "300"))
MAX_ROWS = int(os.getenv("GUFI_MCP_MAX_ROWS", "5000"))

SQL_DIALECT = "sqlite"

# Tables each per-directory phase may read from. gufi_query runs T against
# treesummary, S against each directory's summary, and E against its entries.
TREE_TABLES = frozenset({"treesummary", "vtsummarydir", "vtsummaryuser", "vtsummarygroup"})
SUMMARY_TABLES = frozenset({"summary", "summarylong", "vsummarydir", "vsummaryuser",
                            "vsummarygroup", "vrsummary", "vrsummarylong"})
ENTRIES_TABLES = frozenset({"entries", "pentries", "pentries_rollup", "vrpentries"})
PHASE_TABLES = {"T": TREE_TABLES, "S": SUMMARY_TABLES, "E": ENTRIES_TABLES}

# Table name accepted in sql_file_index -> gufi_vt_<name>(...) function that serves it.
# gufi_vt_entries intermittently never finishes, so entries is read through
# pentries, whose columns are a superset of entries.
GUFI_VT_TABLES = {
    "treesummary": "treesummary",
    "summary": "summary",
    "entries": "pentries",
    "pentries": "pentries",
    "vrsummary": "vrsummary",
    "vrpentries": "vrpentries",
}

PER_DIRECTORY_TABLE = "per_directory"
PARTIAL_TABLE = "partial"
AGGREGATE_VTAB = "temp.gufi_mcp"


#
# Results
#

class GufiQueryResult(BaseModel):
    """Rows returned by a SQL tool."""

    columns: list[str] = Field(description="Column names, in row order.")
    rows: list[list[Any]] = Field(description="Result rows. Each row has one value per column.")
    row_count: int = Field(description="Number of rows returned (after truncation).")
    truncated: bool = Field(default=False, description="True if more rows than the row limit were available and the rest were dropped.")

    @model_validator(mode="after")
    def rows_match_columns(self) -> "GufiQueryResult":
        if self.row_count != len(self.rows):
            raise ValueError("row_count does not match the number of rows")
        width = len(self.columns)
        if any(len(row) != width for row in self.rows):
            raise ValueError("every row must have one value per column")
        return self


class GufiToolResult(BaseModel):
    """Output of a GUFI command-line tool, split into rows on the tool's delimiter."""

    command: list[str] = Field(description="The argv that was executed.")
    rows: list[list[str]] = Field(description="Non-empty output lines, split into columns.")
    row_count: int = Field(description="Number of rows returned (after truncation).")
    truncated: bool = Field(default=False, description="True if output had more lines than the row limit.")
    stderr: str = Field(default="", description="Warnings the tool printed to stderr. Non-empty output here may mean results are incomplete.")

    @model_validator(mode="after")
    def row_count_matches(self) -> "GufiToolResult":
        if self.row_count != len(self.rows):
            raise ValueError("row_count does not match the number of rows")
        return self


#
# Index and schema helpers
#

def get_gufi_indexes() -> list[str]:
    """Names of the indexes directly under GUFI_INDEX_ROOT (directories containing db.db)."""
    if not GUFI_INDEX_ROOT.is_dir():
        raise GufiError(f"GUFI index root {GUFI_INDEX_ROOT} does not exist or is not a directory")
    return sorted(entry.name for entry in GUFI_INDEX_ROOT.iterdir()
                  if entry.is_dir() and (entry / "db.db").is_file())


def resolve_local_index(index: str) -> Path:
    """Resolve an index name from gufi://indexes to its absolute path."""
    indexes = get_gufi_indexes()
    if index not in indexes:
        raise GufiError(f"Unknown index {index!r}. Available indexes: {', '.join(indexes) or 'none'}")
    path = GUFI_INDEX_ROOT / index
    # gufi_vt strips every leading/trailing quote from its arguments and does not
    # unescape doubled quotes, so a path containing quotes cannot be passed safely.
    if any(ch in str(path) for ch in "'\"\0"):
        raise GufiError(f"Index path {path} contains quote characters, which gufi_vt cannot accept")
    return path


_REMOTE_INDEX_RE = re.compile(r"^/?[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*/?$")


def resolve_remote_index(index: str) -> str:
    """Validate a path on the remote host. Remote indexes cannot be listed locally."""
    if REMOTEHOST is None:
        raise GufiError("Remote queries are not configured (REMOTEHOST is not set)")
    if not _REMOTE_INDEX_RE.fullmatch(index) or ".." in index.split("/"):
        raise GufiError(f"Remote index path {index!r} must be a plain path of letters, digits, '.', '_', '-' and '/'")
    return index.rstrip("/") or "/"


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise GufiError(f"Database {db_path} does not exist")
    try:
        return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise GufiError(f"Could not open {db_path} read-only: {e}") from e


def has_treesummary(index: str) -> bool:
    """True if the top-level database of the index has a treesummary table."""
    db_path = resolve_local_index(index) / "db.db"
    result = run_db_sql(db_path, "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'treesummary'")
    return result.row_count > 0


def schema_db_path() -> Path:
    """Database used to describe the GUFI schema: the root db.db, else the first index."""
    root_db = GUFI_INDEX_ROOT / "db.db"
    if root_db.is_file():
        return root_db
    indexes = get_gufi_indexes()
    if not indexes:
        raise GufiError(f"No GUFI indexes found under {GUFI_INDEX_ROOT}")
    return GUFI_INDEX_ROOT / indexes[0] / "db.db"


def list_schema_objects() -> dict[str, str]:
    """Map of table/view name to type ('table' or 'view') in the GUFI schema."""
    result = run_db_sql(
        schema_db_path(),
        "SELECT name, type FROM sqlite_master WHERE sql IS NOT NULL AND type IN ('table', 'view') "
        "ORDER BY type DESC, name",
    )
    return {row[0]: row[1] for row in result.rows}


def describe_schema_object(name: str) -> dict[str, dict[str, str]]:
    """Columns of one table or view, with types and hints from the schema file."""
    objects = list_schema_objects()
    if name not in objects:
        raise GufiError(f"Unknown table or view {name!r}. Read gufi://schemas/all for valid names")
    result = run_db_sql(schema_db_path(), "SELECT name, type FROM pragma_table_info(?)", (name,))
    paths = {column: {"type": "TEXT", "hint": hint} for column, hint in path_columns(name.lower()).items()}
    return {**paths, **load_schema_hints(name, [row for row in result.rows if row[0].lower() not in HIDDEN_COLUMNS])}


@lru_cache(maxsize=1)
def get_static_schema() -> dict[str, dict[str, Any]]:
    """Table definitions and column hints from SCHEMAFILE, keyed by lowercase table name."""
    try:
        with open(SCHEMAFILE, "r") as f:
            tables = json.load(f)["tables"]
    except (OSError, ValueError, KeyError) as e:
        raise GufiError(f"Could not load schema file {SCHEMAFILE}: {e}") from e
    return {
        name.lower(): {"columns": {col.lower(): info for col, info in table.get("columns", {}).items()}}
        for name, table in tables.items()
    }


def schema_columns(table: str) -> set[str]:
    """Columns an agent may reference in table: its visible columns plus the server's path columns."""
    schema = get_static_schema()
    if table not in schema:
        raise GufiError(f"Table {table!r} is not described in the schema file")
    return (set(schema[table]["columns"]) - HIDDEN_COLUMNS) | set(path_columns(table))


#
# Path columns
#

# GUFI can only build a path while it is inside the directory being processed:
# rpath(sname, sroll[, name]) joins that directory with sname and sroll, which are
# not paths on their own. The server hides them and offers path columns instead,
# in the form GUFI's command-line tools print: the index name followed by the path
# inside the index, i.e. relative to GUFI_INDEX_ROOT.
HIDDEN_COLUMNS = frozenset({"sname", "sroll", "srollsubdirs"})
PATH_FUNCTIONS = frozenset({"rpath", "spath", "path", "epath", "fpath"})
ENTRY_PATH_TABLES = frozenset({"vrpentries"})
DIRECTORY_PATH_TABLES = frozenset({"vrsummary", "vrsummarylong"})
PATH_TABLES_TEXT = "vrpentries (path, dirpath) and vrsummary (path)"
PATH_FORM = "the index name followed by the path inside the index, the same form gufi_find prints and the gufi_* tools' path arguments accept"


def path_columns(table: str) -> dict[str, str]:
    """{column: hint} of the path columns the server adds to table."""
    if table in ENTRY_PATH_TABLES:
        columns = {
            "path": f"Full path of the file or link, e.g. myindex/dir/file.txt: {PATH_FORM}.",
            "dirpath": "Full path of the directory containing the file or link, e.g. myindex/dir.",
        }
        kind = "file or link"
    elif table in DIRECTORY_PATH_TABLES:
        columns = {"path": f"Full path of the directory, e.g. myindex/dir (the index's top directory is myindex): {PATH_FORM}."}
        kind = "directory"
    else:
        return {}
    if GUFI_SOURCE_ROOT is not None:
        columns["spath"] = (f"Path of the {kind} on the source file system that was indexed, "
                            f"e.g. {GUFI_SOURCE_ROOT}/myindex/dir.")
    return columns


def _index_relative(absolute_sql: str, label: str, root: str) -> str:
    """SQL turning an absolute path under root into label + the path below root."""
    return f"'{label}' || substr({absolute_sql}, {len(root) + 1})"


def _source_prefix(label: str) -> str:
    return f"{GUFI_SOURCE_ROOT}/{label}"


def per_directory_path_sql(table: str, column: str, label: str, root: str) -> str:
    """Path column of table computed with GUFI's path functions, for gufi_query per-directory SQL."""
    entry = table in ENTRY_PATH_TABLES
    if column == "spath":
        return "spath(sname, sroll, name)" if entry else "spath(sname, sroll)"
    if column == "path" and entry:
        return _index_relative("rpath(sname, sroll, name)", label, root)
    return _index_relative("rpath(sname, sroll)", label, root)


def gufi_vt_path_sql(table: str, column: str, label: str, root: str, name_sql: str) -> str:
    """Path column of table computed from a gufi_vt_* row, whose rpath column is rpath(sname, sroll)."""
    prefix = _source_prefix(label) if column == "spath" else label
    directory = _index_relative('"rpath"', prefix, root)
    if table in ENTRY_PATH_TABLES and column != "dirpath":
        return f"{directory} || '/' || {name_sql}"
    return directory


def expand_path_columns(sql: str, context: str, column_sql) -> str:
    """Replace each table reference that needs path columns with a derived table providing them.

    Only the table name is spliced, so the rest of the agent's SQL reaches GUFI unchanged.
    column_sql(table, column) returns the SQL for one path column.
    """
    tree = parse_select(sql, context)
    used = {column.name.lower() for column in tree.find_all(exp.Column)}
    edits = []
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        wanted = [column for column in path_columns(name) if column in used]
        if not wanted:
            continue
        projection = ", ".join(f"{column_sql(name, column)} AS {column}" for column in wanted)
        derived = f"(SELECT {projection}, * FROM {name})"
        if not table.alias:
            derived += f" AS {table.name}"
        meta = table.this.meta
        edits.append((meta["start"], meta["end"] + 1, derived))
    for start, end, text in sorted(edits, reverse=True):
        sql = sql[:start] + text + sql[end:]
    return sql


def load_schema_hints(name: str, schema: list[list[Any]]) -> dict[str, dict[str, str]]:
    """Attach static hints to (column, type) rows from PRAGMA table_info."""
    hints = get_static_schema().get(name.lower(), {}).get("columns", {})
    described = {}
    for column, col_type in schema:
        hint = hints.get(column.lower(), {})
        described[column] = {
            "type": col_type or hint.get("type", ""),
            "hint": hint.get("hint", ""),
        }
    return described


#
# SQL validation
#

def parse_select(sql: str, context: str) -> exp.Select:
    """Parse sql as exactly one plain SELECT statement, or raise GufiError."""
    if not isinstance(sql, str) or not sql.strip():
        raise GufiError(f"{context}: SQL is empty")
    if "\0" in sql:
        raise GufiError(f"{context}: SQL contains a NUL character")
    try:
        statements = [s for s in sqlglot.parse(sql, read=SQL_DIALECT) if s is not None]
    except SqlglotError as e:
        raise GufiError(f"{context}: SQL could not be parsed: {e}") from e
    if len(statements) != 1:
        raise GufiError(f"{context}: expected exactly one SQL statement, found {len(statements)}")
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        raise GufiError(f"{context}: only a single SELECT statement is allowed (got {tree.key.upper()})")
    if tree.find(exp.With):
        raise GufiError(f"{context}: WITH clauses are not supported")
    if tree.find(exp.Placeholder):
        raise GufiError(f"{context}: bind parameters (?) are not supported")
    return tree


def select_tables(tree: exp.Select, context: str) -> list[str]:
    """Lowercase names of every table referenced anywhere in the statement."""
    names = []
    for table in tree.find_all(exp.Table):
        if table.args.get("db") or table.args.get("catalog"):
            raise GufiError(f"{context}: schema-qualified table {table.sql(dialect=SQL_DIALECT)} is not allowed")
        if not table.name:
            raise GufiError(f"{context}: table-valued functions are not allowed in FROM")
        names.append(table.name.lower())
    return sorted(set(names))


def select_columns(tree: exp.Select) -> set[str]:
    """Lowercase column names the statement reads.

    Outside the SELECT list (e.g. ORDER BY, HAVING) a name may refer to an alias
    instead of a column, so alias names are only excluded there.
    """
    aliases = {alias.alias.lower() for alias in tree.find_all(exp.Alias)}
    projected = {id(col) for select in tree.selects for col in select.find_all(exp.Column)}
    return {
        col.name.lower() for col in tree.find_all(exp.Column)
        if id(col) in projected or col.name.lower() not in aliases
    }


def _single_table(tree: exp.Select, context: str) -> str:
    tables = select_tables(tree, context)
    if len(tables) != 1:
        found = ", ".join(tables) or "none"
        raise GufiError(f"{context}: SQL must read from exactly one table (found: {found})")
    return tables[0]


PATH_COLUMN_NAMES = frozenset({"path", "dirpath", "spath"})


def _check_columns(tree: exp.Select, allowed: set[str], table: str, context: str, path_hint: str = "") -> None:
    unknown = sorted(select_columns(tree) - allowed)
    if unknown:
        hint = f". {path_hint}" if path_hint and PATH_COLUMN_NAMES & set(unknown) else ""
        raise GufiError(f"{context}: column(s) {', '.join(unknown)} do not exist in {table}{hint}")


TABLE_PATH_HINT = f"Path columns exist only in {PATH_TABLES_TEXT}"
CARRY_PATH_HINT = (f"Path columns come from {PATH_TABLES_TEXT} in per_directory_sql; select them there and declare them "
                   "as TEXT in per_directory_shape (and partial_shape) to use them in later stages")
USE_PATH_COLUMNS = f"Use the path columns instead: {PATH_TABLES_TEXT}"


def _reject_path_internals(tree: exp.Expression, context: str) -> None:
    """sname/sroll are not paths and GUFI's path functions return index-internal forms; both are hidden."""
    for column in tree.find_all(exp.Column):
        if column.name.lower() in HIDDEN_COLUMNS:
            raise GufiError(f"{context}: {column.name} is internal rollup bookkeeping, not a path, and is not "
                            f"available. {USE_PATH_COLUMNS}")
    for call in tree.find_all(exp.Anonymous):
        if call.name.lower() in PATH_FUNCTIONS:
            raise GufiError(f"{context}: {call.name.lower()}() is not available. {USE_PATH_COLUMNS}")


def _reject_ordering(tree: exp.Expression, context: str) -> None:
    """ORDER BY, LIMIT and OFFSET anywhere in tree (subqueries and window clauses included)."""
    for node_type, keyword in ((exp.Order, "ORDER BY"), (exp.Limit, "LIMIT"), (exp.Offset, "OFFSET")):
        if tree.find(node_type) is not None:
            raise GufiError(
                f"{context}: {keyword} is only allowed in final_sql. Per-directory and partial SQL run once per "
                "directory or thread, so ordering or limiting there drops or reorders partial results; "
                "move it to final_sql"
            )


def _check_projection_width(tree: exp.Select, expected: int, target: str, context: str) -> None:
    if tree.is_star:
        raise GufiError(f"{context}: SELECT * is not allowed here; list the {expected} column(s) of {target} explicitly")
    if len(tree.selects) != expected:
        raise GufiError(f"{context}: SELECT returns {len(tree.selects)} column(s) but {target} has {expected}")


_SHAPE_COLUMN_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s+(INT64|INTEGER|INT|BIGINT|REAL|FLOAT|DOUBLE|NUMERIC|TEXT|BLOB)",
    re.IGNORECASE,
)


def parse_shape(shape: str, context: str) -> dict[str, str]:
    """Parse 'name TYPE, name TYPE' into an ordered {name: TYPE} map."""
    columns: dict[str, str] = {}
    for part in shape.split(","):
        match = _SHAPE_COLUMN_RE.fullmatch(part.strip())
        if not match:
            raise GufiError(
                f"{context}: {part.strip()!r} is not a 'name TYPE' column definition "
                "(TYPE is one of INT64, INTEGER, INT, BIGINT, REAL, FLOAT, DOUBLE, NUMERIC, TEXT, BLOB)"
            )
        name, col_type = match.group(1).lower(), match.group(2).upper()
        if name in columns:
            raise GufiError(f"{context}: column {name!r} is defined more than once")
        columns[name] = col_type
    return columns


@contextmanager
def _as_value_error() -> Iterator[None]:
    """Re-raise GufiError as ValueError so pydantic reports it as a validation error."""
    try:
        yield
    except GufiError as e:
        raise ValueError(str(e)) from e


def _phase_for_table(table: str) -> str:
    for phase, tables in PHASE_TABLES.items():
        if table in tables:
            return phase
    allowed = sorted(TREE_TABLES | SUMMARY_TABLES | ENTRIES_TABLES)
    raise GufiError(f"Table {table!r} cannot be used in per_directory_sql. Allowed tables: {', '.join(allowed)}")


#
# Aggregate query model
#

class GufiAggregateQuery(BaseModel):
    """An index-wide GUFI aggregate pipeline (gufi_query -I, -T/-S/-E, -K, -J, -G)."""

    model_config = ConfigDict(extra="forbid")

    index: str = Field(
        description=(
            "Name of the index to query, exactly as listed by the gufi://indexes resource. "
            "per_directory_sql that reads treesummary tables requires an index whose gufi://indexes entry has treesummary true."
        ),
    )

    per_directory_sql: list[str] = Field(
        min_length=1,
        max_length=3,
        description=(
            "Per-directory stage: one to three SELECT statements that GUFI runs in parallel threads, once per directory; "
            "each result row is inserted into that thread's per_directory table. Each statement reads exactly one "
            "table, which decides its phase: treesummary/vtsummary* (once per tree), summary/vsummary*/vrsummary* "
            "(once per directory), or entries/pentries/vrpentries (the directory's files). At most one statement per "
            "phase. Each must return exactly the columns of per_directory_shape, in order. ORDER BY, LIMIT and OFFSET are "
            "rejected anywhere here (subqueries and windows included): they would apply per directory only. "
            f"Full paths are the path columns of {PATH_TABLES_TEXT}; this is the only stage that can read them. "
            "Read gufi://schemas/all for table names and gufi://schemas/<table> for column names before writing it. "
            "Example: SELECT gid, size FROM vrpentries WHERE type = 'l'"
        ),
    )

    per_directory_shape: str = Field(
        description=(
            "Columns of the per_directory table that per_directory_sql rows are inserted into, as comma-separated 'name TYPE' "
            "pairs (TYPE: INT64, INTEGER, INT, BIGINT, REAL, FLOAT, DOUBLE, NUMERIC, TEXT, BLOB). "
            "Example: 'gid INT64, size INT64'"
        ),
    )

    partial_sql: str = Field(
        description=(
            "Partial stage: SELECT run against each thread's per_directory table; its rows are inserted into the shared "
            "partial table. Must read only from per_directory and return exactly the columns of partial_shape, in order. "
            "It runs once per thread, so the partial table holds one partial result per thread. ORDER BY, LIMIT "
            "and OFFSET are rejected anywhere here; they belong in final_sql. "
            "Example: SELECT gid, COUNT(*), SUM(size) FROM per_directory GROUP BY gid"
        ),
    )

    partial_shape: str = Field(
        description=(
            "Columns of the partial table, as comma-separated 'name TYPE' pairs. "
            "Example: 'gid INT64, links INT64, bytes INT64'"
        ),
    )

    final_sql: str = Field(
        description=(
            "SELECT run once against the partial table to produce the answer. Must read only from partial. "
            "Combine the per-thread partial results here (SUM of counts and sums, MAX of maxima), and put global "
            "ORDER BY and LIMIT here; this is the only stage where they are allowed. "
            "Example: SELECT gid, SUM(links) AS links, SUM(bytes) AS bytes FROM partial GROUP BY gid ORDER BY links DESC"
        ),
    )

    _phases: dict[str, str] = PrivateAttr(default_factory=dict)

    @field_validator("index")
    @classmethod
    def index_exists(cls, index: str) -> str:
        with _as_value_error():
            resolve_local_index(index)
        return index

    @field_validator("per_directory_shape", "partial_shape")
    @classmethod
    def shape_valid(cls, shape: str, info) -> str:
        with _as_value_error():
            columns = parse_shape(shape, info.field_name)
            hidden = sorted(HIDDEN_COLUMNS & set(columns))
            if hidden:
                raise GufiError(f"{info.field_name}: {', '.join(hidden)} is internal rollup bookkeeping, not a path. "
                                f"Carry a path column as TEXT instead ({PATH_TABLES_TEXT})")
        return ", ".join(f"{name} {col_type}" for name, col_type in columns.items())

    @field_validator("per_directory_sql")
    @classmethod
    def per_directory_statements_valid(cls, statements: list[str]) -> list[str]:
        cleaned = []
        with _as_value_error():
            for i, sql in enumerate(statements):
                context = f"per_directory_sql[{i}]"
                sql = _strip_terminators(sql)
                tree = parse_select(sql, context)
                _reject_double_quotes(sql, context)
                _reject_ordering(tree, context)
                _reject_path_internals(tree, context)
                cleaned.append(sql)
        return cleaned

    @field_validator("partial_sql", "final_sql")
    @classmethod
    def statement_valid(cls, sql: str, info) -> str:
        sql = _strip_terminators(sql)
        with _as_value_error():
            tree = parse_select(sql, info.field_name)
            _reject_double_quotes(sql, info.field_name)
            if info.field_name == "partial_sql":
                _reject_ordering(tree, info.field_name)
            _reject_path_internals(tree, info.field_name)
        return sql

    @model_validator(mode="after")
    def pipeline_consistent(self) -> "GufiAggregateQuery":
        """Check that every stage reads tables and columns that the previous stage provides."""
        with _as_value_error():
            per_directory_cols = parse_shape(self.per_directory_shape, "per_directory_shape")
            partial_cols = parse_shape(self.partial_shape, "partial_shape")

            phases: dict[str, str] = {}
            for i, sql in enumerate(self.per_directory_sql):
                context = f"per_directory_sql[{i}]"
                tree = parse_select(sql, context)
                table = _single_table(tree, context)
                phase = _phase_for_table(table)
                if phase in phases:
                    raise GufiError(
                        f"{context}: more than one per_directory_sql statement reads a {phase}-phase table; "
                        "combine them into one statement"
                    )
                _check_columns(tree, schema_columns(table), table, context, TABLE_PATH_HINT)
                _check_projection_width(tree, len(per_directory_cols), "per_directory_shape", context)
                if phase == "T" and not has_treesummary(self.index):
                    raise GufiError(f"{context}: index {self.index!r} has no treesummary table; use summary or entries tables")
                phases[phase] = sql

            gather = parse_select(self.partial_sql, "partial_sql")
            if select_tables(gather, "partial_sql") != [PER_DIRECTORY_TABLE]:
                raise GufiError("partial_sql must read only from the per_directory table")
            _check_columns(gather, set(per_directory_cols), "per_directory (per_directory_shape)", "partial_sql", CARRY_PATH_HINT)
            _check_projection_width(gather, len(partial_cols), "partial_shape", "partial_sql")

            final = parse_select(self.final_sql, "final_sql")
            if select_tables(final, "final_sql") != [PARTIAL_TABLE]:
                raise GufiError("final_sql must read only from the partial table")
            _check_columns(final, set(partial_cols), "partial (partial_shape)", "final_sql", CARRY_PATH_HINT)

        self._phases = phases
        return self

    def build_query_string(self) -> str:
        """CREATE VIRTUAL TABLE statement that runs this pipeline through gufi_vt.

        The virtual table yields the partial table's rows; final_sql is applied
        on top of it by final_query_string(). gufi_vt only knows the types of plain
        columns, so running final_sql inside gufi_vt would return computed values
        such as SUM(...) as text.
        """
        partial_columns = ", ".join(parse_shape(self.partial_shape, "partial_shape"))
        root = str(resolve_local_index(self.index))
        options = [f'index="{root}"', f"threads={THREADS}"]
        if GUFI_SOURCE_ROOT is not None:
            options.append(f'p="{_source_prefix(self.index)}"')
        options.append(_gufi_vt_arg("I", f"CREATE TABLE {PER_DIRECTORY_TABLE}({self.per_directory_shape})"))
        for phase in ("T", "S", "E"):
            if phase in self._phases:
                sql = expand_path_columns(self._phases[phase], f"per_directory {phase}",
                                          lambda table, column: per_directory_path_sql(table, column, self.index, root))
                options.append(_gufi_vt_arg(phase, f"INSERT INTO {PER_DIRECTORY_TABLE} {sql}"))
        options.extend([
            _gufi_vt_arg("K", f"CREATE TABLE {PARTIAL_TABLE}({self.partial_shape})"),
            _gufi_vt_arg("J", f"INSERT INTO {PARTIAL_TABLE} {self.partial_sql}"),
            _gufi_vt_arg("G", f"SELECT {partial_columns} FROM {PARTIAL_TABLE}"),
        ])
        return f"CREATE VIRTUAL TABLE {AGGREGATE_VTAB} USING gufi_vt({', '.join(options)})"

    def final_query_string(self) -> str:
        """final_sql run over the virtual table's rows, materialized as the partial table.

        Materializing first also avoids the gufi_vt hang when final_sql stops reading early.
        """
        return f"WITH {PARTIAL_TABLE} AS MATERIALIZED (SELECT * FROM {AGGREGATE_VTAB}) {self.final_sql}"


def _strip_terminators(sql: str) -> str:
    return sql.strip().rstrip(";").strip() if isinstance(sql, str) else sql


def _reject_double_quotes(sql: str, context: str) -> None:
    # gufi_vt module arguments are wrapped in double quotes and cannot contain them.
    if '"' in sql:
        raise GufiError(f"{context}: double quotes are not allowed; use single quotes for string literals")


def _gufi_vt_arg(key: str, sql: str) -> str:
    # The trailing ';' keeps gufi_vt from stripping a closing single quote off the SQL.
    return f'{key}="{sql};"'


#
# SQL execution
#

def _connect_gufi_vt() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    try:
        conn.enable_load_extension(True)
        conn.load_extension(GUFIVTLIB)
    except (sqlite3.Error, AttributeError) as e:
        conn.close()
        raise GufiError(f"Could not load the gufi_vt extension from {GUFIVTLIB}: {e}") from e
    finally:
        try:
            conn.enable_load_extension(False)
        except (sqlite3.Error, AttributeError):
            pass
    return conn


def _fetch(conn: sqlite3.Connection, sql: str, params: tuple = (), drain: bool = False) -> GufiQueryResult:
    """Execute sql and return at most MAX_ROWS rows.

    drain reads every row before truncating; gufi_vt cursors must be drained
    because closing one early leaves gufi_query blocked on a full pipe.
    """
    logger.debug("Executing SQL: %s params=%r", sql, params)
    try:
        cursor = conn.execute(sql, params)
        columns = [desc[0] for desc in cursor.description or []]
        fetched = cursor.fetchall() if drain else cursor.fetchmany(MAX_ROWS + 1)
        rows = [list(row) for row in fetched]
    except sqlite3.Error as e:
        raise GufiError(f"SQLite error: {e}") from e
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    return GufiQueryResult(columns=columns, rows=rows, row_count=len(rows), truncated=truncated)


def run_db_sql(db_path: Path, sql: str, params: tuple = ()) -> GufiQueryResult:
    """Run one statement against a single database file, opened read-only."""
    conn = _connect_readonly(db_path)
    try:
        return _fetch(conn, sql, params)
    finally:
        conn.close()


# gufi_vt_* emit SELECT path(), epath(), fpath(), rpath(), level(), * FROM <view> and store the
# values by position under a schema compiled into the extension. Some GUFI builds declare a
# schema that differs from the view (gufi_vt_vrsummary lacks crtime, shifting every later
# column by one), so a column is read from the declared name at its view position.
GUFI_VT_EXTRA_COLUMNS = ("path", "epath", "fpath", "rpath", "level")
_GUFI_VT_COLUMN_MAPS: dict[str, Optional[dict[str, str]]] = {}


def _gufi_vt_column_map(vt: str) -> Optional[dict[str, str]]:
    """{view column: declared gufi_vt column holding it}, or None when the two schemas agree."""
    if vt not in _GUFI_VT_COLUMN_MAPS:
        try:
            conn = _connect_gufi_vt()
        except GufiError:
            return None
        try:
            declared = [row[1] for row in conn.execute(f"PRAGMA table_xinfo(gufi_vt_{vt})") if not row[6]]
        finally:
            conn.close()
        view = list(get_static_schema()[vt]["columns"])
        stored = declared[len(GUFI_VT_EXTRA_COLUMNS):]
        _GUFI_VT_COLUMN_MAPS[vt] = None if stored == view else dict(zip(view, stored))
        if _GUFI_VT_COLUMN_MAPS[vt] is not None:
            logger.warning("gufi_vt_%s declares a schema that differs from the %s view; remapping columns", vt, vt)
    return _GUFI_VT_COLUMN_MAPS[vt]


def _gufi_vt_stored(vt: str, column: str) -> str:
    """Name of the gufi_vt_<vt> column that holds the view's column."""
    mapping = _gufi_vt_column_map(vt)
    if mapping is None:
        return column
    if column not in mapping:
        raise GufiError(f"sql_file_index: column {column} cannot be read through the installed gufi_vt_{vt}; "
                        "use aggregate_sql_query instead")
    return mapping[column]


def _gufi_vt_source(vt: str, column: str) -> str:
    stored = _gufi_vt_stored(vt, column)
    return f'"{column}"' if stored == column else f'"{stored}" AS "{column}"'


def build_gufi_vt_select(sql: str, index: str, remote: bool = False) -> tuple[str, tuple]:
    """Rewrite a SELECT over one GUFI table to read from the matching gufi_vt_* function.

    Returns the rewritten SQL and the parameters to bind for its index arguments.
    """
    context = "sql_file_index"
    placeholder = "__gufi_mcp_source__"
    if placeholder in sql:
        raise GufiError(f"{context}: SQL must not contain {placeholder}")

    tree = parse_select(_strip_terminators(sql), context)
    table_name = _single_table(tree, context)
    sources = list(tree.find_all(exp.Table))
    if len(sources) != 1:
        raise GufiError(f"{context}: the table may only be referenced once (no subqueries or self-joins); "
                        "use aggregate_sql_query for multi-step queries")
    table = table_name.removeprefix("gufi_vt_")
    if table not in GUFI_VT_TABLES:
        raise GufiError(f"{context}: table {table_name!r} is not queryable. Use one of: {', '.join(sorted(GUFI_VT_TABLES))}")
    _reject_path_internals(tree, context)
    _check_columns(tree, schema_columns(table), table, context, TABLE_PATH_HINT)

    # gufi_vt_* positional arguments: index, threads, min_level, max_level, path_list, verbose[, remote_cmd, remote_args]
    if remote:
        root = resolve_remote_index(index)
        args, params = "?, ?, NULL, NULL, NULL, 0, 'ssh', ?", (root, THREADS, REMOTEHOST)
    else:
        root = str(resolve_local_index(index))
        args, params = "?, ?, NULL, NULL, NULL, 0", (root, THREADS)
    label = root.rsplit("/", 1)[-1]

    vt = GUFI_VT_TABLES[table]
    paths = path_columns(table)
    mapping = _gufi_vt_column_map(vt)
    if tree.is_star:
        wanted = [col for col in get_static_schema()[vt]["columns"]
                  if col not in HIDDEN_COLUMNS and (mapping is None or col in mapping)] + list(paths)
    else:
        # Columns are already checked against the schema; any column works when none are referenced.
        wanted = sorted(select_columns(tree)) or ["name"]
    name_sql = f'"{_gufi_vt_stored(vt, "name")}"' if paths else ""
    source_columns = ", ".join(
        f'{gufi_vt_path_sql(table, col, label, root, name_sql)} AS "{col}"' if col in paths else _gufi_vt_source(vt, col)
        for col in wanted
    )

    source = sources[0]
    alias = source.alias or table
    replacement = exp.to_table(placeholder)
    replacement.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
    source.replace(replacement)

    # gufi_vt hangs if SQLite stops reading before gufi_query finishes writing (e.g. a
    # bare LIMIT), so the source is always read in full into a materialized CTE first.
    rewritten = (
        f"WITH {placeholder} AS MATERIALIZED (SELECT {source_columns} FROM gufi_vt_{vt}({args})) "
        + tree.sql(dialect=SQL_DIALECT)
    )
    return rewritten, params


_GUFI_VT_LOCK = threading.Lock()


def _gufi_query_children() -> list[int]:
    """PIDs of gufi_query processes started directly by this process (by gufi_vt)."""
    pids = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            comm_and_rest = stat.read_text()
        except OSError:
            continue
        comm = comm_and_rest[comm_and_rest.find("(") + 1:comm_and_rest.rfind(")")]
        fields = comm_and_rest[comm_and_rest.rfind(")") + 2:].split()
        if comm == "gufi_query" and fields and int(fields[1]) == os.getpid():
            pids.append(int(stat.parent.name))
    return pids


@contextmanager
def _gufi_vt_watchdog() -> Iterator[None]:
    """Run one gufi_vt query at a time and kill its gufi_query if it exceeds TOOL_TIMEOUT.

    gufi_vt can block forever waiting on gufi_query. Killing the child unblocks it,
    but whatever rows were read are incomplete, so a timeout is always an error.
    Serializing queries ensures only the timed-out query's child is killed.
    """
    timed_out = threading.Event()

    def kill_children() -> None:
        timed_out.set()
        for pid in _gufi_query_children():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    timeout_error = GufiError(f"gufi_vt query did not finish within {TOOL_TIMEOUT} seconds and was stopped")
    with _GUFI_VT_LOCK:
        timer = threading.Timer(TOOL_TIMEOUT, kill_children)
        timer.daemon = True
        timer.start()
        try:
            yield
        except GufiError:
            if timed_out.is_set():
                raise timeout_error from None
            raise
        finally:
            timer.cancel()
        if timed_out.is_set():
            raise timeout_error


def run_gufi_vt_select(sql: str, params: tuple) -> GufiQueryResult:
    with _gufi_vt_watchdog():
        conn = _connect_gufi_vt()
        try:
            return _fetch(conn, sql, params, drain=True)
        finally:
            conn.close()


def run_aggregate(query: GufiAggregateQuery) -> GufiQueryResult:
    create = query.build_query_string()
    with _gufi_vt_watchdog():
        conn = _connect_gufi_vt()
        try:
            logger.debug("Creating aggregate table: %s", create)
            try:
                conn.execute(create)
            except sqlite3.Error as e:
                raise GufiError(f"gufi_vt rejected the aggregate pipeline: {e}") from e
            return _fetch(conn, query.final_query_string(), drain=True)
        finally:
            conn.close()


#
# Command-line tools
#

def _check_cli_path(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("path must not be empty")
    if value.startswith("-"):
        raise ValueError(f"path {value!r} must not start with '-'")
    if any(ch in value for ch in "\0\n\r"):
        raise ValueError("path must not contain NUL or newline characters")
    return value


CliPath = Annotated[str, AfterValidator(_check_cli_path)]
"""A path relative to the GUFI index root (or absolute within it), safe to pass as an argv element."""

_FIND_NUMBER_RE = re.compile(r"[+-]?\d+")
_FIND_SIZE_RE = re.compile(r"[+-]?\d+[bcwkMG]?")


def _check_find_number(value: Any) -> str:
    value = str(value).strip()
    if not _FIND_NUMBER_RE.fullmatch(value):
        raise ValueError("must be an integer, optionally prefixed with + (more than) or - (less than)")
    return value


def _check_find_size(value: str) -> str:
    value = value.strip()
    if not _FIND_SIZE_RE.fullmatch(value):
        raise ValueError("must be an integer with optional +/- prefix and unit suffix b, c, w, k, M, or G (e.g. +10M)")
    return value


FindNumber = Annotated[str, BeforeValidator(_check_find_number)]
FindSize = Annotated[str, AfterValidator(_check_find_size)]


def _cli_value(value: Any) -> str:
    return str(value.value if isinstance(value, Enum) else value)


def build_tool_command(binary: str, args: BaseModel, flags: dict[str, str],
                       leading: list[str] = (), trailing: list[str] = ()) -> list[str]:
    """Turn a validated tool model into argv.

    Booleans become bare flags, None and False are omitted, lists repeat the flag,
    and other values are passed as '--flag=value' so values beginning with '-'
    are not mistaken for options.
    """
    cmd = [binary, *leading]
    for field, flag in flags.items():
        value = getattr(args, field)
        if value is None or value is False:
            continue
        if value is True:
            cmd.append(flag)
            continue
        if len(flag) <= 2:
            raise GufiError(f"{binary}: valued option {field} must map to a long flag")
        items = value if isinstance(value, list) else [value]
        cmd.extend(f"{flag}={_cli_value(item)}" for item in items)
    cmd.extend(trailing)
    return cmd


def relative_index_paths(text: str) -> str:
    """Rewrite absolute paths under GUFI_INDEX_ROOT into the index-name-first form.

    Most GUFI tools print that form already; gufi_stat and gufi_du --summarize print absolute paths.
    """
    prefix = re.escape(f"{str(GUFI_INDEX_ROOT).rstrip('/')}/")
    return re.sub(rf"(?<![^\s'\"]){prefix}", "", text)


def cli_index_root() -> Optional[Path]:
    """IndexRoot from the server config the installed GUFI command-line tools read, if it can be found."""
    gufi_find = shutil.which("gufi_find")
    if gufi_find is None:
        return None
    config_module = Path(gufi_find).resolve().parent.parent / "lib" / "gufi_config.py"
    try:
        match = re.search(r"^PATH = '([^']+)'", config_module.read_text(), re.MULTILINE)
        config = Path(match.group(1)).read_text() if match else ""
    except OSError:
        return None
    root = re.search(r"^IndexRoot=(.+)$", config, re.MULTILINE)
    return Path(root.group(1).strip()).resolve() if root else None


def run_gufi_tool(cmd: list[str], delim: Optional[str] = "\t") -> GufiToolResult:
    """Run a GUFI command-line tool and split its stdout into rows."""
    logger.debug("Running %s", cmd)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TOOL_TIMEOUT, check=False)
    except FileNotFoundError as e:
        raise GufiError(f"{cmd[0]} was not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise GufiError(f"{cmd[0]} timed out after {TOOL_TIMEOUT} seconds") from e

    stderr = proc.stderr.strip()
    if proc.returncode != 0:
        raise GufiError(f"{cmd[0]} exited with status {proc.returncode}: {stderr or 'no error output'}")

    lines = [line for line in relative_index_paths(proc.stdout).splitlines() if line.strip()]
    truncated = len(lines) > MAX_ROWS
    lines = lines[:MAX_ROWS]
    rows = [line.split(delim) if delim else [line] for line in lines]
    return GufiToolResult(command=cmd, rows=rows, row_count=len(rows), truncated=truncated, stderr=stderr)


def run_gufi_text(cmd: list[str]) -> str:
    """Run a GUFI command and return its stdout as text."""
    return "\n".join(row[0] for row in run_gufi_tool(cmd, delim=None).rows)


#
# Tool argument models
#

class GufiToolArgsLS(BaseModel):
    """GUFI equivalent of ls: List directory contents."""

    model_config = ConfigDict(extra="forbid")

    path: CliPath = Field(
        default=".",
        description="Directory to list, relative to the GUFI index root. Paths start with an index name from gufi://indexes, e.g. 'myindex' or 'myindex/sub'.",
    )

    all: bool = Field(default=False, description="If true, include entries starting with '.' (ls -a).")
    almost_all: bool = Field(default=False, description="If true, include entries starting with '.' except '.' and '..' (ls -A).")
    ignore_backups: bool = Field(default=False, description="If true, exclude entries ending with ~.")
    full_time: bool = Field(default=False, description="If true, list entries with full time, like 'ls -l --time-style=full-iso'.")
    no_group: bool = Field(default=False, description="If true, do not print group names in long listings.")
    human_readable: bool = Field(default=False, description="If true, print sizes with suffixes like K, M, G.")
    inode: bool = Field(default=False, description="If true, print the inode number of each file.")
    list_format: bool = Field(default=False, description="If true, use long listing format (ls -l).")
    numeric_uid_gid: bool = Field(default=False, description="If true, long listing with numeric UID and GID.")
    reverse: bool = Field(default=False, description="If true, reverse the sort order.")
    recursive: bool = Field(default=False, description="If true, list subdirectories recursively.")
    size: bool = Field(default=False, description="If true, print the allocated size of each file in blocks.")
    sort_size: bool = Field(default=False, description="If true, sort by file size, largest first.")
    sort_time: bool = Field(default=False, description="If true, sort by modification time, newest first.")

    block_size: Optional[int] = Field(
        default=None, ge=1, le=1048576,
        description="With list_format, scale sizes by this many bytes when printing.",
    )

    time_style: Optional[Literal["full-iso", "iso", "locale", "long-iso"]] = Field(
        default=None,
        description="Time/date format used with list_format.",
    )


args_ls_dict = {
    "all": "-a",
    "almost_all": "-A",
    "ignore_backups": "-B",
    "full_time": "--full-time",
    "no_group": "--no-group",
    "human_readable": "-h",
    "inode": "-i",
    "list_format": "-l",
    "numeric_uid_gid": "-n",
    "reverse": "-r",
    "recursive": "-R",
    "size": "-s",
    "sort_size": "-S",
    "sort_time": "-t",
    "block_size": "--block-size",
    "time_style": "--time-style",
}


class GufiToolArgsDU(BaseModel):
    """GUFI equivalent of du: Summarize disk usage."""

    model_config = ConfigDict(extra="forbid")

    paths: list[CliPath] = Field(
        default_factory=lambda: ["."],
        min_length=1,
        description=(
            "One or more paths to summarize, relative to the GUFI index root. Paths start with an index name from gufi://indexes, e.g. 'myindex' or 'myindex/sub'. "
            "Directories need treesummary data (gufi://indexes shows which indexes have it)."
        ),
    )

    apparent_size: bool = Field(
        default=False,
        description="If true, print apparent sizes rather than disk usage (may differ for sparse or fragmented files).",
    )
    block_size: Optional[int] = Field(default=None, ge=1, le=1048576, description="Scale reported sizes by this block size.")
    total: bool = Field(default=False, description="If true, produce a grand total across all paths.")
    human_readable: bool = Field(default=False, description="If true, print sizes in human-readable form (e.g. 1K, 234M, 2G).")
    inodes: bool = Field(default=False, description="If true, report inode usage instead of block usage.")
    separate_dirs: bool = Field(default=False, description="If true, do not include the size of subdirectories.")
    summarize: bool = Field(default=False, description="If true, display only a total for each path.")
    verbose: bool = Field(default=False, description="If true, report the gufi_query being executed in stderr.")


args_du_dict = {
    "apparent_size": "--apparent-size",
    "block_size": "--block-size",
    "total": "--total",
    "human_readable": "--human-readable",
    "inodes": "--inodes",
    "separate_dirs": "--separate-dirs",
    "summarize": "--summarize",
    "verbose": "--verbose",
}


class GufiToolArgsStat(BaseModel):
    """GUFI equivalent of stat: Display file status."""

    model_config = ConfigDict(extra="forbid")

    files: list[CliPath] = Field(
        default_factory=lambda: ["."],
        min_length=1,
        description="Files or directories to stat, relative to the GUFI index root. Paths start with an index name from gufi://indexes, e.g. 'myindex' or 'myindex/sub'.",
    )

    format: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Optional printf-style format string; output follows this format instead of the default.",
    )
    terse: bool = Field(default=False, description="If true, print information in terse form.")
    verbose: bool = Field(default=False, description="If true, report the command being run in stderr.")


args_stat_dict = {
    "format": "--format",
    "terse": "--terse",
    "verbose": "--verbose",
}


class GufiStatsStatistic(str, Enum):
    DEPTH = "depth"
    FILESIZE = "filesize"
    FILECOUNT = "filecount"
    LINKCOUNT = "linkcount"
    DIRCOUNT = "dircount"
    LEAF_DIRS = "leaf-dirs"
    LEAF_DEPTH = "leaf-depth"
    LEAF_FILES = "leaf-files"
    LEAF_LINKS = "leaf-links"
    TOTAL_FILESIZE = "total-filesize"
    TOTAL_FILECOUNT = "total-filecount"
    TOTAL_LINKCOUNT = "total-linkcount"
    TOTAL_DIRCOUNT = "total-dircount"
    TOTAL_LEAF_FILES = "total-leaf-files"
    TOTAL_LEAF_LINKS = "total-leaf-links"
    FILES_PER_LEVEL = "files-per-level"
    LINKS_PER_LEVEL = "links-per-level"
    DIRS_PER_LEVEL = "dirs-per-level"
    EXTENSIONS = "extensions"
    EXTENSIONS_HIST = "extensions-hist"
    FILESIZE_LOG2_BINS = "filesize-log2-bins"
    FILESIZE_LOG1024_BINS = "filesize-log1024-bins"
    FILESIZE_LOG2_BINS_HIST = "filesize-log2-bins-hist"
    FILESIZE_LOG1024_BINS_HIST = "filesize-log1024-bins-hist"
    DIRFILECOUNT_LOG2_BINS = "dirfilecount-log2-bins"
    DIRFILECOUNT_LOG1024_BINS = "dirfilecount-log1024-bins"
    DIRFILECOUNT_LOG2_BINS_HIST = "dirfilecount-log2-bins-hist"
    DIRFILECOUNT_LOG1024_BINS_HIST = "dirfilecount-log1024-bins-hist"
    FILE_AGE_ATIME = "file-age-atime"
    FILE_AGE_MTIME = "file-age-mtime"
    FILE_AGE_CTIME = "file-age-ctime"
    DIR_AGE_ATIME = "dir-age-atime"
    DIR_AGE_MTIME = "dir-age-mtime"
    DIR_AGE_CTIME = "dir-age-ctime"
    FILE_AGE_ATIME_HIST = "file-age-atime-hist"
    FILE_AGE_MTIME_HIST = "file-age-mtime-hist"
    FILE_AGE_CTIME_HIST = "file-age-ctime-hist"
    DIR_AGE_ATIME_HIST = "dir-age-atime-hist"
    DIR_AGE_MTIME_HIST = "dir-age-mtime-hist"
    DIR_AGE_CTIME_HIST = "dir-age-ctime-hist"
    UNIQUE_FILE_UID_COUNT = "unique-file-uid-count"
    UNIQUE_FILE_GID_COUNT = "unique-file-gid-count"
    UNIQUE_LINK_UID_COUNT = "unique-link-uid-count"
    UNIQUE_LINK_GID_COUNT = "unique-link-gid-count"
    UNIQUE_DIR_UID_COUNT = "unique-dir-uid-count"
    UNIQUE_DIR_GID_COUNT = "unique-dir-gid-count"
    AVERAGE_LEAF_FILES = "average-leaf-files"
    AVERAGE_LEAF_LINKS = "average-leaf-links"
    AVERAGE_LEAF_SIZE = "average-leaf-size"
    MEDIAN_LEAF_FILES = "median-leaf-files"
    MEDIAN_LEAF_LINKS = "median-leaf-links"
    MEDIAN_LEAF_SIZE = "median-leaf-size"
    DUPLICATE_NAMES = "duplicate-names"
    UID_SIZE = "uid-size"
    GID_SIZE = "gid-size"


class GufiToolArgsStats(BaseModel):
    """GUFI equivalent of gufi_stats: Index-level filesystem statistics."""

    model_config = ConfigDict(extra="forbid")

    statistic: GufiStatsStatistic = Field(
        default=GufiStatsStatistic.TOTAL_FILECOUNT,
        description="Which statistic to compute.",
    )

    path: CliPath = Field(
        default=".",
        description="Path to compute the statistic for, relative to the GUFI index root. Paths start with an index name from gufi://indexes, e.g. 'myindex' or 'myindex/sub'.",
    )

    recursive: bool = Field(
        default=False,
        description="If true, run recursively for statistics that support it. Mutually exclusive with cumulative.",
    )
    cumulative: bool = Field(
        default=False,
        description="If true, return cumulative values for statistics that support it. Mutually exclusive with recursive.",
    )
    order: Optional[Literal["ASC", "DESC", "least", "most"]] = Field(
        default=None,
        description="Sort order, for statistics that produce ordered output.",
    )
    num_results: Optional[int] = Field(default=None, ge=1, description="Return only the first n results when applicable.")
    uid: Optional[str] = Field(default=None, min_length=1, description="Restrict results to this user name or numeric UID.")
    reftime: Optional[str] = Field(default=None, min_length=1, description="Reference time used when comparing timestamps.")
    verbose: bool = Field(default=False, description="If true, report the gufi_query being executed in stderr.")

    @model_validator(mode="after")
    def recursive_and_cumulative_exclusive(self) -> "GufiToolArgsStats":
        if self.recursive and self.cumulative:
            raise ValueError("recursive and cumulative cannot both be true")
        return self


args_stats_dict = {
    "recursive": "--recursive",
    "cumulative": "--cumulative",
    "order": "--order",
    "num_results": "--num-results",
    "uid": "--uid",
    "reftime": "--reftime",
    "verbose": "--verbose",
}


class GufiToolArgsGetfattr(BaseModel):
    """GUFI equivalent of getfattr: Read extended attributes from indexed paths."""

    model_config = ConfigDict(extra="forbid")

    paths: list[CliPath] = Field(
        default_factory=lambda: ["."],
        min_length=1,
        description="Paths to read extended attributes from, relative to the GUFI index root. Paths start with an index name from gufi://indexes, e.g. 'myindex' or 'myindex/sub'.",
    )

    name: Optional[str] = Field(default=None, min_length=1, description="Dump the value of this named extended attribute only.")
    dump: bool = Field(default=False, description="If true, dump all extended attributes on each path.")
    match: str = Field(
        default=r"^user\.",
        min_length=1,
        description="Regular expression limiting which attribute names are included. Use '-' to include all attributes.",
    )
    only_values: bool = Field(default=False, description="If true, dump raw extended attribute values without encoding.")
    recursive: bool = Field(default=False, description="If true, list attributes recursively for all files and directories.")
    verbose: bool = Field(default=False, description="If true, report the gufi_query being executed in stderr.")

    @model_validator(mode="after")
    def name_or_dump_not_both(self) -> "GufiToolArgsGetfattr":
        if self.name and self.dump:
            raise ValueError("specify either name or dump, not both")
        return self


args_getfattr_dict = {
    "name": "--name",
    "dump": "--dump",
    "match": "--match",
    "only_values": "--only-values",
    "recursive": "--recursive",
    "verbose": "--verbose",
}


class GufiToolArgsFind(BaseModel):
    """GUFI equivalent of find: Search indexed directory trees."""

    model_config = ConfigDict(extra="forbid")

    paths: list[CliPath] = Field(
        default_factory=lambda: ["."],
        min_length=1,
        description="Starting paths for the search, relative to the GUFI index root. Paths start with an index name from gufi://indexes, e.g. 'myindex' or 'myindex/sub'.",
    )

    maxdepth: Optional[int] = Field(default=None, ge=0, description="Descend at most this many directory levels below each starting path.")
    mindepth: Optional[int] = Field(default=None, ge=0, description="Do not apply tests or actions above this depth.")

    amin: Optional[FindNumber] = Field(default=None, description="Last accessed n minutes ago; +n for more than, -n for less than.")
    atime: Optional[FindNumber] = Field(default=None, description="Last accessed n*24 hours ago; +n for more than, -n for less than.")
    cmin: Optional[FindNumber] = Field(default=None, description="Status last changed n minutes ago; +n / -n for more / less than.")
    ctime: Optional[FindNumber] = Field(default=None, description="Status last changed n*24 hours ago; +n / -n for more / less than.")
    mmin: Optional[FindNumber] = Field(default=None, description="Data last modified n minutes ago; +n / -n for more / less than.")
    mtime: Optional[FindNumber] = Field(default=None, description="Data last modified n*24 hours ago; +n / -n for more / less than.")

    anewer: Optional[CliPath] = Field(default=None, description="Accessed more recently than this reference path was modified.")
    cnewer: Optional[CliPath] = Field(default=None, description="Status changed more recently than this reference path was modified.")
    newer: Optional[CliPath] = Field(default=None, description="Modified more recently than this reference path.")
    samefile: Optional[CliPath] = Field(default=None, description="Refers to the same inode as this path.")

    empty: bool = Field(default=False, description="If true, match empty regular files or directories.")
    executable: bool = Field(default=False, description="If true, match executable files and searchable directories.")
    readable: bool = Field(default=False, description="If true, match readable files.")
    writable: bool = Field(default=False, description="If true, match writable files.")
    false: bool = Field(default=False, description="If true, never match.")
    true: bool = Field(default=False, description="If true, always match.")

    gid: Optional[int] = Field(default=None, ge=0, description="Numeric group ID must equal n.")
    group: Optional[str] = Field(default=None, min_length=1, description="Owned by this group name (numeric GID allowed).")
    uid: Optional[int] = Field(default=None, ge=0, description="Numeric user ID must equal n.")
    user: Optional[str] = Field(default=None, min_length=1, description="Owned by this user name (numeric UID allowed).")
    inum: Optional[int] = Field(default=None, ge=0, description="Inode number; prefer samefile when comparing paths.")
    links: Optional[int] = Field(default=None, ge=0, description="Has exactly n hard links.")

    name: Optional[str] = Field(default=None, min_length=1, description="Base name matches this shell pattern.")
    iname: Optional[str] = Field(default=None, min_length=1, description="Base name matches this case-insensitive regex.")
    path_pattern: Optional[str] = Field(default=None, min_length=1, description="Full path matches this shell pattern.")
    regex: Optional[str] = Field(default=None, min_length=1, description="Full path matches this regular expression.")
    iregex: Optional[str] = Field(default=None, min_length=1, description="Full path matches this case-insensitive regular expression.")
    lname: Optional[str] = Field(default=None, min_length=1, description="Symbolic link contents match this shell pattern.")

    perm: Optional[str] = Field(default=None, description="Permission bits as octal mode, optionally prefixed with - or /.")
    size: Optional[FindSize] = Field(default=None, description="Size predicate, e.g. '+10M' (more than 10 MiB) or '-2k' (less than 2 KiB).")
    file_type: Optional[list[Literal["b", "c", "d", "p", "f", "l", "s"]]] = Field(
        default=None,
        min_length=1,
        description="Restrict to file type(s): b block, c char, d directory, p fifo, f regular, l symlink, s socket.",
    )

    ls: bool = Field(default=False, description="List each match in ls -dils format. Mutually exclusive with print and printf.")
    print: bool = Field(default=False, description="Print the full path of each match (the default action). Mutually exclusive with ls and printf.")
    printf: Optional[str] = Field(default=None, min_length=1, description="Print each match using a GNU find printf format. Mutually exclusive with ls and print.")

    num_results: Optional[int] = Field(default=None, ge=1, description="Return only the first n matches.")
    smallest: bool = Field(default=False, description="Sort output by size ascending. Mutually exclusive with largest.")
    largest: bool = Field(default=False, description="Sort output by size descending. Mutually exclusive with smallest.")
    compress: bool = Field(default=False, description="If true, reduce memory use via zlib compression when available.")
    verbose: bool = Field(default=False, description="If true, report the gufi_query being executed in stderr.")

    @field_validator("perm")
    @classmethod
    def perm_looks_like_mode(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if not re.fullmatch(r"[-/]?[0-7]{3,4}", value):
            raise ValueError("perm must be 3 or 4 octal digits, optionally prefixed with - or /")
        return value

    @model_validator(mode="after")
    def options_consistent(self) -> "GufiToolArgsFind":
        if self.maxdepth is not None and self.mindepth is not None and self.mindepth > self.maxdepth:
            raise ValueError("mindepth cannot be greater than maxdepth")
        if self.smallest and self.largest:
            raise ValueError("smallest and largest cannot both be true")
        if self.false and self.true:
            raise ValueError("false and true cannot both be true")
        actions = [name for name in ("ls", "print", "printf") if getattr(self, name)]
        if len(actions) > 1:
            raise ValueError(f"choose one action only; multiple set: {', '.join(actions)}")
        return self


args_find_dict = {
    "maxdepth": "-maxdepth",
    "mindepth": "-mindepth",
    "amin": "-amin",
    "anewer": "-anewer",
    "atime": "-atime",
    "cmin": "-cmin",
    "cnewer": "-cnewer",
    "ctime": "-ctime",
    "empty": "-empty",
    "executable": "-executable",
    "false": "-false",
    "gid": "-gid",
    "group": "-group",
    "iname": "-iname",
    "inum": "-inum",
    "iregex": "-iregex",
    "links": "-links",
    "lname": "-lname",
    "mmin": "-mmin",
    "mtime": "-mtime",
    "name": "-name",
    "newer": "-newer",
    "path_pattern": "-path",
    "perm": "-perm",
    "readable": "-readable",
    "regex": "-regex",
    "samefile": "-samefile",
    "size": "-size",
    "true": "-true",
    "file_type": "-type",
    "uid": "-uid",
    "user": "-user",
    "writable": "-writable",
    "ls": "-ls",
    "print": "-print",
    "printf": "-printf",
    "num_results": "--num-results",
    "smallest": "--smallest",
    "largest": "--largest",
    "compress": "--compress",
    "verbose": "-V",
}
