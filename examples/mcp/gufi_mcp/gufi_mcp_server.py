#!/usr/bin/env python3
# This file is part of GUFI, which is part of MarFS, which is released
# under the BSD license.
#
#
# Copyright (c) 2017, Los Alamos National Security (LANS), LLC
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
# list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation and/or
# other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors
# may be used to endorse or promote products derived from this software without
# specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
# IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE
# OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
#
# From Los Alamos National Security, LLC:
# LA-CC-15-039
#
# Copyright (c) 2017, Los Alamos National Security, LLC All rights reserved.
# Copyright 2017. Los Alamos National Security, LLC. This software was produced
# under U.S. Government contract DE-AC52-06NA25396 for Los Alamos National
# Laboratory (LANL), which is operated by Los Alamos National Security, LLC for
# the U.S. Department of Energy. The U.S. Government has rights to use,
# reproduce, and distribute this software.  NEITHER THE GOVERNMENT NOR LOS
# ALAMOS NATIONAL SECURITY, LLC MAKES ANY WARRANTY, EXPRESS OR IMPLIED, OR
# ASSUMES ANY LIABILITY FOR THE USE OF THIS SOFTWARE.  If software is
# modified to produce derivative works, such modified software should be
# clearly marked, so as not to confuse it with the version available from
# LANL.
#
# THIS SOFTWARE IS PROVIDED BY LOS ALAMOS NATIONAL SECURITY, LLC AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL LOS ALAMOS NATIONAL SECURITY, LLC OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT
# OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY
# OF SUCH DAMAGE.


from gufi_util import (
    GufiMcpSettings,
    MCPServer,
    Path,
    SchemaRegistry,
    get_settings,
    is_valid_sql_query,
    json,
    os,
    parse_schema_registry,
    re,
    resolve_index_path,
    resolve_view_types,
    run_gufi_client_tool,
    subprocess,
    validate_query_columns,
)

SETTINGS: GufiMcpSettings = get_settings()
SCHEMA_REGISTRY: SchemaRegistry = resolve_view_types(
    parse_schema_registry(str(SETTINGS.schema_file))
)

mcp = MCPServer("gufi-mcp")


def _run_gufi_client(
    tool: str,
    index: str,
    arguments: str = "",
    client_config: str = "",
) -> str:
    """Shared helper for all gufi_client_* MCP tools."""
    if not index.strip():
        index = SETTINGS.default_index
    config_override = client_config.strip() or None
    return run_gufi_client_tool(tool, index, arguments, config_override)


