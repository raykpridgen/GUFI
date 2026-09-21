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
import json

load_dotenv()

SCHEMAFILE = os.getenv('SCHEMAFILE')
REMOTEHOST = os.getenv('REMOTEHOST')
MCPTRANSPORT = os.getenv('MCPTRANSPORT')
MCPSRVHOST = os.getenv('MCPSRVHOST')
MCPSRVPORT = os.getenv('MCPSRVPORT')
GUFIVTLIB = os.getenv('GUFIVTLIB')
GUFI_INDEX_ROOT = os.getenv('GUFI_INDEX_ROOT')
GUFI_QUERY = os.getenv('GUFI_QUERY')
GUFI_LIB = os.getenv('GUFI_LIB')
from enum import Enum

WRAPPER_TOOLS = ("gufi_find", "gufi_ls", "gufi_du", "gufi_stat", "gufi_stats", "gufi_getfattr")
_WRAPPER_TOOLS = WRAPPER_TOOLS
_TOOL_HELP_CACHE: dict[str, str] = {}

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
    error: str | None = None


def parse_delimited_stdout(stdout: str, delimiter: str = "\t") -> list[list[str]]:
    ''' Parse delimiter-separated command stdout into structured rows. '''
    text = stdout.strip()
    if not text:
        return []
    return [line.split(delimiter) for line in text.splitlines()]


def gufi_lib_path() -> str:
    ''' Return the directory containing gufi_common for GUFI Python CLI tools. '''
    if GUFI_LIB:
        return GUFI_LIB
    if GUFIVTLIB:
        return str(Path(GUFIVTLIB).resolve().parent)
    return ""


def run_gufi_cli(argv: list[str]) -> subprocess.CompletedProcess:
    ''' Run a GUFI Python CLI with PYTHONPATH set so gufi_common imports succeed. '''
    env = os.environ.copy()
    gufi_lib = gufi_lib_path()
    if gufi_lib:
        prefix = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = gufi_lib + (os.pathsep + prefix if prefix else "")
    return subprocess.run(argv, capture_output=True, text=True, env=env)


def pack_command_result(result: subprocess.CompletedProcess, delimiter: str = "\t") -> dict[str, Any]:
    ''' Build a tool result from a subprocess; failed runs surface error, not fake rows. '''
    stderr = result.stderr.strip() if result.stderr and result.stderr.strip() else None
    if result.returncode != 0:
        error = stderr or f"Command failed with exit code {result.returncode}"
        tool_result = GufiToolResult(
            rows=[],
            row_count=0,
            stderr=stderr,
            returncode=result.returncode,
            error=error,
        )
        return tool_result.model_dump()

    rows = parse_delimited_stdout(result.stdout, delimiter)
    error = None
    if not rows and stderr and any(
        phrase in stderr
        for phrase in (
            "No such file or directory",
            "Could not stat",
            "unknown predicate",
            "ModuleNotFoundError",
        )
    ):
        error = stderr
    tool_result = GufiToolResult(
        rows=rows,
        row_count=len(rows),
        stderr=stderr,
        returncode=result.returncode,
        error=error,
    )
    return tool_result.model_dump()


def capture_tool_help(tool_name: str) -> str:
    ''' Run a GUFI CLI --help once and cache the text for tool schemas and resources. '''
    if tool_name in _TOOL_HELP_CACHE:
        return _TOOL_HELP_CACHE[tool_name]
    result = run_gufi_cli([tool_name, "--help"])
    text = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0 and not text:
        text = f"{tool_name} --help failed (exit {result.returncode})"
    _TOOL_HELP_CACHE[tool_name] = text
    return text


def warm_tool_help_cache() -> None:
    ''' Pre-load help text for all command wrapper tools at server startup. '''
    for tool_name in _WRAPPER_TOOLS:
        capture_tool_help(tool_name)


def get_tool_help(tool_name: str) -> str:
    ''' Return cached or freshly captured help for one wrapper tool. '''
    return capture_tool_help(tool_name)


def format_tool_description(summary: str, tool_name: str, example: str, *, max_help_chars: int = 1800) -> str:
    ''' Build an MCP tool description with embedded usage and a copy-paste example. '''
    help_text = get_tool_help(tool_name)
    if len(help_text) > max_help_chars:
        help_text = help_text[:max_help_chars] + "\n... [truncated; read gufi://tool-guide/" + tool_name + "]"
    return (
        f"{summary}\n\n"
        f"Example:\n{example}\n\n"
        f"Usage ({tool_name}):\n{help_text}\n\n"
        "Do not call this tool solely to fetch help; usage is included above."
    )


def _append_extra_flags(cmd: list[str], extra_flags: list[str] | None) -> None:
    if extra_flags:
        cmd.extend(extra_flags)


def cli_target_path(index: str, subpath: str | None = None) -> str:
    ''' Build the path argument GUFI CLI tools expect (index name, not absolute path). '''
    if os.path.isabs(index):
        base = index.rstrip("/")
    else:
        if index not in get_gufi_indexes():
            raise RuntimeError(f"Error: Index provided not found at index root: {index}")
        base = index.strip().rstrip("/")
    if subpath:
        return f"{base}/{subpath.lstrip('/')}"
    return base


