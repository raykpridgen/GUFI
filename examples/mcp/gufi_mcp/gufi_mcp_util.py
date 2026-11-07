"""Setup, schema description, and the aggregate query pipeline for the GUFI MCP server.

Every function that can fail for a reason the agent should see raises
``GufiError``. It subclasses MCP's ``ToolError``, so when it escapes a tool the
agent receives the message instead of a generic "Error executing tool".
"""

import json
import logging
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from functools import lru_cache, wraps
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Optional

import sqlglot
from dotenv import load_dotenv
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator
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
    if any(ch in value for ch in "'\"\0$`\\"):
        raise RuntimeError("GUFI_SOURCE_ROOT must not contain quote, NUL, $, ` or \\ characters")
    return value.rstrip("/")


# With remote_cmd, gufi_vt joins the gufi_query command into one string for the
# remote shell: SQL and -p sit inside double quotes and index paths are unquoted.
_REMOTE_UNSAFE_SQL = re.compile(r"[$`\\]")
_REMOTE_SAFE_PATH = re.compile(r"/[A-Za-z0-9._+/-]*")


def _optional_remote() -> tuple[Optional[str], list[str]]:
    """remote_cmd and remote_arg values for gufi_vt, from GUFI_REMOTE_CMD and GUFI_REMOTE_ARGS."""
    command = os.getenv("GUFI_REMOTE_CMD", "").strip()
    args = shlex.split(os.getenv("GUFI_REMOTE_ARGS", ""))
    if not command:
        if args:
            raise RuntimeError("GUFI_REMOTE_ARGS is set but GUFI_REMOTE_CMD is not")
        return None, []
    if any(ch in value for value in (command, *args) for ch in "'\"\0"):
        raise RuntimeError("GUFI_REMOTE_CMD and GUFI_REMOTE_ARGS must not contain quote or NUL characters")
    if Path(command).name == "ssh":
        # gufi_vt does not redirect the child's stdin, so ssh would read the MCP stdio
        # stream, and the server cannot answer password or host key prompts.
        if "-n" not in args:
            args.insert(0, "-n")
        if not any(arg.startswith("BatchMode") or arg.startswith("-oBatchMode") for arg in args):
            args[1:1] = ["-o", "BatchMode=yes"]
    return command, args


def _index_root(remote: bool) -> PurePosixPath:
    value = _require_env("GUFI_INDEX_ROOT")
    if not remote:
        return Path(value).resolve()
    if not _REMOTE_SAFE_PATH.fullmatch(value):
        raise RuntimeError("With GUFI_REMOTE_CMD, GUFI_INDEX_ROOT must be an absolute path on the remote host "
                           "made of letters, digits, and . _ + - / only")
    return PurePosixPath(value)


SCHEMAFILE = _resolve_relative_to_module(_require_env("SCHEMAFILE"))
GUFIVTLIB = _require_env("GUFIVTLIB")
# Command and arguments that run gufi_query on another host, e.g. ssh and user@host.
# Unset, gufi_query runs on this host.
REMOTE_CMD, REMOTE_ARGS = _optional_remote()
# Parent of the indexes, on the host that runs gufi_query.
GUFI_INDEX_ROOT = _index_root(REMOTE_CMD is not None)
# Parent of the indexed source trees, as GUFI_INDEX_ROOT is the parent of the indexes:
# index <GUFI_INDEX_ROOT>/<name> was built from <GUFI_SOURCE_ROOT>/<name>. Enables spath.
GUFI_SOURCE_ROOT = _optional_source_root()
THREADS = int(os.getenv("GUFI_MCP_THREADS", "0")) or os.cpu_count() or 1
TOOL_TIMEOUT = int(os.getenv("GUFI_MCP_TOOL_TIMEOUT", "300"))
MAX_ROWS = int(os.getenv("GUFI_MCP_MAX_ROWS", "5000"))
# Seconds that index lists, treesummary checks, and schema metadata are reused; 0 disables.
CACHE_TTL = float(os.getenv("GUFI_MCP_CACHE_TTL", "300"))

SQL_DIALECT = "sqlite"

# Tables each per-directory phase may read from. gufi_query runs T against
# treesummary, S against each directory's summary, and E against its entries.
TREE_TABLES = frozenset({"treesummary", "vtsummarydir", "vtsummaryuser", "vtsummarygroup"})
SUMMARY_TABLES = frozenset({"summary", "summarylong", "vsummarydir", "vsummaryuser",
                            "vsummarygroup", "vrsummary", "vrsummarylong"})