@mcp.tool()
def gufi_version() -> str:
    """Return the version string of the configured gufi_query executable."""
    result = subprocess.run(
        [str(SETTINGS.gufi_executable), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else result.stderr


@mcp.tool()
def gufi_location() -> str:
    """Return the absolute path to the configured gufi_query executable."""
    return str(SETTINGS.gufi_executable.resolve())


@mcp.tool()
def gufi_query_local_index(index: str, sql_query: str, return_limit: int = 0) -> str:
    """Run a SQL query against a local GUFI index. return_limit=0 means no limit."""
    try:
        index_path = resolve_index_path(index, SETTINGS.indexes_root)
    except FileNotFoundError as exc:
        return f"Error: {exc}\n"

    if not is_valid_sql_query(sql_query, dialect="sqlite"):
        return "Error: invalid SQL syntax.\n"

    match_table = re.search(r"\bFROM\b\s+(\w+)", sql_query, re.IGNORECASE)
    if match_table is None:
        return "Error: could not determine table from query.\n"

    table_found = match_table.group(1)
    if table_found not in SCHEMA_REGISTRY:
        available = list(SCHEMA_REGISTRY.keys())
        return f"Error: unknown table '{table_found}'. Available tables: {available}\n"

    bad_cols = validate_query_columns(sql_query, table_found, SCHEMA_REGISTRY)
    if bad_cols:
        valid_cols = [c["name"] for c in SCHEMA_REGISTRY[table_found]]
        return (
            f"Error: unknown column(s) {bad_cols}. "
            f"Columns available in '{table_found}': {valid_cols}\n"
        )

    result = subprocess.run(
        [
            str(SETTINGS.gufi_executable),
            "-d", "\t",
            "-E", sql_query,
            str(index_path) + os.sep,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return f"Error: gufi_query failed: {result.stderr}\n"

    lines = result.stdout.splitlines()
    if return_limit > 0:
        lines = lines[:return_limit]
    return "\n".join(lines)


@mcp.tool()
def gufi_client_ls(index: str, arguments: str = "", client_config: str = "") -> str:
    """List entries in a GUFI index via the remote gufi_ls client wrapper."""
    return _run_gufi_client("ls", index, arguments, client_config)


@mcp.tool()
def gufi_client_du(index: str, arguments: str = "", client_config: str = "") -> str:
    """Summarize disk usage for a GUFI index via the remote gufi_du client wrapper."""
    return _run_gufi_client("du", index, arguments, client_config)


@mcp.tool()
def gufi_client_find(index: str, arguments: str = "", client_config: str = "") -> str:
    """Find paths in a GUFI index via the remote gufi_find client wrapper."""
    return _run_gufi_client("find", index, arguments, client_config)


@mcp.tool()
def gufi_client_stat(index: str, arguments: str = "", client_config: str = "") -> str:
    """Stat entries in a GUFI index via the remote gufi_stat client wrapper."""
    return _run_gufi_client("stat", index, arguments, client_config)


@mcp.tool()
def gufi_client_stats(index: str, arguments: str = "", client_config: str = "") -> str:
    """Run canned gufi_stats queries via the remote client wrapper."""
    return _run_gufi_client("stats", index, arguments, client_config)


@mcp.tool()
def gufi_client_getfattr(index: str, arguments: str = "", client_config: str = "") -> str:
    """Read extended attributes via the remote gufi_getfattr client wrapper."""
    return _run_gufi_client("getfattr", index, arguments, client_config)


@mcp.tool()
def gufi_client_query(index: str, arguments: str = "", client_config: str = "") -> str:
    """Run gufi_query on the server via SSH. Pass gufi_query flags in arguments."""
    return _run_gufi_client("query", index, arguments, client_config)


@mcp.prompt()
def find_biggest_files(index: str) -> str:
    """Prompt an agent to find the largest files in a GUFI index."""
    target = index.strip() or SETTINGS.default_index
    return (
        f"Use the GUFI tools to find the largest files in index '{target}'. "
        "Start with gufi_query_local_index or gufi_client_find as appropriate."
    )


@mcp.resource("gufi://indexes")
def gufi_indexes() -> dict[str, str]:
    """List GUFI indexes discovered under GUFI_INDEXES_ROOT."""
    indexes: dict[str, str] = {}
    index_root = SETTINGS.indexes_root

    if index_root.is_dir():
        for entry in index_root.iterdir():
            if not entry.is_dir():
                continue
            db_path = entry / "db.db"
            if db_path.is_file():
                indexes[entry.name] = str(entry) + os.sep

    return indexes


@mcp.resource("gufi://schemas/{schema}")
def gufi_schemas_search(schema: str = "query_surfaces") -> dict:
    """
    Discover GUFI table/view schemas.

    schema='query_surfaces' returns descriptions of all query surfaces.
    schema=<table_name> returns column definitions for that table or view.
    """
    with open(SETTINGS.schema_file, "r", encoding="utf-8") as file:
        data = json.load(file)

    if schema == "query_surfaces":
        return data.get("query_surfaces", {})

    if schema in SCHEMA_REGISTRY:
        return {schema: SCHEMA_REGISTRY[schema]}

    return {
        "error": (
            f"Schema '{schema}' not found. "
            "Use 'query_surfaces' to list available tables."
        )
    }


if __name__ == "__main__":
    mcp.run(
        transport=SETTINGS.mcp_transport,
        host=SETTINGS.mcp_server_host,
        port=SETTINGS.mcp_server_port,
    )



# OLD STUFF - MOSTLY EXTRACIRRICULAR



'''
@mcp.tool()
def gufi_schema_columns(table: str) -> list[ColumnDef]:
    """Return the column names and types for a GUFI table or view. Call this before writing a query to confirm valid column names."""
    if table not in SCHEMA_REGISTRY:
        available = list(SCHEMA_REGISTRY.keys())
        return [ColumnDef(name="error", type=f"Unknown table '{table}'. Available: {available}")]
    return SCHEMA_REGISTRY[table]

@mcp.tool()
def gufi_schema_tables() -> list[str]:
    """List all queryable GUFI tables and views known to the schema registry."""
    return list(SCHEMA_REGISTRY.keys())

@mcp.tool()
def gufi_plot_analytics(
    title: str,
    plot_type: str,
    labels: list[str],
    values: list[float],
    x_label: str = "",
    y_label: str = "",
    y_range: list[float] | None = None,
    filename: str = "",
) -> PlotResult:
    """
    Generate a matplotlib plot from analytics data and return the file path and
    a base64-encoded PNG.

    plot_type: 'bar' (vertical bars), 'barh' (horizontal bars), 'pie', or 'line'.
    labels: category names (x-axis ticks, pie slice labels, or barh y-axis labels).
    values: one numeric value per label.
    y_range: optional [min, max] to fix the y-axis scale.
    filename: optional output file stem; auto-generated from title + timestamp if omitted.
    """
    valid_types = {"bar", "barh", "pie", "line"}
    if plot_type not in valid_types:
        return PlotResult(
            file_path="", image_b64="",
            plot_type=plot_type,
            title=f"Error: unsupported plot_type '{plot_type}'. Use one of {sorted(valid_types)}."
        )

    if len(labels) != len(values):
        return PlotResult(
            file_path="", image_b64="",
            plot_type=plot_type,
            title=f"Error: labels ({len(labels)}) and values ({len(values)}) must be the same length."
        )

    PLOT_DIR.mkdir(parents=True, exist_ok=True)

    # Build output path
    if filename:
        stem = re.sub(r"[^\w\-]", "_", filename)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = re.sub(r"[^\w\-]", "_", title) + f"_{ts}"
    out_path = PLOT_DIR / f"{stem}.png"

    fig, ax = plt.subplots(figsize=(10, 6))
    try:
        if plot_type == "bar":
            ax.bar(labels, values)
            if y_range:
                ax.set_ylim(y_range)
        elif plot_type == "barh":
            ax.barh(labels, values)
            if y_range:
                ax.set_xlim(y_range)
        elif plot_type == "pie":
            ax.pie(values, labels=labels, autopct="%1.1f%%")
        elif plot_type == "line":
            ax.plot(labels, values, marker="o")
            if y_range:
                ax.set_ylim(y_range)

        ax.set_title(title)
        if x_label and plot_type != "pie":
            ax.set_xlabel(x_label)
        if y_label and plot_type != "pie":
            ax.set_ylabel(y_label)

        plt.tight_layout()
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
    finally:
        plt.close(fig)

    buf = io.BytesIO()
    with open(out_path, "rb") as f:
        buf.write(f.read())
    image_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    return PlotResult(
        file_path=str(out_path),
        image_b64=image_b64,
        plot_type=plot_type,
        title=title,
    )

@mcp.tool()
def gufi_query_find_largest_files(index: str, return_count: int) -> list[FileEntry]:
    """Find the largest files in a GUFI index. Returns the top return_count files sorted by size descending."""
    index_root = Path(GUFI_INDEXES_ROOT + index)

    if not index_root.is_dir():
        return []

    # Select largest file in each subdir
    result = subprocess.run(
        [GUFI_EXE, "-d", "\t", "-E",
         "SELECT name, size, uid FROM vrpentries ORDER BY size DESC",
         f"{GUFI_INDEXES_ROOT}{index_root.name}/"],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        return []

    # Split entries
    rows: list[FileEntry] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            # Append to a structured format
            try:
                rows.append(FileEntry(
                    name=parts[0],
                    size=int(parts[1]),
                    uid=parts[2] if len(parts) > 2 else ""
                ))
            except ValueError:
                continue

    # Sort rows and return by how many requested
    rows.sort(key=lambda r: r["size"], reverse=True)
    return rows[:return_count]



@mcp.tool()
def gufi_subtree_analytics(
    idx_path: str,
    depth: int = 0,
    target_uid: int = 0
) -> SubtreeAnalytics:
    """
    Compute high-level aggregated metrics for a directory subtree in a GUFI index.

    idx_path: index name (e.g. 'home_index') or 'index_name/sub/idx_path' for a subtree.
    depth: max depth below the target path; 0 = unlimited.
    target_uid: if set, filters entry-level metrics to that UID only.

    NOTE: For rolled-up indexes, starting from a subtree path may yield incomplete
    results since child entries may be consolidated into parent db.db files. Use a
    full index path or a non-rolled-up index for guaranteed completeness.
    """
    warnings: list[str] = []
    t_start = time.perf_counter()

    _empty = SubtreeAnalytics(
        query_metadata=QueryMetadata(
            target_path=idx_path, total_files_in_scope=0,
            is_fully_rolled_up=False, treesummary_available=False,
            execution_time_ms=0.0, warnings=[]
        ),
        summary_totals=SummaryTotals(
            total_bytes=0, total_bytes_human="0 B",
            total_files=0, total_directories=0, total_symlinks=0
        ),
        size_distribution_histogram=SizeHistogram(
            under_1mb    =HistogramBucket(count=0, total_bytes=0),
            mb_1_to_100mb=HistogramBucket(count=0, total_bytes=0),
            mb_100_to_1gb=HistogramBucket(count=0, total_bytes=0),
            gb_1_to_100gb=HistogramBucket(count=0, total_bytes=0),
            over_100gb   =HistogramBucket(count=0, total_bytes=0),
        ),
        age_and_staleness=AgeAndStaleness(
            active_under_30_days=AgeBand(bytes=0, percentage=0.0),
            warm_30_to_180_days =AgeBand(bytes=0, percentage=0.0),
            cold_180_plus_days  =AgeBand(bytes=0, percentage=0.0),
            oldest_file_mtime="", newest_file_mtime=""
        ),
        top_extensions_by_size=[],
        top_owners=[]
    )

    # --- Path resolution ---
    try:
        _index_name, start_dir = resolve_gufi_path(idx_path)
    except ValueError as e:
        _empty["query_metadata"]["warnings"] = [str(e)]
        return _empty

    start_path = str(start_dir) + "/"

    # --- treesummary detection and optional build ---
    has_treesummary = check_treesummary(start_dir)
    if not has_treesummary:
        if build_treesummary(start_dir):
            has_treesummary = check_treesummary(start_dir)

    # depth_clause_s: for -S queries against summary (column is 'depth')
    # depth_clause_e: for -E queries against vrpentries (column is 'ddepth', the d-prefixed alias)
    depth_clause_s = f" AND depth <= {depth}"  if depth > 0 else ""
    depth_clause_e = f" AND ddepth <= {depth}" if depth > 0 else ""
    uid_clause     = f" AND uid = {target_uid}" if target_uid is not None else ""

    # -T SQL prunes entire branches beyond max depth when treesummary is available
    tree_sql_arg = ["-T", f"SELECT inode FROM treesummary WHERE depth <= {depth}"] \
        if has_treesummary and depth > 0 else []

    def run_gufi(flag: str, sql: str) -> list[str]:
        cmd = [GUFI_EXE, "-d", "\t"] + tree_sql_arg + [flag, sql, start_path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            warnings.append(f"gufi_query error ({flag}): {r.stderr.strip()}")
            return []
        return r.stdout.splitlines()

    # --- Query 1: summary totals + rollup check via summary (-S) ---
    summary_sql = (
        f"SELECT SUM(totfiles), SUM(totlinks), COUNT(*), SUM(totsize), "
        f"SUM(CASE WHEN isrolledup=0 THEN 1 ELSE 0 END) "
        f"FROM summary WHERE rectype=0{depth_clause_s}"
    )
    totfiles = totlinks = totdirs = totsize = non_rolled = 0
    for row in run_gufi("-S", summary_sql):
        parts = row.split("\t")
        if len(parts) == 5:
            try:
                totfiles   += int(parts[0] or 0)
                totlinks   += int(parts[1] or 0)
                totdirs    += int(parts[2] or 0)
                totsize    += int(parts[3] or 0)
                non_rolled += int(parts[4] or 0)
            except ValueError:
                continue

    is_fully_rolled_up = (non_rolled == 0)
    if not is_fully_rolled_up and len(Path(idx_path).parts) > 1:
        warnings.append(
            "Index is not fully rolled up. Querying from a subtree path on a "
            "partially rolled-up index may produce incomplete results."
        )

    # --- Query 2: size distribution histogram via vrpentries (-E) ---
    MB = 1024 * 1024
    GB = 1024 * MB
    hist_sql = (
        f"SELECT "
        f"SUM(CASE WHEN size < {MB} THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN size < {MB} THEN size ELSE 0 END), "
        f"SUM(CASE WHEN size >= {MB}     AND size < {100*MB} THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN size >= {MB}     AND size < {100*MB} THEN size ELSE 0 END), "
        f"SUM(CASE WHEN size >= {100*MB} AND size < {GB}     THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN size >= {100*MB} AND size < {GB}     THEN size ELSE 0 END), "
        f"SUM(CASE WHEN size >= {GB}     AND size < {100*GB} THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN size >= {GB}     AND size < {100*GB} THEN size ELSE 0 END), "
        f"SUM(CASE WHEN size >= {100*GB} THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN size >= {100*GB} THEN size ELSE 0 END) "
        f"FROM vrpentries WHERE 1=1{depth_clause_e}{uid_clause}"
    )
    h = [0] * 10
    for row in run_gufi("-E", hist_sql):
        parts = row.split("\t")
        if len(parts) == 10:
            try:
                h = [h[i] + int(parts[i] or 0) for i in range(10)]
            except ValueError:
                continue

    size_histogram = SizeHistogram(
        under_1mb    =HistogramBucket(count=h[0], total_bytes=h[1]),
        mb_1_to_100mb=HistogramBucket(count=h[2], total_bytes=h[3]),
        mb_100_to_1gb=HistogramBucket(count=h[4], total_bytes=h[5]),
        gb_1_to_100gb=HistogramBucket(count=h[6], total_bytes=h[7]),
        over_100gb   =HistogramBucket(count=h[8], total_bytes=h[9]),
    )

    # --- Query 3: age and staleness ---
    now_expr = "CAST(strftime('%s', 'now') AS INTEGER)"
    day = 86400
    age_sql = (
        f"SELECT "
        f"SUM(CASE WHEN ({now_expr} - mtime) < {30*day}  THEN size ELSE 0 END), "
        f"SUM(CASE WHEN ({now_expr} - mtime) >= {30*day}  AND ({now_expr} - mtime) < {180*day} THEN size ELSE 0 END), "
        f"SUM(CASE WHEN ({now_expr} - mtime) >= {180*day} THEN size ELSE 0 END), "
        f"MIN(mtime), MAX(mtime) "
        f"FROM vrpentries WHERE size > 0{depth_clause_e}{uid_clause}"
    )
    active_bytes = warm_bytes = cold_bytes = 0
    oldest_epoch = newest_epoch = 0
    for row in run_gufi("-E", age_sql):
        parts = row.split("\t")
        if len(parts) == 5:
            try:
                active_bytes += int(parts[0] or 0)
                warm_bytes   += int(parts[1] or 0)
                cold_bytes   += int(parts[2] or 0)
                mn = int(parts[3] or 0)
                mx = int(parts[4] or 0)
                if oldest_epoch == 0 or (mn > 0 and mn < oldest_epoch):
                    oldest_epoch = mn
                if mx > newest_epoch:
                    newest_epoch = mx
            except ValueError:
                continue

    total_age_bytes = active_bytes + warm_bytes + cold_bytes or 1

    def epoch_to_iso(epoch: int) -> str:
        if epoch <= 0:
            return ""
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    age_staleness = AgeAndStaleness(
        active_under_30_days=AgeBand(bytes=active_bytes, percentage=round(active_bytes / total_age_bytes * 100, 2)),
        warm_30_to_180_days =AgeBand(bytes=warm_bytes,   percentage=round(warm_bytes   / total_age_bytes * 100, 2)),
        cold_180_plus_days  =AgeBand(bytes=cold_bytes,   percentage=round(cold_bytes   / total_age_bytes * 100, 2)),
        oldest_file_mtime=epoch_to_iso(oldest_epoch),
        newest_file_mtime=epoch_to_iso(newest_epoch),
    )

    # --- Query 4: top extensions by size ---
    # REPLACE trick: find last '.' by reversing the string via REPLACE/INSTR/CHAR(0)
    ext_sql = (
        f"SELECT "
        f"CASE WHEN INSTR(name, '.') > 0 "
        f"     THEN LOWER(SUBSTR(name, LENGTH(name) - INSTR(REPLACE(name, '.', CHAR(0)), CHAR(0)) + 2)) "
        f"     ELSE '' END AS ext, "
        f"COUNT(*), SUM(size) "
        f"FROM vrpentries "
        f"WHERE type = 'f'{depth_clause_e}{uid_clause} "
        f"GROUP BY ext ORDER BY SUM(size) DESC LIMIT 10"
    )
    ext_accum: dict[str, list[int]] = {}
    for row in run_gufi("-E", ext_sql):
        parts = row.split("\t")
        if len(parts) == 3:
            try:
                ext = parts[0]
                if ext not in ext_accum:
                    ext_accum[ext] = [0, 0]
                ext_accum[ext][0] += int(parts[1] or 0)
                ext_accum[ext][1] += int(parts[2] or 0)
            except ValueError:
                continue

    top_extensions: list[ExtensionEntry] = [
        ExtensionEntry(extension=ext, count=v[0], total_bytes=v[1])
        for ext, v in sorted(ext_accum.items(), key=lambda kv: kv[1][1], reverse=True)[:10]
    ]

    # --- Query 5: top owners ---
    owner_sql = (
        f"SELECT uid, gid, COUNT(*), SUM(size) "
        f"FROM vrpentries "
        f"WHERE type = 'f'{depth_clause_e}{uid_clause} "
        f"GROUP BY uid, gid ORDER BY SUM(size) DESC LIMIT 10"
    )
    owner_accum: dict[tuple[int, int], list[int]] = {}
    for row in run_gufi("-E", owner_sql):
        parts = row.split("\t")
        if len(parts) == 4:
            try:
                key = (int(parts[0] or 0), int(parts[1] or 0))
                if key not in owner_accum:
                    owner_accum[key] = [0, 0]
                owner_accum[key][0] += int(parts[2] or 0)
                owner_accum[key][1] += int(parts[3] or 0)
            except ValueError:
                continue

    top_owners: list[OwnerEntry] = [
        OwnerEntry(uid=k[0], gid=k[1], file_count=v[0], total_bytes=v[1])
        for k, v in sorted(owner_accum.items(), key=lambda kv: kv[1][1], reverse=True)[:10]
    ]

    elapsed_ms = (time.perf_counter() - t_start) * 1000

    return SubtreeAnalytics(
        query_metadata=QueryMetadata(
            target_path=str(start_dir),
            total_files_in_scope=totfiles,
            is_fully_rolled_up=is_fully_rolled_up,
            treesummary_available=has_treesummary,
            execution_time_ms=round(elapsed_ms, 2),
            warnings=warnings,
        ),
        summary_totals=SummaryTotals(
            total_bytes=totsize,
            total_bytes_human=format_bytes(totsize),
            total_files=totfiles,
            total_directories=totdirs,
            total_symlinks=totlinks,
        ),
        size_distribution_histogram=size_histogram,
        age_and_staleness=age_staleness,
        top_extensions_by_size=top_extensions,
        top_owners=top_owners,
    )

@mcp.tool()
def gufi_check_index_health(
    idx_path: str,
    source_path: str | None = None,
    sample_size: int = 500,
    max_depth: int = 0,
    check_shards: bool = True,
    shard_check_depth: int = 3,
    max_shard_issues: int = DEFAULT_MAX_SHARD_ISSUES,
) -> IndexHealthResult:
    """
    Assess GUFI index health: age, sampled directory drift, and shard integrity.

    idx_path: index name (e.g. 'home_index') or 'index_name/sub/path' for a subtree.
    source_path: optional override for the live source root (default: info.db src).
    sample_size: number of directories to sample for POSIX vs GUFI mtime drift (0 to skip).
    max_depth: max directory depth for drift sampling; 0 = unlimited.
    check_shards: when True, audit expected vs accessible shards under the target subtree.
    shard_check_depth: max depth (relative to target) for shard integrity checks.
    max_shard_issues: cap on returned shard issue entries; full totals are in shard_counts.

    Freshness (FRESH / DRIFTED / STALE) is derived from index age and drift ratio.
    Shard terms:
      - metadata_expected: child dirs GUFI says should have db.db shards
      - accessible: db.db files that open cleanly with required tables
      - missing_shard: expected shard absent or invalid path in index tree
      - corrupt_shard: db.db present but fails SQLite validation
      - unindexed_path: live source directory with no accessible index shard
    """
    t_start = time.perf_counter()
    warnings: list[str] = []

    def _empty_result() -> IndexHealthResult:
        return IndexHealthResult(
            target_path=idx_path,
            source_path=None,
            freshness="STALE",
            index_age_hours=0.0,
            last_indexed="",
            sampling=empty_sampling_stats(),
            shard_counts=empty_shard_counts(),
            shard_issues=[],
            warnings=warnings,
            execution_time_ms=round((time.perf_counter() - t_start) * 1000, 2),
        )

    try:
        index_name, start_dir = resolve_gufi_path(idx_path)
    except ValueError as e:
        warnings.append(str(e))
        return _empty_result()

    parts = Path(idx_path).parts
    subpath = Path(*parts[1:]) if len(parts) > 1 else Path()

    if not (start_dir / "db.db").is_file():
        warnings.append(f"No db.db at target path '{start_dir}'")
        return _empty_result()

    age_hours, last_indexed, age_warnings = compute_index_age(start_dir, index_name)
    warnings.extend(age_warnings)

    source_root, src_warnings = resolve_source_path(index_name, subpath, source_path)
    warnings.extend(src_warnings)

    start_path = str(start_dir) + "/"
    depth_clause = f" AND depth <= {max_depth}" if max_depth > 0 else ""

    sampling = empty_sampling_stats()
    if source_root and sample_size > 0:
        sql = (
            f"SELECT name, mtime FROM vsummarydir "
            f"WHERE type = 'd'{depth_clause} "
            f"ORDER BY RANDOM() LIMIT {sample_size}"
        )
        cmd = [GUFI_EXE, "-d", "\t", "-S", sql, start_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            warnings.append(f"gufi_query drift sampling failed: {result.stderr.strip()}")
        else:
            sampled: list[tuple[str, int]] = []
            for row in result.stdout.splitlines():
                parts_row = row.split("\t")
                if len(parts_row) >= 2:
                    try:
                        sampled.append((parts_row[0], int(parts_row[1] or 0)))
                    except ValueError:
                        continue
            sampling = measure_drift(sampled, source_root)
    elif not source_root:
        warnings.append("source path unavailable; drift check skipped")

    shard_counts = empty_shard_counts()
    shard_issues: list[ShardIssue] = []
    if check_shards:
        shard_counts, shard_issues, shard_warnings = audit_shard_integrity(
            start_dir, source_root, shard_check_depth, max_shard_issues
        )
        warnings.extend(shard_warnings)

        total_expected = shard_counts["metadata_expected"] or 1
        bad_shards = (
            shard_counts["missing"]
            + shard_counts["corrupt"]
            + shard_counts["unindexed"]
        )
        if bad_shards > 0 and bad_shards / total_expected > 0.05:
            warnings.append("shard integrity issues detected (>5% of expected shards affected)")

    drift_ratio = sampling["drift_ratio"] if source_root and sample_size > 0 else 0.0
    freshness = classify_freshness(age_hours, drift_ratio)

    return IndexHealthResult(
        target_path=idx_path,
        source_path=str(source_root) if source_root else None,
        freshness=freshness,
        index_age_hours=round(age_hours, 2),
        last_indexed=last_indexed,
        sampling=sampling,
        shard_counts=shard_counts,
        shard_issues=shard_issues,
        warnings=warnings,
        execution_time_ms=round((time.perf_counter() - t_start) * 1000, 2),
    )


'''