def resolve_index_path(index: str, subpath: str | None = None) -> str:
    ''' Resolve an index name and optional subpath to a filesystem path under the index root. '''
    relative = cli_target_path(index, subpath)
    if os.path.isabs(relative):
        return relative
    return os.path.join(GUFI_INDEX_ROOT, relative)


def build_find_argv(
    index: str,
    *,
    subpath: str | None = None,
    name: str | None = None,
    type: str | None = "f",
    mtime: str | None = None,
    size: str | None = None,
    limit: int | None = None,
    largest: bool = False,
    extra_flags: list[str] | None = None,
) -> list[str]:
    ''' Build argv for gufi_find. mtime uses GNU find day semantics (+N = older than N days). '''
    cmd = ["gufi_find", cli_target_path(index, subpath)]
    if type:
        cmd.extend(["-type", type])
    if name:
        cmd.extend(["-name", name])
    if mtime:
        cmd.extend(["-mtime", mtime])
    if size:
        cmd.extend(["-size", size])
    if largest:
        cmd.append("--largest")
    if limit is not None:
        cmd.extend(["--num-results", str(limit)])
    _append_extra_flags(cmd, extra_flags)
    cmd.extend(["--delim", "\t"])
    return cmd


def build_ls_argv(
    index: str,
    *,
    subpath: str | None = None,
    long_format: bool = False,
    human_readable: bool = False,
    recursive: bool = False,
    extra_flags: list[str] | None = None,
) -> list[str]:
    ''' Build argv for gufi_ls. '''
    cmd = ["gufi_ls"]
    if long_format:
        cmd.append("-l")
    if human_readable:
        cmd.append("-h")
    if recursive:
        cmd.append("-R")
    _append_extra_flags(cmd, extra_flags)
    cmd.extend(["--delim", "\t", cli_target_path(index, subpath)])
    return cmd


def build_du_argv(
    index: str,
    *,
    subpath: str | None = None,
    human_readable: bool = False,
    summarize: bool = False,
    extra_flags: list[str] | None = None,
) -> list[str]:
    ''' Build argv for gufi_du. Requires treesummary on the target path. '''
    cmd = ["gufi_du"]
    if human_readable:
        cmd.append("-h")
    if summarize:
        cmd.append("-s")
    _append_extra_flags(cmd, extra_flags)
    cmd.append(cli_target_path(index, subpath))
    return cmd


def build_stat_argv(
    index: str,
    file: str,
    *,
    extra_flags: list[str] | None = None,
) -> list[str]:
    ''' Build argv for gufi_stat. file is relative to the index root unless absolute. '''
    cmd = ["gufi_stat"]
    _append_extra_flags(cmd, extra_flags)
    if os.path.isabs(file):
        cmd.append(file)
    else:
        cmd.append(cli_target_path(index, file))
    return cmd


def build_stats_argv(
    index: str,
    stat: str,
    *,
    subpath: str | None = None,
    recursive: bool = False,
    num_results: int | None = None,
    extra_flags: list[str] | None = None,
) -> list[str]:
    ''' Build argv for gufi_stats. '''
    cmd = ["gufi_stats"]
    if recursive:
        cmd.append("-r")
    if num_results is not None:
        cmd.extend(["--num-results", str(num_results)])
    _append_extra_flags(cmd, extra_flags)
    cmd.extend([stat, cli_target_path(index, subpath), "--delim", "\t"])
    return cmd


def build_getfattr_argv(
    index: str,
    path: str,
    *,
    recursive: bool = False,
    extra_flags: list[str] | None = None,
) -> list[str]:
    ''' Build argv for gufi_getfattr. '''
    cmd = ["gufi_getfattr"]
    if recursive:
        cmd.append("-R")
    _append_extra_flags(cmd, extra_flags)
    cmd.extend([cli_target_path(index, path), "--delim", "\t"])
    return cmd

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


