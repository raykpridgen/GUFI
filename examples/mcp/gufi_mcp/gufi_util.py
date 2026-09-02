from mcp.server import MCPServer
from dataclasses import dataclass
from typing import TypedDict
from datetime import datetime, timezone
import asyncio
import base64
import io
import sqlite3
import sys
import subprocess
import os
import json
import re
import shlex
import time
from dotenv import load_dotenv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import sqlglot
import sqlglot.expressions as exp
from sqlglot import parse_one, ParseError

_PACKAGE_DIR = Path(__file__).resolve().parent
_SETTINGS: "GufiMcpSettings | None" = None


@dataclass(frozen=True)
class GufiMcpSettings:
    """Runtime configuration loaded from .env in the gufi_mcp package directory."""

    schema_file: Path
    indexes_root: Path
    gufi_executable: Path
    server_prefix: Path
    client_config: Path
    client_bin: Path
    ssh_identity: Path
    default_index: str
    remote_host: str
    gufi_vt_lib: Path | None
    mcp_transport: str
    mcp_server_host: str
    mcp_server_port: int
    mcp_server_url: str
    plot_dir: Path

    @classmethod
    def load(cls, env_file: Path | None = None) -> "GufiMcpSettings":
        env_path = env_file or (_PACKAGE_DIR / ".env")
        load_dotenv(env_path, override=False)

        def path_from_env(key: str, default: str | None = None, required: bool = True) -> Path:
            raw = os.getenv(key, default)
            if raw is None or raw.strip() == "":
                if required:
                    raise ValueError(f"Missing required environment variable: {key}")
                raise ValueError(f"Missing environment variable: {key}")
            value = Path(raw.strip().strip("'\""))
            if not value.is_absolute():
                value = (_PACKAGE_DIR / value).resolve()
            return value

        schema_file = path_from_env("SCHEMAFILE", "./schemas.json")
        indexes_root = path_from_env("GUFI_INDEXES_ROOT")
        gufi_executable = path_from_env("GUFI_EXECUTABLE")
        server_prefix = path_from_env(
            "GUFI_SERVER_PREFIX",
            str(gufi_executable.parent),
            required=False,
        )
        client_config = path_from_env("GUFI_CLIENT_CONFIG")
        client_bin = path_from_env("GUFI_CLIENT_BIN")
        ssh_identity = Path(os.path.expanduser(os.getenv("GUFI_SSH_IDENTITY", "~/.ssh/gufi_local")))

        host = os.getenv("MCPSRVHOST", "127.0.0.1").strip().strip("'\"")
        port = int(os.getenv("MCPSRVPORT", "8000"))
        transport = os.getenv("MCPTRANSPORT", "streamable-http").strip().strip("'\"")
        mcp_server_url = os.getenv("MCP_SERVER_URL", f"http://{host}:{port}/mcp").strip().strip("'\"")

        vt_raw = os.getenv("GUFIVTLIB", "").strip().strip("'\"")
        gufi_vt_lib = None
        if vt_raw and vt_raw != "path/to/gufi_vt":
            gufi_vt_lib = Path(vt_raw)
            if not gufi_vt_lib.is_absolute():
                gufi_vt_lib = (_PACKAGE_DIR / gufi_vt_lib).resolve()

        plot_default = str(_PACKAGE_DIR / "plots")
        plot_raw = os.getenv("GUFI_PLOT_DIR", plot_default).strip().strip("'\"")
        plot_dir = Path(plot_raw)
        if not plot_dir.is_absolute():
            plot_dir = (_PACKAGE_DIR / plot_dir).resolve()

        return cls(
            schema_file=schema_file,
            indexes_root=indexes_root,
            gufi_executable=gufi_executable,
            server_prefix=server_prefix,
            client_config=client_config,
            client_bin=client_bin,
            ssh_identity=ssh_identity,
            default_index=os.getenv("DEFAULT_INDEX", "notes").strip().strip("'\""),
            remote_host=os.getenv("REMOTEHOST", "127.0.0.1").strip().strip("'\""),
            gufi_vt_lib=gufi_vt_lib,
            mcp_transport=transport,
            mcp_server_host=host,
            mcp_server_port=port,
            mcp_server_url=mcp_server_url,
            plot_dir=plot_dir,
        )


def get_settings() -> GufiMcpSettings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = GufiMcpSettings.load()
    return _SETTINGS