ENTRIES_TABLES = frozenset({"entries", "pentries", "pentries_rollup", "vrpentries"})
PHASE_TABLES = {"T": TREE_TABLES, "S": SUMMARY_TABLES, "E": ENTRIES_TABLES}

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


#
# GUFI installation
#

def _where() -> str:
    return f"on the remote host ({REMOTE_CMD} {' '.join(REMOTE_ARGS)})" if REMOTE_CMD else "on this host"


def _run_command(argv: list[str], what: str) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=TOOL_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as e:
        raise GufiError(f"{what} timed out after {TOOL_TIMEOUT} seconds") from e
    except OSError as e:
        raise GufiError(f"{what} could not be started: {e}") from e
    if proc.returncode != 0:
        raise GufiError(f"{what} exited with status {proc.returncode} {_where()}: "
                        f"{proc.stderr.strip() or 'no error output'}")
    return proc.stdout.strip()


def gufi_query_path() -> str:
    """Absolute path of the gufi_query binary that gufi_vt runs, on this host or the remote host."""
    if REMOTE_CMD:
        location = _run_command([REMOTE_CMD, *REMOTE_ARGS, "command -v gufi_query"], "command -v gufi_query")
    else:
        location = shutil.which("gufi_query") or ""
    if not location:
        raise GufiError(f"gufi_query was not found on PATH {_where()}")
    return location


def gufi_query_version() -> str:
    """Output of gufi_query --version, on this host or the remote host."""
    if REMOTE_CMD:
        return _run_command([REMOTE_CMD, *REMOTE_ARGS, "gufi_query --version"], "gufi_query --version")
    return _run_command([gufi_query_path(), "--version"], "gufi_query --version")


#
# Index and schema helpers
#
# Everything that reads an index goes through gufi_vt, so the same code serves
# local indexes and, with GUFI_REMOTE_CMD, indexes on another host. gufi_query
# attaches each directory's db.db as schema "tree".

_REMOTE_SAFE_NAME = re.compile(r"[A-Za-z0-9._+-]+")
_CACHES: list = []


def _ttl_cache(func):
    """Reuse results for CACHE_TTL seconds, keyed by the arguments. Errors are not cached."""
    entries: dict[tuple, tuple[float, Any]] = {}
    lock = threading.Lock()

    @wraps(func)
    def wrapper(*args):
        now = time.monotonic()
        with lock:
            hit = entries.get(args)
        if hit is not None and now - hit[0] < CACHE_TTL:
            return hit[1]
        value = func(*args)
        with lock:
            entries[args] = (now, value)
        return value

    wrapper.cache_clear = entries.clear
    _CACHES.append(wrapper)
    return wrapper


def clear_caches() -> None:
    for cached in _CACHES:
        cached.cache_clear()


def _introspect(root: str, shape: str, sql: str, min_level: int = 0, max_level: int = 0) -> list[list[Any]]:
    """Rows of one summary-phase SELECT that gufi_query runs in every directory from min_level to max_level under root."""
    columns = ", ".join(parse_shape(shape, "introspection shape"))
    options = gufi_vt_options(root, threads=1, min_level=min_level, max_level=max_level)
    create = pipeline_statement(options, {"S": sql}, shape, f"SELECT {columns} FROM {PER_DIRECTORY_TABLE}", shape)
    return run_gufi_vt(create, f"SELECT * FROM {AGGREGATE_VTAB}").rows


@_ttl_cache
def _index_names(root: str) -> tuple[str, ...]:
    if not REMOTE_CMD and not Path(root).is_dir():
        raise GufiError(f"GUFI index root {root} does not exist or is not a directory")
    # gufi_query skips directories without a db.db, so every row is an index.
    names = sorted(PurePosixPath(row[0]).name for row in _introspect(root, "path TEXT", "SELECT path()", 1, 1))
    if REMOTE_CMD:
        unsafe = [name for name in names if not _REMOTE_SAFE_NAME.fullmatch(name)]
        if unsafe:
            logger.warning("Skipping remote indexes whose names cannot be passed through the remote shell: %s", unsafe)
        names = [name for name in names if name not in unsafe]
    return tuple(names)


def get_gufi_indexes() -> list[str]:
    """Names of the indexes directly under GUFI_INDEX_ROOT (directories containing db.db)."""
    return list(_index_names(str(GUFI_INDEX_ROOT)))