def build_mtime_filter_query(
    index: str,
    cutoff_epoch: int,
    *,
    limit: int = 100,
    order: str = "mtime ASC",
    comparison: str = "<",
    table: str = "vrpentries",
    threads: int | None = None,
) -> GufiQuery:
    ''' Build an aggregate query for index-wide files filtered by mtime against a Unix epoch cutoff. '''
    thread_count = threads if threads is not None else (os.cpu_count() or 1)
    if table == "vrpentries":
        extract_sql = (
            f"INSERT INTO intermediate SELECT sname, dname, name, mtime, size "
            f"FROM vrpentries WHERE type = 'f' AND mtime {comparison} {cutoff_epoch}"
        )
        i_sql = "CREATE TABLE intermediate(sname TEXT, dname TEXT, name TEXT, mtime INT64, size INT64)"
        k_sql = "CREATE TABLE aggregate(sname TEXT, dname TEXT, name TEXT, mtime INT64, size INT64)"
        j_sql = "INSERT INTO aggregate SELECT sname, dname, name, mtime, size FROM intermediate"
        g_sql = f"SELECT sname, dname, name, mtime, size FROM aggregate ORDER BY {order} LIMIT {limit}"
    else:
        extract_sql = (
            f"INSERT INTO intermediate SELECT path() AS filepath, name, mtime, size "
            f"FROM {table} WHERE type = 'f' AND mtime {comparison} {cutoff_epoch}"
        )
        i_sql = "CREATE TABLE intermediate(filepath TEXT, name TEXT, mtime INT64, size INT64)"
        k_sql = "CREATE TABLE aggregate(filepath TEXT, name TEXT, mtime INT64, size INT64)"
        j_sql = "INSERT INTO aggregate SELECT filepath, name, mtime, size FROM intermediate"
        g_sql = f"SELECT filepath, name, mtime, size FROM aggregate ORDER BY {order} LIMIT {limit}"

    return GufiQuery(
        index=index,
        config=[f"threads={thread_count}"],
        sql_options=[
            GufiOption(option=GufiSQLOption.I, sql=i_sql),
            GufiOption(option=GufiSQLOption.E, sql=extract_sql),
            GufiOption(option=GufiSQLOption.K, sql=k_sql),
            GufiOption(option=GufiSQLOption.J, sql=j_sql),
            GufiOption(option=GufiSQLOption.G, sql=g_sql),
        ],
    )


def build_mtime_count_query(
    index: str,
    cutoff_epoch: int,
    *,
    comparison: str = "<",
    threads: int | None = None,
) -> GufiQuery:
    ''' Build an aggregate query that counts regular files matching an mtime cutoff index-wide. '''
    thread_count = threads if threads is not None else (os.cpu_count() or 1)
    return GufiQuery(
        index=index,
        config=[f"threads={thread_count}"],
        sql_options=[
            GufiOption(option=GufiSQLOption.I, sql="CREATE TABLE intermediate(count INT64)"),
            GufiOption(
                option=GufiSQLOption.E,
                sql=(
                    f"INSERT INTO intermediate SELECT 1 FROM vrpentries "
                    f"WHERE type = 'f' AND mtime {comparison} {cutoff_epoch}"
                ),
            ),
            GufiOption(option=GufiSQLOption.K, sql="CREATE TABLE aggregate(count INT64)"),
            GufiOption(option=GufiSQLOption.J, sql="INSERT INTO aggregate SELECT COUNT(*) FROM intermediate"),
            GufiOption(option=GufiSQLOption.G, sql="SELECT SUM(count) AS file_count FROM aggregate"),
        ],
    )


def build_mtime_bucket_query(
    index: str,
    now_epoch: int,
    *,
    bucket_days: list[int] | None = None,
    threads: int | None = None,
) -> GufiQuery:
    ''' Build an aggregate query that buckets regular files by modification age. '''
    thread_count = threads if threads is not None else (os.cpu_count() or 1)
    days = bucket_days or [7, 14]
    if len(days) == 1:
        day = days[0]
        sec = day * 86400
        case_sql = (
            f"CASE WHEN mtime >= {now_epoch - sec} THEN 'modified_last_{day}_days' "
            f"ELSE 'stale_over_{day}_days' END"
        )
    else:
        d0, d1 = days[0], days[1]
        sec0 = d0 * 86400
        sec1 = d1 * 86400
        case_sql = (
            f"CASE WHEN mtime >= {now_epoch - sec0} THEN 'modified_last_{d0}_days' "
            f"WHEN mtime >= {now_epoch - sec1} THEN 'stale_{d0}_to_{d1}_days' "
            f"ELSE 'stale_over_{d1}_days' END"
        )

    return GufiQuery(
        index=index,
        config=[f"threads={thread_count}"],
        sql_options=[
            GufiOption(option=GufiSQLOption.I, sql="CREATE TABLE intermediate(bucket TEXT)"),
            GufiOption(
                option=GufiSQLOption.E,
                sql=f"INSERT INTO intermediate SELECT {case_sql} FROM vrpentries WHERE type = 'f'",
            ),
            GufiOption(option=GufiSQLOption.K, sql="CREATE TABLE aggregate(bucket TEXT, file_count INT64)"),
            GufiOption(
                option=GufiSQLOption.J,
                sql="INSERT INTO aggregate SELECT bucket, COUNT(*) FROM intermediate GROUP BY bucket",
            ),
            GufiOption(
                option=GufiSQLOption.G,
                sql="SELECT bucket, SUM(file_count) AS file_count FROM aggregate GROUP BY bucket ORDER BY file_count DESC",
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

def load_schema_hints(name: str, schema: list[list[str]]) -> dict:
    ''' Attach static hints to dynamically loaded schema from PRAGMA '''

    with open(SCHEMAFILE, "r") as f:
        hints = json.load(f)
    schema_hints = hints["tables"][name]["columns"]
    schema_comp = {}

    # For each column in the schema, attach hint to a dictionary entry
    for col in schema:
        # Fill type from file if blank
        if col[1] == '':
            schema_comp[col[0]] = {"type": schema_hints[col[0]]["type"], "hint": schema_hints[col[0]]["hint"]}
        else:
            schema_comp[col[0]] = {"type": col[1], "hint": schema_hints[col[0]]["hint"]}

    return schema_comp