def _sync_module_settings(settings: GufiMcpSettings) -> None:
    global SCHEMAFILE, REMOTEHOST, MCPTRANSPORT, MCPSRVHOST, MCPSRVPORT
    global GUFIVTLIB, GUFI_EXE, GUFI_INDEXES_ROOT, PLOT_DIR

    SCHEMAFILE = str(settings.schema_file)
    REMOTEHOST = settings.remote_host
    MCPTRANSPORT = settings.mcp_transport
    MCPSRVHOST = settings.mcp_server_host
    MCPSRVPORT = str(settings.mcp_server_port)
    GUFIVTLIB = str(settings.gufi_vt_lib) if settings.gufi_vt_lib else ""
    GUFI_EXE = str(settings.gufi_executable)
    GUFI_INDEXES_ROOT = str(settings.indexes_root)
    if not GUFI_INDEXES_ROOT.endswith(os.sep):
        GUFI_INDEXES_ROOT += os.sep
    PLOT_DIR = settings.plot_dir


_settings = get_settings()
_sync_module_settings(_settings)

class FileEntry(TypedDict):
    name: str
    size: int
    uid: str


class ColumnDef(TypedDict):
    name: str
    type: str


class QueryMetadata(TypedDict):
    target_path: str
    total_files_in_scope: int
    is_fully_rolled_up: bool
    treesummary_available: bool
    execution_time_ms: float
    warnings: list[str]


class SummaryTotals(TypedDict):
    total_bytes: int
    total_bytes_human: str
    total_files: int
    total_directories: int
    total_symlinks: int


class HistogramBucket(TypedDict):
    count: int
    total_bytes: int


class SizeHistogram(TypedDict):
    under_1mb: HistogramBucket
    mb_1_to_100mb: HistogramBucket
    mb_100_to_1gb: HistogramBucket
    gb_1_to_100gb: HistogramBucket
    over_100gb: HistogramBucket


class AgeBand(TypedDict):
    bytes: int
    percentage: float


class AgeAndStaleness(TypedDict):
    active_under_30_days: AgeBand
    warm_30_to_180_days: AgeBand
    cold_180_plus_days: AgeBand
    oldest_file_mtime: str
    newest_file_mtime: str


class ExtensionEntry(TypedDict):
    extension: str
    count: int
    total_bytes: int


class OwnerEntry(TypedDict):
    uid: int
    gid: int
    total_bytes: int
    file_count: int


class SubtreeAnalytics(TypedDict):
    query_metadata: QueryMetadata
    summary_totals: SummaryTotals
    size_distribution_histogram: SizeHistogram
    age_and_staleness: AgeAndStaleness
    top_extensions_by_size: list[ExtensionEntry]
    top_owners: list[OwnerEntry]


class PlotResult(TypedDict):
    file_path: str
    image_b64: str
    plot_type: str
    title: str


class SamplingStats(TypedDict):
    directories_sampled: int
    directories_drifted: int
    directories_same: int
    directories_unreadable: int
    drift_ratio: float


class ShardCounts(TypedDict):
    metadata_expected: int
    source_expected: int
    accessible: int
    missing: int
    corrupt: int
    unindexed: int
    rolled_up_subdirs: int


class ShardIssue(TypedDict):
    relative_path: str
    index_path: str
    source_path: str | None
    issue: str
    detail: str


class IndexHealthResult(TypedDict):
    target_path: str
    source_path: str | None
    freshness: str
    index_age_hours: float
    last_indexed: str
    sampling: SamplingStats
    shard_counts: ShardCounts
    shard_issues: list[ShardIssue]
    warnings: list[str]
    execution_time_ms: float


FRESH_AGE_HOURS = 12
STALE_AGE_HOURS = 48
FRESH_DRIFT = 0.05
STALE_DRIFT = 0.15
EMPTY_DB_TEMPLATE_SIZE = 49152
DRIFT_TOLERANCE_SEC = 1
DEFAULT_MAX_SHARD_ISSUES = 50

SchemaRegistry = dict[str, list[ColumnDef]]


