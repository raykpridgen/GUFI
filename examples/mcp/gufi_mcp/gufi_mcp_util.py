import sqlite3
import sys
from pathlib import Path
from typing import Any, Literal
from sqlglot import parse_one, ParseError, exp
import os
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import re
import subprocess

load_dotenv()

SCHEMAFILE = os.getenv('SCHEMAFILE')
REMOTEHOST = os.getenv('REMOTEHOST')
MCPTRANSPORT = os.getenv('MCPTRANSPORT')
MCPSRVHOST = os.getenv('MCPSRVHOST')
MCPSRVPORT = os.getenv('MCPSRVPORT')
GUFIVTLIB = os.getenv('GUFIVTLIB')
GUFI_INDEX_ROOT = os.getenv('GUFI_INDEX_ROOT')
GUFI_QUERY = os.getenv('GUFI_QUERY')
from enum import Enum

class GufiSQLOption(str, Enum):
    I = "-I"
    T = "-T"
    S = "-S"
    E = "-E"
    K = "-K"
    J = "-J"
    G = "-G"
    F = "-F"

# Object for GUFI options
class GufiOption(BaseModel):
    option: GufiSQLOption
    sql: str

# Object to handle query construction
class GufiQuery(BaseModel):
    index: str
    sql_options: list[GufiOption] = Field(default_factory=list)
    config: list[str] = Field(default_factory=list)
    delimiter: str = "\t"