def resolve_index(index: str) -> PurePosixPath:
    """Resolve an index name from gufi_indexes to its absolute path on the host that runs gufi_query."""
    indexes = get_gufi_indexes()
    if index not in indexes:
        raise GufiError(f"Unknown index {index!r}. Available indexes: {', '.join(indexes) or 'none'}")
    path = GUFI_INDEX_ROOT / index
    # gufi_vt strips every leading/trailing quote from its arguments and does not
    # unescape doubled quotes, so a path containing quotes cannot be passed safely.
    if any(ch in str(path) for ch in "'\"\0"):
        raise GufiError(f"Index path {path} contains quote characters, which gufi_vt cannot accept")
    return path


@_ttl_cache
def _has_treesummary(root: str) -> bool:
    rows = _introspect(root, "found INT64",
                       "SELECT COUNT(*) FROM tree.sqlite_master WHERE type = 'table' AND name = 'treesummary'")
    return bool(rows and rows[0][0])


def has_treesummary(index: str) -> bool:
    """True if the top-level database of the index has a treesummary table."""
    return _has_treesummary(str(resolve_index(index)))


@_ttl_cache
def _schema_objects(root: str) -> tuple[tuple[str, str], ...]:
    rows = _introspect(root, "name TEXT, type TEXT",
                       "SELECT name, type FROM tree.sqlite_master WHERE sql IS NOT NULL AND type IN ('table', 'view')")
    by_name = sorted((name, kind) for name, kind in rows)
    return tuple(sorted(by_name, key=lambda row: row[1], reverse=True))


def schema_root() -> str:
    """Index used to describe the GUFI schema: GUFI_INDEX_ROOT if it has a db.db, else the first index."""
    root = str(GUFI_INDEX_ROOT)
    if _schema_objects(root):
        return root
    indexes = get_gufi_indexes()
    if not indexes:
        raise GufiError(f"No GUFI indexes found under {GUFI_INDEX_ROOT} {_where()}")
    return str(GUFI_INDEX_ROOT / indexes[0])


@_ttl_cache
def _table_info(root: str, name: str) -> tuple[tuple[str, str], ...]:
    # name is checked against the schema's own object names before it is spliced in.
    rows = _introspect(root, "cid INT64, name TEXT, type TEXT",
                       f"SELECT cid, name, type FROM pragma_table_info('{name}', 'tree')")
    return tuple((column, col_type) for _, column, col_type in sorted(rows))


def list_schema_objects() -> dict[str, str]:
    """Map of table/view name to type ('table' or 'view') in the GUFI schema."""
    return dict(_schema_objects(schema_root()))


def table_columns(name: str) -> list[list[str]]:
    """[column, type] rows of one table or view, in column order."""
    return [list(row) for row in _table_info(schema_root(), name)]


def describe_schema_object(name: str) -> dict[str, dict[str, str]]:
    """Columns of one table or view, with types and hints from the schema file."""
    objects = list_schema_objects()
    if name not in objects:
        raise GufiError(f"Unknown table or view {name!r}. Read gufi_schemas/all for valid names")
    rows = table_columns(name)
    paths = {column: {"type": "TEXT", "hint": hint} for column, hint in path_columns(name.lower()).items()}
    described = {**paths, **load_schema_hints(name, [row for row in rows if row[0].lower() not in HIDDEN_COLUMNS])}
    for column, info in described.items():
        tags = functions_for_column(column)
        if tags:
            hint = info["hint"].rstrip()
            if hint and not hint.endswith("."):
                hint += "."
            info["hint"] = f"{hint} Functions: {'; '.join(tags)}.".lstrip()
    return described


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
PATH_FORM = "the index name followed by the path inside the index"


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
    hints = path_column_hints().get(table, {})
    return {column: hints.get(column) or hint for column, hint in columns.items()}


@lru_cache(maxsize=1)
def path_column_hints() -> dict[str, dict[str, str]]:
    """{table: {path column: hint}} from SCHEMAFILE's path_columns section.

    Kept apart from "tables" because those column lists must match the views' real columns.
    """
    try:
        with open(SCHEMAFILE, "r") as f:
            section = json.load(f).get("path_columns", {})
    except (OSError, ValueError) as e:
        raise GufiError(f"Could not load schema file {SCHEMAFILE}: {e}") from e
    return {table.lower(): {col.lower(): hint for col, hint in cols.items()} for table, cols in section.items()}


#
# GUFI SQL functions
#

# gufi_query registers these in every stage (per-directory, per-thread, and the
# aggregate database). strop, intop, and blobop run shell commands through popen(3).
BLOCKED_FUNCTIONS = frozenset({"strop", "intop", "blobop"})