def parse_schema_registry(path: str) -> SchemaRegistry:
    """
    Parse schemas.json into a structured registry of column definitions.

    CREATE TABLE entries are parsed via sqlglot AST to recover exact name+type.
    CREATE VIEW entries use the trailing SQLite comment /* view(col1,col2,...) */
    to recover column names; types are marked 'derived' since they come from joins.
    The 'query_surfaces' description dict is skipped.
    """
    with open(path, "r") as f:
        data = json.load(f)

    registry: SchemaRegistry = {}

    for key, value in data.items():
        if key == "query_surfaces" or not isinstance(value, str):
            continue

        ddl = value
        upper = ddl.lstrip().upper()

        if upper.startswith("CREATE TABLE"):
            try:
                stmt = parse_one(ddl, read="sqlite")
                cols: list[ColumnDef] = []
                for col_def in stmt.find_all(exp.ColumnDef):
                    col_type = col_def.args.get("kind")
                    cols.append(ColumnDef(
                        name=col_def.name,
                        type=col_type.sql() if col_type else "UNKNOWN"
                    ))
                registry[key] = cols
            except ParseError as e:
                print(f"Schema parse error for '{key}': {e}", file=sys.stderr)

        elif upper.startswith("CREATE VIEW") or upper.startswith("CREATE TEMP VIEW"):
            # Extract column list from trailing comment: /* view_name(col1,col2,...) */
            match = re.search(r'/\*\s*\w+\(([^)]+)\)\s*\*/', ddl)
            if match:
                col_names = [c.strip() for c in match.group(1).split(",")]
                registry[key] = [ColumnDef(name=n, type="derived") for n in col_names]
            else:
                print(f"No column comment found for view '{key}'", file=sys.stderr)

    return registry


def resolve_view_types(registry: SchemaRegistry) -> SchemaRegistry:
    """
    Second pass: resolve 'derived' column types in views by tracing column names
    back to concrete base table entries in the same registry.

    Builds a flat name->type lookup from all non-derived columns first, then
    walks every view column still marked 'derived' and replaces it with the
    matched base type. Columns that are genuine computed expressions and cannot
    be matched remain 'derived'.
    """
    # Collect known types from concrete table columns only
    base_types: dict[str, str] = {}
    for cols in registry.values():
        for col in cols:
            if col["type"] != "derived":
                base_types[col["name"].lower()] = col["type"]

    # Resolve derived columns in views
    # Quick fix: Check for s and d to resolve to base types
    for cols in registry.values():
        for col in cols:
            if col["type"] == "derived":
                resolved = base_types.get(col["name"].lower())
                if resolved:
                    col["type"] = resolved
                else:
                    if col["name"][0] == "d":
                        resolved = base_types.get(col["name"].lower()[1:])
                        if resolved:
                            col["type"] = resolved

                    elif col["name"][0] == "s":
                        resolved = base_types.get(col["name"].lower()[1:])
                        if resolved:
                            col["type"] = resolved

    return registry


def is_valid_sql_query(sql_query: str, dialect: str = "sqlite") -> bool:
    try:
        parse_one(sql_query, read=dialect)
        return True
    except ParseError as e:
        print(f"SQL Grammar Error: {str(e)}", file=sys.stderr)
        return False


def validate_query_columns(sql_query: str, table: str, registry: SchemaRegistry) -> list[str]:
    """
    Walk the sqlglot AST of sql_query and return any column names that are not
    present in registry[table]. Returns an empty list when all columns are valid
    or the table is not in the registry.
    """
    if table not in registry:
        return []

    known = {col["name"].lower() for col in registry[table]}

    try:
        ast = parse_one(sql_query, read="sqlite")
    except ParseError:
        return []

    unknown = []
    for col_node in ast.find_all(exp.Column):
        col_name = col_node.name.lower()
        # Skip wildcard (*) and table-qualified references we can't resolve here
        if col_name and col_name != "*" and col_name not in known:
            unknown.append(col_node.name)

    return unknown