# Object to handle query returns
class GufiQueryResult(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    error: str | None = None
    sql: str | None = None
    compiled_sql: str | None = None
    warning: str | None = None
    execution_mode: Literal["shard_local", "aggregate"] = "shard_local"
    requested_limit: int | None = None

# Object for tool results
class GufiToolResult(BaseModel):
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    stderr: str | None = None
    returncode: int | None = None


def parse_delimited_stdout(stdout: str, delimiter: str = "\t") -> list[list[str]]:
    ''' Parse delimiter-separated command stdout into structured rows. '''
    text = stdout.strip()
    if not text:
        return []
    return [line.split(delimiter) for line in text.splitlines()]


def pack_command_result(result: subprocess.CompletedProcess, delimiter: str = "\t") -> dict[str, Any]:
    ''' Build a tool result from a subprocess, surfacing stderr when stdout is empty. '''
    rows = parse_delimited_stdout(result.stdout, delimiter)
    stderr = result.stderr.strip() if result.stderr and result.stderr.strip() else None
    if not rows and stderr:
        rows = [[line] for line in stderr.splitlines()]
    tool_result = GufiToolResult(
        rows=rows,
        row_count=len(rows),
        stderr=stderr,
        returncode=result.returncode,
    )
    return tool_result.model_dump()

def is_valid_sql_query(sql_query: str, dialect: str = "sqlite") -> bool:
    try:
        parse_one(sql_query, read=dialect)
        return True
    except ParseError as e:
        print(f"SQL Grammar Error: {str(e)}", file=sys.stderr)
        return False

def table_exists(index: str, table_name: str) -> bool:
    ''' Confirm existence of a table based on an index and table name '''

    conn = sqlite3.connect(f'{GUFI_INDEX_ROOT}{index}/db.db')
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
            AND name = ?
        """, (table_name,))

        return cursor.fetchone() is not None

    finally:
        conn.close()

def get_columns_from_sqlin(sqlin) -> list[str]:
    ''' Separate out column names into a list '''
    col_string = re.match(r'(?i)SELECT\s+(.+?)\s+FROM', sqlin, flags=re.IGNORECASE | re.DOTALL).group(1)
    return [col.strip() for col in col_string.split(',')]

def sqlite_string(value: str, quote: str = "'") -> str:
    ''' Quote a string literal for SQLite SQL text.

    gufi_vt module arguments are embedded in a CREATE VIRTUAL TABLE statement,
    so quotes inside each argument need to be escaped before SQLite parses it.
    '''
    return f"{quote}{value.replace(quote, quote * 2)}{quote}"

def ensure_sql_statement(sql: str) -> str:
    ''' Make a gufi_query SQL fragment look like a full SQL statement.

    GUFI examples pass -I/-E/-K/-J/-G fragments with trailing semicolons; this
    preserves caller-provided semicolons and adds one when the caller omitted it.
    '''
    sql = sql.strip()
    return sql if sql.endswith(";") else f"{sql};"

def resolve_query_index(index: str) -> str:
    ''' Resolve an MCP index name into the path gufi_vt should receive.

    Absolute paths are passed through for advanced callers. Relative names are
    resolved under GUFI_INDEX_ROOT, matching the other local MCP query helpers.
    '''
    if os.path.isabs(index):
        return index
    return os.path.join(GUFI_INDEX_ROOT, index)

def rewrite_gufi_vt_from(sql: str, vt_args: str) -> str:
    ''' Rewrite logical GUFI table names into executable gufi_vt calls.

    Models see schemas for raw GUFI tables such as pentries and vrpentries, but
    SQLite execution needs the gufi_vt_* table-valued function with index args.
    This narrow regex only rewrites whitelisted FROM table tokens.
    '''
    tables = "treesummary|summary|entries|pentries|vrsummary|vrpentries"
    pattern = rf"(?i)\bFROM\s+(?:gufi_vt_)?({tables})\b(?!\s*\()"

    def replace(match: re.Match) -> str:
        table = match.group(1)
        return f"FROM gufi_vt_{table}({vt_args})"

    return re.sub(pattern, replace, sql, count=1)

def gufi_query_stage_from_sql(sql: str) -> str | None:
    ''' Pick the gufi_query SQL phase needed for a logical SELECT.

    The generic gufi_vt table maps SQL text to gufi_query flags. Entry-like
    tables run through -E, summary-like tables through -S, and treesummary
    through -T.
    '''
    match = re.search(
        r"(?i)\bFROM\s+(?:gufi_vt_)?(treesummary|summary|entries|pentries|vrsummary|vrpentries)\b",
        sql,
    )
    if not match:
        return None

    table = match.group(1).lower()
    if table == "treesummary":
        return "T"
    if table in {"summary", "vrsummary"}:
        return "S"
    return "E"

_GUFI_ENTRY_FROM = re.compile(
    r"(?i)\bFROM\s+(?:gufi_vt_)?(entries|pentries|vrpentries|vrsummary|summary|treesummary)\b"
)


def normalize_gufi_query_sql(sql: str) -> str:
    ''' Normalize logical SQL before passing it into generic gufi_vt.

    The fixed gufi_vt_pentries wrapper exposes a synthetic path column, but raw
    pentries SQL uses the GUFI path() function. This keeps simple client/model
    queries like SELECT path,name,size FROM pentries executable through -E.
    '''
    sql = re.sub(r"(?i)\bpath\s*\(\s*name\s*\)", "path()", sql)
    if _GUFI_ENTRY_FROM.search(sql):
        sql = re.sub(r"(?i)\bSELECT\s+path\s*,", "SELECT path() AS path,", sql, count=1)
        sql = re.sub(r"(?i)\bSELECT\s+path\s+FROM\b", "SELECT path() AS path FROM", sql, count=1)
    return sql


def normalize_aggregate_query(query: GufiQuery) -> GufiQuery:
    ''' Normalize SQL text in each aggregate pipeline phase. '''
    return query.model_copy(
        update={
            "sql_options": [
                GufiOption(option=item.option, sql=normalize_gufi_query_sql(item.sql))
                for item in query.sql_options
            ]
        }
    )


def detect_shard_local_clauses(sql: str) -> dict[str, Any]:
    ''' Detect SQL clauses that gufi_vt applies per subtree rather than globally. '''
    defaults = {
        "has_order_by": False,
        "has_limit": False,
        "has_offset": False,
        "has_group_by": False,
        "has_aggregates": False,
        "requested_limit": None,
    }
    try:
        parsed = parse_one(sql, read="sqlite")
    except ParseError:
        return defaults

    has_aggregates = any(isinstance(node, exp.AggFunc) for node in parsed.walk())
    limit_node = parsed.find(exp.Limit)
    requested_limit = None
    if limit_node and limit_node.expression is not None:
        try:
            requested_limit = int(limit_node.expression.this)
        except (TypeError, ValueError):
            pass

    return {
        "has_order_by": parsed.find(exp.Order) is not None,
        "has_limit": limit_node is not None,
        "has_offset": parsed.find(exp.Offset) is not None,
        "has_group_by": parsed.find(exp.Group) is not None,
        "has_aggregates": has_aggregates,
        "requested_limit": requested_limit,
    }


def aggregate_recipe_hint(clauses: dict[str, Any]) -> str | None:
    ''' Build guidance when a query needs the aggregate merge pipeline. '''
    reasons: list[str] = []
    if clauses.get("has_order_by") or clauses.get("has_limit") or clauses.get("has_offset"):
        reasons.append("ORDER BY/LIMIT/OFFSET")
    if clauses.get("has_group_by") or clauses.get("has_aggregates"):
        reasons.append("GROUP BY or aggregate functions")
    if not reasons:
        return None
    joined = " and ".join(reasons)
    return (
        f"{joined} in sql_file_index are applied per GUFI subtree, not index-wide. "
        "Use aggregate_sql_query with -I, -E, -K, -J, and put global ORDER BY/LIMIT in -G."
    )


def apply_shard_local_warnings(query_result: GufiQueryResult, sql: str) -> GufiQueryResult:
    ''' Attach shard-local semantics metadata to a direct SQL query result. '''
    clauses = detect_shard_local_clauses(sql)
    hint = aggregate_recipe_hint(clauses)
    if hint:
        query_result.warning = hint
        query_result.execution_mode = "shard_local"
        query_result.requested_limit = clauses.get("requested_limit")
    return query_result


_DATA_PHASES = frozenset({GufiSQLOption.E, GufiSQLOption.S, GufiSQLOption.T})


def build_top_n_files_query(
    index: str,
    limit: int = 20,
    table: str = "vrpentries",
    *,
    threads: int | None = None,
) -> GufiQuery:
    ''' Build a five-phase aggregate query for index-wide largest files. '''
    thread_count = threads if threads is not None else (os.cpu_count() or 1)
    if table == "vrpentries":
        extract_sql = "INSERT INTO intermediate SELECT sname AS path, name, size, mtime FROM vrpentries WHERE type = 'f'"
    else:
        extract_sql = (
            f"INSERT INTO intermediate SELECT path() AS path, name, size, mtime "
            f"FROM {table} WHERE type = 'f'"
        )
    return GufiQuery(
        index=index,
        config=[f"threads={thread_count}"],
        sql_options=[
            GufiOption(
                option=GufiSQLOption.I,
                sql="CREATE TABLE intermediate(path TEXT, name TEXT, size INT64, mtime INT64)",
            ),
            GufiOption(
                option=GufiSQLOption.E,
                sql=extract_sql,
            ),
            GufiOption(
                option=GufiSQLOption.K,
                sql="CREATE TABLE aggregate(path TEXT, name TEXT, size INT64, mtime INT64)",
            ),
            GufiOption(
                option=GufiSQLOption.J,
                sql="INSERT INTO aggregate SELECT path, name, size, mtime FROM intermediate",
            ),
            GufiOption(
                option=GufiSQLOption.G,
                sql=(
                    f"SELECT path, name, size, mtime FROM aggregate "
                    f"ORDER BY size DESC LIMIT {limit}"
                ),
            ),
        ],
    )

def resolve_local_index(index: str) -> str:
    ''' resolve name of a local index to the full path '''

    if index not in get_gufi_indexes():
        raise RuntimeError("Error: Index provided not found at index root.")

    return f'{GUFI_INDEX_ROOT}{index}'

def resolve_remote_index(index: str) -> str:
    ''' resolve name of a remote index to the full path '''

    # SELECT * FROM paths WHERE path ~ '^/[^/]+/?$';

    return f'{index}'

def get_gufi_indexes() -> list[str]:
    index_root = Path(GUFI_INDEX_ROOT).resolve()
    if not index_root.exists():
        raise RuntimeError("Error: GUFI index root does not exist.")
    indexes = []

    # Confirm path is a directory with other dirs inside
    if index_root.is_dir():
        for entry in index_root.iterdir():
            if not entry.is_dir():
                continue
            indexes.append(entry.name)

    return indexes

def execute_sql(sqlline: str, gufi_vt: bool, plain_index: str = None) -> list[Any]:
    ''' Wrapper to execute SQL queries '''

    # GUFI_VT uses memory and operates through the function
    if gufi_vt:
        conn = sqlite3.connect(':memory:')
    # Pulling schema needs a path to an index
    else:
        # Specific path / table to connect to
        if plain_index:
            index = f"{plain_index}" + "/db.db"
        else:
            index = f"{GUFI_INDEX_ROOT}" + "/db.db"
        conn = sqlite3.connect(index)

    # Handle SQLITE3 errors
    try:
        # Using gufi_vt extension
        if gufi_vt:
            conn.enable_load_extension(True)
            cursor = conn.cursor()
            conn.load_extension(GUFIVTLIB)
            conn.enable_load_extension(False)
        # Plain SQL query
        else:
            cursor = conn.cursor()

        print(sqlline, file=sys.stderr)

        # Call GUFI_VT
        cursor.execute(sqlline)

        return cursor.fetchall()

    except sqlite3.Error as e:
        print(f"An SQLite error occurred: {e}",file=sys.stderr)
        conn.close()
        return [f"sql error:", f"{str(e)}"]

    finally:
        conn.close()

def has_treesummary(index: str) -> bool:

    if index == GUFI_INDEX_ROOT:
        response = execute_sql("SELECT name, type FROM sqlite_master WHERE sql IS NOT NULL", False)

    elif index not in get_gufi_indexes():
        raise RuntimeError("Error: Index provided not found at index root.")

    else:
        response = execute_sql("SELECT name FROM sqlite_master WHERE sql IS NOT NULL AND name == 'treesummary'", False, resolve_local_index(index))

    if len(response) == 0:
        return False
    else:
        return True

def index_exists(index: str) -> bool:
    ''' Return True when index names a known GUFI index or an existing directory. '''
    if not index or not str(index).strip():
        return False

    raw = str(index).strip().rstrip("/")
    if os.path.isabs(raw):
        return os.path.isdir(raw)

    if raw in get_gufi_indexes():
        return True

    return os.path.isdir(os.path.join(GUFI_INDEX_ROOT, raw))


def subpath_exists(path: str) -> bool:
    ''' Confirm an index name or subpath exists under the configured GUFI index root. '''
    return index_exists(path)

def validate_aggregate_order(query: GufiQuery) -> tuple[bool, str | None]:
    ''' Validate aggregate pipeline phase order and global ordering placement. '''
    options = query.sql_options
    if not options:
        return False, "Aggregate query requires at least one sql_options entry."

    option_keys = [item.option for item in options]
    if GufiSQLOption.I not in option_keys:
        return False, "Aggregate pipeline requires -I to create the intermediate table first."

    first_data_idx = next(
        (index for index, option in enumerate(option_keys) if option in _DATA_PHASES),
        None,
    )
    i_idx = option_keys.index(GufiSQLOption.I)
    if first_data_idx is not None and i_idx > first_data_idx:
        return False, "-I must come before -E, -S, or -T phases."

    for item in options:
        if item.option not in _DATA_PHASES:
            continue
        clauses = detect_shard_local_clauses(item.sql)
        if clauses["has_order_by"] or clauses["has_limit"] or clauses["has_offset"]:
            return False, (
                f"{item.option} SQL must not contain ORDER BY, LIMIT, or OFFSET; "
                "those belong in the -G final select after -K/-J merge."
            )

    has_g = GufiSQLOption.G in option_keys
    has_k = GufiSQLOption.K in option_keys
    has_j = GufiSQLOption.J in option_keys
    has_e = GufiSQLOption.E in option_keys

    for item in options:
        if item.option != GufiSQLOption.F:
            continue
        clauses = detect_shard_local_clauses(item.sql)
        if (clauses["has_order_by"] or clauses["has_limit"]) and not has_g:
            return False, (
                "-F must not be used for final ORDER BY/LIMIT. "
                "Use -K, -J, and -G; put ORDER BY/LIMIT in -G."
            )
        if re.search(r"(?i)^\s*SELECT", item.sql.strip()) and not has_g:
            return False, (
                "Pipeline uses -F as final SELECT without -G. "
                "Use -K, -J, and -G for index-wide results."
            )

    if has_e and has_g:
        g_sql = next(item.sql for item in options if item.option == GufiSQLOption.G)
        g_clauses = detect_shard_local_clauses(g_sql)
        if g_clauses["has_order_by"] or g_clauses["has_limit"]:
            if not has_k or not has_j:
                return False, (
                    "Index-wide ORDER BY/LIMIT requires -K and -J to merge intermediate rows "
                    "before the -G final select."
                )

    return True, None