@lru_cache(maxsize=1)
def function_catalog() -> dict[str, dict[str, Any]]:
    """{function: entry} from SCHEMAFILE's functions section."""
    try:
        with open(SCHEMAFILE, "r") as f:
            section = json.load(f).get("functions", {})
    except (OSError, ValueError) as e:
        raise GufiError(f"Could not load schema file {SCHEMAFILE}: {e}") from e
    return {name.lower(): entry for name, entry in section.items() if name.lower() not in BLOCKED_FUNCTIONS}


@lru_cache(maxsize=1)
def _column_tags() -> dict[str, list[str]]:
    """{column: [tag template, ...]} for advertised functions, in catalog order, duplicates dropped."""
    tags: dict[str, list[str]] = {}
    for entry in function_catalog().values():
        if entry.get("advertise", True) and entry.get("tag"):
            for column in entry.get("columns", []):
                found = tags.setdefault(column.lower(), [])
                if entry["tag"] not in found:
                    found.append(entry["tag"])
    return tags


def functions_for_column(column: str) -> list[str]:
    """Short function tags for column, e.g. ['bytecat(size) -> size band (...)']."""
    return [tag.replace("{col}", column) for tag in _column_tags().get(column.lower(), [])]


def list_functions() -> dict[str, str]:
    """{function: signature} of every advertised GUFI SQL function."""
    return {name: entry["signature"] for name, entry in function_catalog().items() if entry.get("advertise", True)}


def describe_function(name: str) -> dict[str, Any]:
    """Signature, description, applicable columns, and an example for one GUFI SQL function."""
    key = name.lower()
    if key in BLOCKED_FUNCTIONS:
        raise GufiError(f"{key}() runs shell commands and is not available")
    entry = function_catalog().get(key)
    if entry is None:
        raise GufiError(f"Unknown function {name!r}. Read gufi://functions for the list")
    return {field: entry[field] for field in ("signature", "detail", "columns", "example") if entry.get(field)}


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


def _reject_blocked_functions(tree: exp.Expression, context: str) -> None:
    for call in tree.find_all(exp.Anonymous):
        if call.name.lower() in BLOCKED_FUNCTIONS:
            raise GufiError(f"{context}: {call.name.lower()}() runs shell commands and is not available")


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
            "Name of the index to query, exactly as listed by the gufi_indexes resource. "
            "per_directory_sql that reads treesummary tables requires an index whose gufi_indexes entry has treesummary true."
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
            "Read gufi_schemas/all for table names and gufi_schemas/<table> for column names before writing it. "
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
            resolve_index(index)
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
                _reject_unsafe_characters(sql, context)
                _reject_ordering(tree, context)
                _reject_path_internals(tree, context)
                _reject_blocked_functions(tree, context)
                cleaned.append(sql)
        return cleaned

    @field_validator("partial_sql", "final_sql")
    @classmethod
    def statement_valid(cls, sql: str, info) -> str:
        sql = _strip_terminators(sql)
        with _as_value_error():
            tree = parse_select(sql, info.field_name)
            _reject_unsafe_characters(sql, info.field_name)
            if info.field_name == "partial_sql":
                _reject_ordering(tree, info.field_name)
            _reject_path_internals(tree, info.field_name)
            _reject_blocked_functions(tree, info.field_name)
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
        root = str(resolve_index(self.index))
        source_prefix = _source_prefix(self.index) if GUFI_SOURCE_ROOT is not None else None
        phases = {
            phase: expand_path_columns(sql, f"per_directory {phase}",
                                       lambda table, column: per_directory_path_sql(table, column, self.index, root))
            for phase, sql in self._phases.items()
        }
        return pipeline_statement(gufi_vt_options(root, source_prefix=source_prefix), phases,
                                  self.per_directory_shape, self.partial_sql, self.partial_shape)

    def final_query_string(self) -> str:
        """final_sql run over the virtual table's rows, materialized as the partial table.

        Materializing first also avoids the gufi_vt hang when final_sql stops reading early.
        """
        return f"WITH {PARTIAL_TABLE} AS MATERIALIZED (SELECT * FROM {AGGREGATE_VTAB}) {self.final_sql}"


def _strip_terminators(sql: str) -> str:
    return sql.strip().rstrip(";").strip() if isinstance(sql, str) else sql