def format_bytes(n: int) -> str:
    """Human-readable byte size string (TiB/GiB/MiB/KiB/B)."""
    for unit, threshold in [("TiB", 2**40), ("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)]:
        if n >= threshold:
            return f"{n / threshold:.2f} {unit}"
    return f"{n} B"


def resolve_gufi_path(path: str) -> tuple[str, Path]:
    """
    Resolve 'index_name' or 'index_name/sub/path' to (index_name, abs_start_dir).

    The first path component is treated as the index name; any remaining components
    are the subpath within that index. Returns the validated absolute start directory
    for gufi_query. Raises ValueError if the resolved path does not exist.
    """
    parts = Path(path).parts
    index_name = parts[0]
    subpath = Path(*parts[1:]) if len(parts) > 1 else Path("")
    start_dir = Path(GUFI_INDEXES_ROOT) / index_name / subpath
    if not start_dir.is_dir():
        raise ValueError(f"Resolved path '{start_dir}' does not exist or is not a directory.")
    return index_name, start_dir


def check_treesummary(start_dir: Path) -> bool:
    """Return True if the treesummary table exists in start_dir/db.db."""
    db_path = start_dir / "db.db"
    if not db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='treesummary'")
        found = cur.fetchone() is not None
        conn.close()
        return found
    except sqlite3.Error:
        return False


def build_treesummary(start_dir: Path) -> bool:
    """
    Attempt to build treesummary tables by calling GUFI_TREESUMMARY_EXE.
    Returns True on success, False if the executable is not configured or fails.
    """
    exe = os.getenv("GUFI_TREESUMMARY_EXE")
    if not exe:
        return False
    result = subprocess.run([exe, str(start_dir)], capture_output=True, text=True)
    return result.returncode == 0


def epoch_to_iso(epoch: int) -> str:
    if epoch <= 0:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def read_index_info(index_name: str) -> dict | None:
    """Read indexing metadata from the GUFI common-parent info.db for index_name."""
    info_path = Path(GUFI_INDEXES_ROOT) / "info.db"
    if not info_path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(info_path))
        cur = conn.cursor()
        cur.execute("SELECT end, src, start FROM info WHERE name = ?", (index_name,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return {"end": row[0], "src": row[1], "start": row[2]}
    except sqlite3.Error:
        return None


def resolve_source_path(
    index_name: str,
    subpath: Path,
    override: str | None,
) -> tuple[Path | None, list[str]]:
    """Map idx_path to the live source directory, using info.db src or an override."""
    warnings: list[str] = []
    if override:
        src = Path(override)
    else:
        info = read_index_info(index_name)
        if not info or not info.get("src"):
            warnings.append(
                "Source path unavailable: no info.db entry and no source_path override"
            )
            return None, warnings
        src = Path(info["src"])

    if subpath.parts:
        src = src / subpath

    if not src.is_dir():
        warnings.append(f"Source path '{src}' does not exist or is not a directory")
        return None, warnings
    return src, warnings


def classify_freshness(age_hours: float, drift_ratio: float) -> str:
    if age_hours >= STALE_AGE_HOURS or drift_ratio >= STALE_DRIFT:
        return "STALE"
    if age_hours < FRESH_AGE_HOURS and drift_ratio < FRESH_DRIFT:
        return "FRESH"
    return "DRIFTED"


def compute_index_age(start_dir: Path, index_name: str) -> tuple[float, str, list[str]]:
    """Return (age_hours, last_indexed_iso, warnings)."""
    warnings: list[str] = []
    last_indexed_epoch: float | None = None

    info = read_index_info(index_name)
    if info and info.get("end") is not None:
        try:
            last_indexed_epoch = float(info["end"])
        except (TypeError, ValueError):
            warnings.append("info.db end timestamp is not numeric")
    elif (start_dir / "db.db").is_file():
        last_indexed_epoch = (start_dir / "db.db").stat().st_mtime
    else:
        warnings.append("Could not determine last indexed time")
        return 0.0, "", warnings

    age_hours = (time.time() - last_indexed_epoch) / 3600.0
    return age_hours, epoch_to_iso(int(last_indexed_epoch)), warnings


def validate_db_shard(db_path: Path) -> tuple[bool, str]:
    """Return (accessible, detail). Accessible shards open cleanly and contain required tables."""
    if not db_path.is_file():
        return False, "file_not_found"
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("PRAGMA quick_check")
        qc = cur.fetchone()
        if not qc or qc[0] != "ok":
            conn.close()
            return False, f"quick_check:{qc[0] if qc else 'failed'}"

        for table in ("summary", "entries"):
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            if not cur.fetchone():
                conn.close()
                return False, f"missing_table:{table}"

        if db_path.stat().st_size == EMPTY_DB_TEMPLATE_SIZE:
            cur.execute("SELECT COUNT(*) FROM summary WHERE isroot = 1")
            row = cur.fetchone()
            if row and row[0] == 0:
                conn.close()
                return False, "empty_template"

        conn.close()
        return True, ""
    except sqlite3.DatabaseError as e:
        return False, str(e)


def _rel_depth(base: Path, current: Path) -> int:
    try:
        rel = current.relative_to(base)
    except ValueError:
        return 0
    return 0 if rel == Path(".") else len(rel.parts)


def _rel_path_from(base: Path, current: Path) -> str:
    try:
        rel = current.relative_to(base)
    except ValueError:
        return ""
    return "" if rel == Path(".") else rel.as_posix()


def measure_drift(
    sampled_dirs: list[tuple[str, int]],
    source_root: Path,
) -> SamplingStats:
    drifted = same = unreadable = 0
    for name, gufi_mtime in sampled_dirs:
        live_path = source_root / name if name else source_root
        try:
            posix_mtime = int(os.stat(live_path).st_mtime)
        except OSError:
            unreadable += 1
            continue
        if posix_mtime > gufi_mtime + DRIFT_TOLERANCE_SEC:
            drifted += 1
        else:
            same += 1

    sampled = len(sampled_dirs)
    return SamplingStats(
        directories_sampled=sampled,
        directories_drifted=drifted,
        directories_same=same,
        directories_unreadable=unreadable,
        drift_ratio=round(drifted / sampled, 4) if sampled else 0.0,
    )


def empty_shard_counts() -> ShardCounts:
    return ShardCounts(
        metadata_expected=0,
        source_expected=0,
        accessible=0,
        missing=0,
        corrupt=0,
        unindexed=0,
        rolled_up_subdirs=0,
    )


def empty_sampling_stats() -> SamplingStats:
    return SamplingStats(
        directories_sampled=0,
        directories_drifted=0,
        directories_same=0,
        directories_unreadable=0,
        drift_ratio=0.0,
    )


def audit_shard_integrity(
    start_dir: Path,
    source_root: Path | None,
    shard_check_depth: int,
    max_issues: int = DEFAULT_MAX_SHARD_ISSUES,
) -> tuple[ShardCounts, list[ShardIssue], list[str]]:
    """
    Audit expected vs accessible shards, corrupt shards, and unindexed source paths.
    """
    counts = empty_shard_counts()
    issues: list[ShardIssue] = []
    warnings: list[str] = []
    seen_issues: set[tuple[str, str]] = set()
    truncated = False

    def append_issue(issue: ShardIssue) -> None:
        nonlocal truncated
        key = (issue["issue"], issue["relative_path"])
        if key in seen_issues:
            return
        seen_issues.add(key)
        if len(issues) < max_issues:
            issues.append(issue)
        elif not truncated:
            truncated = True
            warnings.append("shard issue list truncated; see shard_counts for totals")

    def make_issue(
        index_dir: Path,
        source_dir: Path | None,
        issue: str,
        detail: str,
    ) -> ShardIssue:
        rp = _rel_path_from(start_dir, index_dir)
        return ShardIssue(
            relative_path=rp,
            index_path=str(index_dir),
            source_path=str(source_dir) if source_dir else None,
            issue=issue,
            detail=detail,
        )

    def source_dir_for(index_dir: Path) -> Path | None:
        if not source_root:
            return None
        rp = _rel_path_from(start_dir, index_dir)
        return source_root / rp if rp else source_root

    for root, dirs, _files in os.walk(start_dir, topdown=True):
        root_path = Path(root)
        depth = _rel_depth(start_dir, root_path)
        if depth > shard_check_depth:
            dirs.clear()
            continue

        db_path = root_path / "db.db"
        src_dir = source_dir_for(root_path)

        if not db_path.is_file():
            if depth > 0:
                rp = _rel_path_from(start_dir, root_path)
                if ("missing_shard", rp) not in seen_issues:
                    counts["missing"] += 1
                    append_issue(make_issue(root_path, src_dir, "missing_shard", "no_db_file"))
            continue

        ok, detail = validate_db_shard(db_path)
        if not ok:
            counts["corrupt"] += 1
            append_issue(make_issue(root_path, src_dir, "corrupt_shard", detail))
            warnings.append(
                f"parent shard corrupt; child metadata check skipped at {root_path}"
            )
            dirs.clear()
            continue

        counts["accessible"] += 1

        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute("SELECT isrolledup FROM summary WHERE isroot = 1")
            row = cur.fetchone()
            is_rolled_up = bool(row and row[0])
            child_names: list[str] = []
            if not is_rolled_up:
                cur.execute("SELECT name FROM entries WHERE type = 'd'")
                child_names = [r[0] for r in cur.fetchall() if r[0]]
            conn.close()
        except sqlite3.Error as e:
            warnings.append(f"Failed to read shard metadata at {root_path}: {e}")
            continue

        if is_rolled_up:
            counts["rolled_up_subdirs"] += len(child_names)
            dirs.clear()
            continue

        for name in child_names:
            counts["metadata_expected"] += 1
            child_index = root_path / name
            child_db = child_index / "db.db"
            child_src = source_dir_for(child_index)

            if not child_db.is_file():
                counts["missing"] += 1
                append_issue(
                    make_issue(child_index, child_src, "missing_shard", "metadata_expected")
                )
                continue

            child_ok, child_detail = validate_db_shard(child_db)
            if not child_ok:
                counts["corrupt"] += 1
                append_issue(
                    make_issue(child_index, child_src, "corrupt_shard", child_detail)
                )

    if source_root:
        for root, dirs, _files in os.walk(source_root, topdown=True):
            root_path = Path(root)
            depth = _rel_depth(source_root, root_path)
            if depth > shard_check_depth:
                dirs.clear()
                continue

            counts["source_expected"] += 1
            rp = _rel_path_from(source_root, root_path)
            index_dir = start_dir / rp if rp else start_dir
            child_db = index_dir / "db.db"
            ok, _detail = validate_db_shard(child_db) if child_db.is_file() else (False, "")
            if ok:
                continue

            counts["unindexed"] += 1
            append_issue(
                ShardIssue(
                    relative_path=rp,
                    index_path=str(index_dir),
                    source_path=str(root_path),
                    issue="unindexed_path",
                    detail="no_accessible_shard",
                )
            )

    return counts, issues, warnings


def parse_client_config(config_path: Path) -> tuple[str, int]:
    """Read Server and Port from a GUFI client config file."""
    server: str | None = None
    port = 22
    for line in config_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key == "Server":
            server = value
        elif key == "Port":
            port = int(value)
    if not server:
        raise ValueError(f"Missing Server setting in client config: {config_path}")
    return server, port


def resolve_index_path(index: str, indexes_root: Path | None = None) -> Path:
    """Resolve an index name (relative to IndexRoot) to an on-disk directory."""
    root = indexes_root or get_settings().indexes_root
    index_path = (root / index).resolve()
    if not index_path.is_dir():
        raise FileNotFoundError(f"GUFI index '{index}' not found at {index_path}")
    return index_path


def _build_client_args(tool: str, index: str, arguments: str, indexes_root: Path | None = None) -> list[str]:
    extra = shlex.split(arguments) if arguments.strip() else []
    if tool == "query":
        index_path = resolve_index_path(index, indexes_root)
        return extra + [str(index_path) + os.sep]
    if tool == "stats":
        return extra + [index]
    return [index] + extra


def run_gufi_client_tool(
    tool: str,
    index: str,
    arguments: str = "",
    client_config: str | None = None,
) -> str:
    """
    Run a GUFI client wrapper command over SSH.

    The remote server executes the real GUFI tool (gufi_ls, gufi_du, etc.) against
    paths relative to IndexRoot. `index` is passed as the first positional argument
    for most tools; for gufi_query and gufi_stats it is appended last.
    `arguments` holds any additional flags (for example "-a" or "-type f").
    """
    settings = get_settings()
    config_path = Path(client_config).expanduser() if client_config else settings.client_config
    if not config_path.is_file():
        return f"Error: client config not found: {config_path}\n"

    try:
        server, port = parse_client_config(config_path)
    except ValueError as exc:
        return f"Error: {exc}\n"

    client_args = _build_client_args(tool, index, arguments, settings.indexes_root)
    client_script = settings.client_bin / f"gufi_{tool}"
    use_client_script = client_script.is_file() and tool not in ("query", "stats")

    if use_client_script:
        cmd = [sys.executable, str(client_script), *client_args]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(settings.client_bin) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    else:
        prefix = settings.server_prefix
        ssh_key = str(settings.ssh_identity.expanduser())
        remote_tool = "gufi_query" if tool == "query" else f"gufi_{tool}"
        remote_cmd = "PYTHONPATH={}/lib {}/bin/{} {}".format(
            prefix,
            prefix,
            remote_tool,
            " ".join(shlex.quote(part) for part in client_args),
        )
        cmd = [
            "ssh",
            "-i", ssh_key,
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",
            server,
            "-p", str(port),
            "--",
            remote_cmd,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)

    output = result.stdout
    if result.stderr:
        output += ("\n" if output else "") + result.stderr
    if result.returncode != 0 and not output.strip():
        return f"Error: gufi_{tool} exited with code {result.returncode}\n"
    return output


def format_tool_result_text(result) -> str:
    """Extract plain text from an MCP CallToolResult."""
    chunks: list[str] = []
    for item in result.content:
        text = getattr(item, "text", None)
        if text:
            chunks.append(text)
    if chunks:
        return "\n".join(chunks)
    if result.structured_content is not None:
        return json.dumps(result.structured_content, indent=2)
    return ""