def _reject_unsafe_characters(sql: str, context: str) -> None:
    # gufi_vt module arguments are wrapped in double quotes and cannot contain them.
    if '"' in sql:
        raise GufiError(f"{context}: double quotes are not allowed; use single quotes for string literals")
    if REMOTE_CMD:
        found = sorted(set(_REMOTE_UNSAFE_SQL.findall(sql)))
        if found:
            raise GufiError(f"{context}: {' '.join(found)} cannot be sent to the remote GUFI host; "
                            "rewrite the SQL without them (for example with char() for a literal character)")


def _gufi_vt_arg(key: str, sql: str) -> str:
    # The trailing ';' keeps gufi_vt from stripping a closing single quote off the SQL.
    return f'{key}="{sql};"'


def gufi_vt_options(root: str, threads: Optional[int] = None, min_level: Optional[int] = None,
                    max_level: Optional[int] = None, source_prefix: Optional[str] = None) -> list[str]:
    """gufi_vt arguments that select the index, the walk, and where gufi_query runs."""
    options = [f'index="{root}"', f"threads={threads or THREADS}"]
    if min_level is not None:
        options.append(f"min_level={min_level}")
    if max_level is not None:
        options.append(f"max_level={max_level}")
    if source_prefix is not None:
        options.append(f'p="{source_prefix}"')
    if REMOTE_CMD:
        options.append(f'remote_cmd="{REMOTE_CMD}"')
        options.extend(f'remote_arg="{arg}"' for arg in REMOTE_ARGS)
    return options


def pipeline_statement(options: list[str], phases: dict[str, str], per_directory_shape: str,
                       partial_sql: str, partial_shape: str) -> str:
    """CREATE VIRTUAL TABLE statement for a gufi_query -I, -T/-S/-E, -K, -J, -G pipeline.

    The virtual table yields the partial table's rows; gufi_vt takes their types
    from partial_shape.
    """
    partial_columns = ", ".join(parse_shape(partial_shape, "partial_shape"))
    options = [*options, _gufi_vt_arg("I", f"CREATE TABLE {PER_DIRECTORY_TABLE}({per_directory_shape})")]
    for phase in ("T", "S", "E"):
        if phase in phases:
            options.append(_gufi_vt_arg(phase, f"INSERT INTO {PER_DIRECTORY_TABLE} {phases[phase]}"))
    options.extend([
        _gufi_vt_arg("K", f"CREATE TABLE {PARTIAL_TABLE}({partial_shape})"),
        _gufi_vt_arg("J", f"INSERT INTO {PARTIAL_TABLE} {partial_sql}"),
        _gufi_vt_arg("G", f"SELECT {partial_columns} FROM {PARTIAL_TABLE}"),
    ])
    return f"CREATE VIRTUAL TABLE {AGGREGATE_VTAB} USING gufi_vt({', '.join(options)})"


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


_GUFI_VT_LOCK = threading.Lock()


def _gufi_query_children() -> list[int]:
    """PIDs of gufi_query (or remote command) processes started directly by this process (by gufi_vt)."""
    # /proc/<pid>/stat holds at most 15 characters of the command name.
    names = {"gufi_query"} | ({Path(REMOTE_CMD).name[:15]} if REMOTE_CMD else set())
    pids = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            comm_and_rest = stat.read_text()
        except OSError:
            continue
        comm = comm_and_rest[comm_and_rest.find("(") + 1:comm_and_rest.rfind(")")]
        fields = comm_and_rest[comm_and_rest.rfind(")") + 2:].split()
        if comm in names and fields and int(fields[1]) == os.getpid():
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


def run_gufi_vt(create: str, select: str, what: str = "query") -> GufiQueryResult:
    """Create the gufi_vt table with create, then return the rows of select over it.

    gufi_vt runs gufi_query here or, with GUFI_REMOTE_CMD, on the remote host.
    Callers must resolve indexes before calling: queries hold a non-reentrant lock.
    """
    with _gufi_vt_watchdog():
        conn = _connect_gufi_vt()
        try:
            logger.debug("Creating gufi_vt table: %s", create)
            try:
                conn.execute(create)
            except sqlite3.Error as e:
                raise GufiError(f"gufi_vt rejected the {what}: {e}") from e
            try:
                return _fetch(conn, select, drain=True)
            except GufiError as e:
                if not REMOTE_CMD:
                    raise
                # A failed connection reaches SQLite only as a generic read error.
                raise GufiError(f"{e} (gufi_query runs {_where()}; check that the connection works "
                                "and that gufi_query is on the remote PATH)") from e
        finally:
            conn.close()


def run_aggregate(query: GufiAggregateQuery) -> GufiQueryResult:
    return run_gufi_vt(query.build_query_string(), query.final_query_string(), "aggregate pipeline")
