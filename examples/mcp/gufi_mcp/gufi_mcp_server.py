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

from mcp.server import MCPServer
import sqlite3
import subprocess
from typing import Any, Annotated, Literal
from pydantic import Field
import os
from dotenv import load_dotenv
from gufi_mcp_util import GufiQueryResult, GufiToolResult, GufiQuery
import gufi_mcp_util as util

load_dotenv()

SCHEMAFILE = os.getenv('SCHEMAFILE')
REMOTEHOST = os.getenv('REMOTEHOST')
MCPTRANSPORT = os.getenv('MCPTRANSPORT')
MCPSRVHOST = os.getenv('MCPSRVHOST')
MCPSRVPORT = os.getenv('MCPSRVPORT')
GUFIVTLIB = os.getenv('GUFIVTLIB')
GUFI_INDEX_ROOT = os.getenv('GUFI_INDEX_ROOT')
GUFI_QUERY = os.getenv('GUFI_QUERY')

mcp = MCPServer(name="gufi-mcp")

EVENT_LOGGER = EventLogger.from_env()
attach_event_logging(mcp, EVENT_LOGGER)

util.warm_tool_help_cache()

_GUFI_FIND_DESC = util.format_tool_description(
    "Find files in a GUFI index by name, type, modification age, or size.",
    "gufi_find",
    '{"index": "work", "type": "f", "mtime": "+7", "limit": 50}',
)
_GUFI_LS_DESC = util.format_tool_description(
    "List entries under an index path (GUFI ls wrapper).",
    "gufi_ls",
    '{"index": "personal_data", "subpath": "Videos", "long_format": true, "human_readable": true}',
)
_GUFI_DU_DESC = util.format_tool_description(
    "Disk-usage summary for an index path. Requires treesummary on the target.",
    "gufi_du",
    '{"index": "personal_data", "human_readable": true}',
)
_GUFI_STAT_DESC = util.format_tool_description(
    "Stat one file under a GUFI index.",
    "gufi_stat",
    '{"index": "personal_data", "file": "Mail/example.mbox"}',
)
_GUFI_STATS_DESC = util.format_tool_description(
    "Run a canned GUFI statistic (for example leaf-dirs, total-files).",
    "gufi_stats",
    '{"index": "personal_data", "stat": "leaf-dirs", "recursive": true, "num_results": 10}',
)
_GUFI_GETFATTR_DESC = util.format_tool_description(
    "Read extended attributes for a path under a GUFI index.",
    "gufi_getfattr",
    '{"index": "personal_data", "path": "Videos", "recursive": false}',
)

@mcp.tool()
def gufi_version() -> str:
    """gufi_query -- version"""
    result = subprocess.run(["gufi_query", "--version"], capture_output=True, text=True)
    return str(result.stdout)

@mcp.tool()
def gufi_location() -> str:
    """gufi_query location"""
    result = subprocess.run(["which", "gufi_query"], capture_output=True, text=True)
    return str(result.stdout)

@mcp.tool()
def sql_file_index(
        sqlin: Annotated[str, Field(description="One SQL SELECT against a GUFI view (vrpentries, pentries, vrsummary, summary, etc.). Prefer views over base tables. Returns shard-local rows; ORDER BY, LIMIT, GROUP BY, and aggregates are applied per subtree, not index-wide. Use aggregate_sql_query for global ordering or totals.")],
        index: Annotated[str, Field(description="Index / subpath to query. This will be resolved internally so the name obtained from the gufi_indexes resource should be used.")],
        remote: Annotated[bool, Field(description="Boolean for if this is a remote connection. Leave untouched unless user specifies otherwise. Config will be handled before invokation if remote is needed.")] = False
) -> GufiQueryResult:
    """Run a direct SQL SELECT against gufi_vt. Results are shard-local."""

    query_result = GufiQueryResult(execution_mode="shard_local")

    try:
        if remote:
            searchpath = util.resolve_remote_index(index)
            threads = os.cpu_count() or 1
            vt_config = [
                util.sqlite_string(searchpath),
                f"threads={threads}",
                "min_level=0",
                "max_level=99",
                "verbose=0",
                "remote_cmd='ssh'",
                f"remote_arg={util.sqlite_string(REMOTEHOST)}",
            ]
        else:
            searchpath = util.resolve_local_index(index)
            threads = os.cpu_count() or 1
            vt_config = [
                util.sqlite_string(searchpath),
                f"threads={threads}",
                "min_level=1",
                "max_level=99",
                "verbose=0",
            ]
    except RuntimeError as exc:
        query_result.error = str(exc)
        return query_result.model_dump()

    logical_sql = util.normalize_gufi_query_sql(sqlin.strip())
    util.apply_shard_local_warnings(query_result, logical_sql)

    stage = util.gufi_query_stage_from_sql(logical_sql)
    if not stage:
        query_result.error = "Could not find a supported GUFI table in the SQL FROM clause."
        query_result.sql = logical_sql
        return query_result.model_dump()

    vt_config.append(f'{stage}={util.sqlite_string(util.ensure_sql_statement(logical_sql), '"')}')
    create_sql = f"""
        CREATE VIRTUAL TABLE temp.gufi
        USING gufi_vt({", ".join(vt_config)})
    """
    query_result.sql = create_sql
    query_result.compiled_sql = create_sql

    try:
        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        conn.load_extension(GUFIVTLIB)
        conn.enable_load_extension(False)

        cursor = conn.cursor()
        cursor.execute(create_sql)
        cursor.execute("SELECT * FROM temp.gufi")
        result = cursor.fetchall()

        query_result.rows = [list(res_row) for res_row in result]
        query_result.columns = [description[0] for description in cursor.description]
        query_result.row_count = len(query_result.rows)
        return query_result.model_dump()

    except sqlite3.Error as e:
        query_result.error = str(e)
        return query_result.model_dump()

    finally:
        try:
            conn.execute("DROP TABLE IF EXISTS temp.gufi")
        except (NameError, sqlite3.Error):
            pass
        try:
            conn.close()
        except (NameError, sqlite3.Error):
            pass

@mcp.tool()
def aggregate_sql_query(
        query: Annotated[GufiQuery, Field(description="GUFI aggregate pipeline object. Use -I, -E, -K, -J, -G for index-wide merge, sort, and totals. Put global ORDER BY/LIMIT in -G, not -E or -F.")],
) -> GufiQueryResult:
    """Run GUFI aggregate SQL phases through gufi_vt for index-wide results."""

    query_result = GufiQueryResult(execution_mode="aggregate")
    normalized_query = util.normalize_aggregate_query(query)

    if not util.subpath_exists(normalized_query.index):
        query_result.error = f"Error: Index or subpath {normalized_query.index} does not exist"
        return query_result.model_dump()

    valid, validation_error = util.validate_aggregate_order(normalized_query)
    if not valid:
        query_result.error = validation_error
        return query_result.model_dump()

    options = [util.sqlite_string(util.resolve_query_index(normalized_query.index))]
    for config in normalized_query.config:
        options.append(config)
    for item in normalized_query.sql_options:
        option = item.option.lstrip("-")
        sql = util.ensure_sql_statement(item.sql)
        options.append(f"{option}={util.sqlite_string(sql, '"')}")

    sql_query = f"""
        CREATE VIRTUAL TABLE temp.gufi
        USING gufi_vt({", ".join(options)})
    """
    query_result.sql = sql_query
    query_result.compiled_sql = sql_query

    conn = sqlite3.connect(":memory:")
    try:
        conn.enable_load_extension(True)
        conn.load_extension(GUFIVTLIB)
        conn.enable_load_extension(False)

        cursor = conn.cursor()
        cursor.execute(sql_query)
        cursor.execute("SELECT * FROM temp.gufi")
        result = cursor.fetchall()

        query_result.rows = [list(res_row) for res_row in result]
        query_result.columns = [description[0] for description in cursor.description]
        query_result.row_count = len(query_result.rows)
        return query_result.model_dump()

    except sqlite3.Error as e:
        query_result.error = str(e)
        return query_result.model_dump()

    finally:
        try:
            conn.execute("DROP TABLE IF EXISTS temp.gufi")
        except (NameError, sqlite3.Error):
            pass
        try:
            conn.close()
        except (NameError, sqlite3.Error):
            pass

@mcp.tool(description=_GUFI_LS_DESC)
def gufi_ls(
        index: Annotated[str, Field(description="GUFI index name from gufi://indexes (for example personal_data).")],
        subpath: Annotated[str, Field(description="Optional subdirectory under the index root.")] = None,
        long_format: Annotated[bool, Field(description="Pass -l for long listing format.")] = False,
        human_readable: Annotated[bool, Field(description="Pass -h for human-readable sizes.")] = False,
        recursive: Annotated[bool, Field(description="Pass -R for recursive listing.")] = False,
        extra_flags: Annotated[list[str], Field(description="Rare extra CLI flags only; prefer named parameters.")] = None,
) -> GufiToolResult:
    ''' List entries under a GUFI index path. '''
    try:
        cmd = util.build_ls_argv(
            index,
            subpath=subpath,
            long_format=long_format,
            human_readable=human_readable,
            recursive=recursive,
            extra_flags=extra_flags,
        )
    except RuntimeError as exc:
        return GufiToolResult(error=str(exc)).model_dump()
    return util.pack_command_result(util.run_gufi_cli(cmd))


@mcp.tool(description=_GUFI_DU_DESC)
def gufi_du(
        index: Annotated[str, Field(description="GUFI index name from gufi://indexes.")],
        subpath: Annotated[str, Field(description="Optional subdirectory; must have treesummary for results.")] = None,
        human_readable: Annotated[bool, Field(description="Pass -h for human-readable sizes.")] = False,
        summarize: Annotated[bool, Field(description="Pass -s to summarize totals only.")] = False,
        extra_flags: Annotated[list[str], Field(description="Rare extra CLI flags only; prefer named parameters.")] = None,
) -> GufiToolResult:
    ''' Disk-usage summary for a GUFI index path. '''
    try:
        cmd = util.build_du_argv(
            index,
            subpath=subpath,
            human_readable=human_readable,
            summarize=summarize,
            extra_flags=extra_flags,
        )
    except RuntimeError as exc:
        return GufiToolResult(error=str(exc)).model_dump()

    result = util.run_gufi_cli(cmd)
    if result.stderr and "have treesummary data?" in result.stderr:
        target = util.resolve_index_path(index, subpath)
        return GufiToolResult(
            rows=[],
            row_count=0,
            stderr=result.stderr.strip(),
            returncode=result.returncode,
            error=f"Treesummary error: {target} does not have treesummary; use SQL or aggregate_sql_query instead.",
        ).model_dump()
    return util.pack_command_result(result)


@mcp.tool(description=_GUFI_FIND_DESC)
def gufi_find(
        index: Annotated[str, Field(description="GUFI index name from gufi://indexes.")],
        subpath: Annotated[str, Field(description="Optional subdirectory under the index to search.")] = None,
        name: Annotated[str, Field(description="Filename glob for -name (for example organizer*).")] = None,
        type: Annotated[str, Field(description="Entry type for -type: f=file, d=directory, l=symlink. Default f.")] = "f",
        mtime: Annotated[str, Field(description="Modification age in days for -mtime. +N means older than N days (for example +7).")] = None,
        size: Annotated[str, Field(description="Size predicate for -size (for example +100M).")] = None,
        limit: Annotated[int, Field(description="Maximum rows to return (--num-results).")] = None,
        largest: Annotated[bool, Field(description="Sort by size descending (--largest).")] = False,
        extra_flags: Annotated[list[str], Field(description="Rare extra CLI flags only; prefer named parameters.")] = None,
) -> GufiToolResult:
    ''' Find files in a GUFI index. '''
    try:
        cmd = util.build_find_argv(
            index,
            subpath=subpath,
            name=name,
            type=type,
            mtime=mtime,
            size=size,
            limit=limit,
            largest=largest,
            extra_flags=extra_flags,
        )
    except RuntimeError as exc:
        return GufiToolResult(error=str(exc)).model_dump()
    return util.pack_command_result(util.run_gufi_cli(cmd))


@mcp.tool(description=_GUFI_STAT_DESC)
def gufi_stat(
        index: Annotated[str, Field(description="GUFI index name from gufi://indexes.")],
        file: Annotated[str, Field(description="File path relative to the index root, unless absolute.")],
        extra_flags: Annotated[list[str], Field(description="Rare extra CLI flags only; prefer named parameters.")] = None,
) -> GufiToolResult:
    ''' Stat one file under a GUFI index. '''
    try:
        cmd = util.build_stat_argv(index, file, extra_flags=extra_flags)
    except RuntimeError as exc:
        return GufiToolResult(error=str(exc)).model_dump()

    result = util.run_gufi_cli(cmd)
    if result.returncode != 0 and result.stderr and "No such file or directory" in result.stderr:
        return GufiToolResult(
            rows=[],
            row_count=0,
            stderr=result.stderr.strip(),
            returncode=result.returncode,
            error=f"File error: gufi_stat could not find {file} under index {index}.",
        ).model_dump()
    return util.pack_command_result(result)


@mcp.tool(description=_GUFI_STATS_DESC)
def gufi_stats(
        index: Annotated[str, Field(description="GUFI index name from gufi://indexes.")],
        stat: Annotated[str, Field(description="Statistic name (see tool description for choices).")],
        subpath: Annotated[str, Field(description="Optional subdirectory under the index.")] = None,
        recursive: Annotated[bool, Field(description="Pass -r for recursive stats.")] = False,
        num_results: Annotated[int, Field(description="Limit rows returned (--num-results).")] = None,
        extra_flags: Annotated[list[str], Field(description="Rare extra CLI flags only; prefer named parameters.")] = None,
) -> GufiToolResult:
    ''' Run a canned GUFI statistic on an index path. '''
    try:
        cmd = util.build_stats_argv(
            index,
            stat,
            subpath=subpath,
            recursive=recursive,
            num_results=num_results,
            extra_flags=extra_flags,
        )
    except RuntimeError as exc:
        return GufiToolResult(error=str(exc)).model_dump()

    result = util.run_gufi_cli(cmd)
    stderr = result.stderr.strip() if result.stderr else ""
    if result.returncode != 0:
        if "argument stat: invalid choice:" in stderr:
            return GufiToolResult(
                rows=[],
                row_count=0,
                stderr=stderr,
                returncode=result.returncode,
                error=f"Tool error: gufi_stats does not support statistic {stat!r}.",
            ).model_dump()
        if "No such file or directory" in stderr:
            target = util.resolve_index_path(index, subpath)
            return GufiToolResult(
                rows=[],
                row_count=0,
                stderr=stderr,
                returncode=result.returncode,
                error=f"Path error: gufi_stats could not find {target}.",
            ).model_dump()
    return util.pack_command_result(result)


@mcp.tool(description=_GUFI_GETFATTR_DESC)
def gufi_getfattr(
        index: Annotated[str, Field(description="GUFI index name from gufi://indexes.")],
        path: Annotated[str, Field(description="Path relative to the index root.")],
        recursive: Annotated[bool, Field(description="Pass -R to recurse.")] = False,
        extra_flags: Annotated[list[str], Field(description="Rare extra CLI flags only; prefer named parameters.")] = None,
) -> GufiToolResult:
    ''' Read extended attributes for a path under a GUFI index. '''
    try:
        cmd = util.build_getfattr_argv(index, path, recursive=recursive, extra_flags=extra_flags)
    except RuntimeError as exc:
        return GufiToolResult(error=str(exc)).model_dump()

    result = util.run_gufi_cli(cmd)
    if result.returncode != 0 and result.stderr and "No such file or directory" in result.stderr:
        target = util.resolve_index_path(index, path)
        return GufiToolResult(
            rows=[],
            row_count=0,
            stderr=result.stderr.strip(),
            returncode=result.returncode,
            error=f"Path error: gufi_getfattr could not find {target}.",
        ).model_dump()
    return util.pack_command_result(result)

# resource that returns indexes available
@mcp.resource("gufi://indexes")
def gufi_indexes() -> list[list[Any]]:
    ''' Access list of available gufi indexes, as well as a boolean for if treesummary is active in this index '''

    output = []
    indexes = util.get_gufi_indexes()
    for index in indexes:
        output.append([index, util.has_treesummary(index)])

    return output

@mcp.resource("gufi://tool-guide/{tool}")
def gufi_tool_guide(
        tool: Annotated[str, Field(description="Wrapper tool name: gufi_find, gufi_ls, gufi_du, gufi_stat, gufi_stats, or gufi_getfattr.")]
) -> str:
    ''' Return full cached CLI help for one command wrapper tool. '''
    normalized = tool.strip()
    if normalized not in util.WRAPPER_TOOLS:
        return f"Unknown tool {tool!r}. Valid names: {', '.join(util.WRAPPER_TOOLS)}"
    return util.get_tool_help(normalized)


@mcp.resource("gufi://schemas/{schema}")
def gufi_schemas(
        schema: Annotated[str, Field(description="After running this resource with no input, all tables will be available. Inputting a table to this variable will reflect schema information for that table.")] = "all"
) -> list[list[str]]:
    """ Return schemas of each gufi index table """

    # Get all tables for GUFI
    if schema == "all":
        sqlline = f'SELECT name, type FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC'
        result = util.execute_sql(sqlline, False)
        if result[0] != "sql error:":
            rows = [list(res_row) for res_row in result]
        else:
            raise RuntimeError(f"Error executing SQL: {result[1]}")
    # Get schema of a specific table
    else:
        sqlline = f"PRAGMA table_info(\"{schema}\")"
        result = util.execute_sql(sqlline, False)
        if result[0] != "sql error:":
            rows = [[res_row[1], res_row[2]] for res_row in result]
        else:
            raise RuntimeError(f"Error executing SQL: {result[1]}")

    return rows

#@mcp.resource("gufi://naive_indexes")
def naive_index_scheme() -> str:
    """
       sql query on local file information index
    """
    schemafile=SCHEMAFILE
    try:
        with open(schemafile, mode="r") as f:
            content = f.read()
        return content
    except FileNotFoundError:
        return "Schema file not found."


@mcp.resource("gufi://session-brief")
def gufi_session_briefing() -> str:

    # What is GUFI
    # How writing sql to it works
    # Importance and nuance of treesummary being available
    # Tools available
    # How to use tools
    # When to use a user tool vs sql query tool vs aggregate query tool
    # A user tool (ls, du, etc) is used for high level searches and quick analysis. Some need treesummary
    # An sql query is one level deeper, providing basic one line queries
    # An aggregate query is used when the agent cannot get a quick answer from the tool above. An example might be a sum of several joined columns.
    # A detailed explanation of the pipeline through I, TSE, J, K, G, F will need to be explained
    # It should also be explained that an agent does not need to use all tools to complete a user request, and simpler actions are preferred. 


    return """
You are working with a GUFI MCP server. GUFI is a parallel file system
indexer: it scans a file tree into SQLite databases so file metadata can be
searched quickly without walking the live file system for every question.

Prefer the simplest tool that can answer the user. You do not need to use every
tool in a session. Start with high-level GUFI tools for quick inspection, move
to SQL when the user needs a more specific query, and use aggregate SQL only
when a simple tool or one-line SQL query cannot answer the request cleanly.

Available context:
- Read gufi://indexes to list known indexes and whether each has treesummary.
- Read gufi://schemas/all to discover query surfaces, then gufi://schemas/{name}
  for one table or view (for example vrpentries, pentries, summary, vrsummary).
- Read gufi://session-brief for this guide. Resource URIs must include the
  gufi:// prefix; gufi_indexes and session-brief alone are not valid URIs.

Query surfaces: prefer views over base tables
- vrpentries: best default for file rows that need directory context. Includes
  file columns plus summary path context (sname, dname). Prefer this over entries.
- pentries: file metadata with parent inode fields. No path column; use path()
  when you need a filesystem path in SQL.
- vrsummary / vsummary* views: directory-level rolled-up metadata.
- summary / treesummary: directory rollup tables when available.
- entries: low-level base table. Avoid for routine agent queries, especially
  when paths are required. entries has no path column.

Before writing SQL, inspect gufi://schemas/vrpentries or gufi://schemas/pentries
instead of guessing column names from entries.

Tool guidance:
- gufi_ls, gufi_du, gufi_find, gufi_stat, and gufi_stats are user-facing GUFI
  command wrappers with structured parameters (index, subpath, name, mtime, etc.).
  Each tool schema includes usage and a copy-paste JSON example. Do not call tools
  solely to fetch help. For full CLI flag lists read gufi://tool-guide/{tool}.
  Some commands need treesummary (notably gufi_du).
- sql_file_index runs a direct SQL SELECT against gufi_vt. It returns
  shard-local rows: ORDER BY, LIMIT, GROUP BY, and aggregate functions are
  applied per GUFI subtree, not across the full index. Use it for filtered row
  scans without global ordering, or when per-shard results are enough. If the
  result includes warning, treat ORDER BY/LIMIT/GROUP BY as non-global.
- aggregate_sql_query creates a temporary gufi_vt virtual table and runs GUFI's
  aggregate pipeline for index-wide merge, totals, grouping, and global ORDER BY.
  Use it whenever the answer must be correct across the entire index. Pass the
  index name from gufi://indexes (for example personal_data), not a filesystem
  path unless you intentionally query an absolute index path.

Treesummary nuance:
treesummary exists only when an index has been rolled up. It stores precomputed
subtree summary data and can make directory-level totals very fast. Read
gufi://indexes first. If treesummary is false for an index, use gufi_du and
gufi_stats carefully and prefer SQL over summary/treesummary tables or an
aggregate pipeline.

Aggregate SQL pipeline:
- I initializes per-thread intermediate tables, usually with CREATE TABLE
  intermediate(...).
- T, S, and E are the per-tree, per-summary, and per-entry SQL phases. These
  usually INSERT rows into intermediate during aggregation. Use only the phases
  needed for the question.
- K initializes the final aggregate table, usually with CREATE TABLE
  aggregate(...).
- J merges each intermediate table into the aggregate table.
- G selects the final result rows from aggregate. Put global ORDER BY and LIMIT
  here. The MCP tool returns these rows as typed SQLite values.
- F is optional cleanup only. Do not use -F for final sorted output.

Critical rules:
- Do not put ORDER BY, LIMIT, or OFFSET in -E, -S, or -T.
- Do not use -F instead of -G for index-wide sorted or top-N answers.
- Prefer vrpentries over entries for file/path questions.
- Do not SELECT path FROM entries; that column does not exist.

Common task patterns:

Name-filtered file listing:
- Goal: find a small set of regular files matching a name pattern with path and
  size.
- Use gufi_find with index, name, type="f", and limit. Example:
  {"index": "personal_data", "name": "organizer*", "type": "f", "limit": 10}
- Or sql_file_index against vrpentries when find filters are insufficient.

Files by modification age (mtime):
- Goal: list or count files not modified recently (for example older than 7 days).
- Bounded listing with paths: gufi_find with mtime="+7" (GNU find: +N means
  strictly older than N days). Example:
  {"index": "work", "type": "f", "mtime": "+7", "limit": 50}
- Index-wide count or grouping: one aggregate_sql_query, not multiple probes.
  mtime in SQL is Unix epoch (INT64). Cutoff for N days ago:
  CAST(strftime('%s','now') AS INT64) - N*86400, or a precomputed epoch.
- Prefer vrpentries for (sname, dname, name) context in aggregates; use path()
  from pentries only when full absolute paths are required.
- Target at most 2 tool calls: gufi_find for samples plus one aggregate for
  index-wide count, or a single aggregate when only totals are needed.
- Aggregate recipes (build with sql_options -I/-E/-K/-J/-G):
  Count files with mtime before cutoff:
  - -E INSERT INTO intermediate SELECT 1 FROM vrpentries WHERE type='f' AND mtime < CUTOFF
  - -G SELECT SUM(count) AS file_count FROM aggregate
  List oldest files index-wide:
  - -E INSERT INTO intermediate SELECT sname, dname, name, mtime, size FROM vrpentries
    WHERE type='f' AND mtime < CUTOFF
  - -G SELECT ... FROM aggregate ORDER BY mtime ASC LIMIT 100

Directory or index summary:
- Goal: determine whether summary or treesummary exists and report directory-level
  totals or file counts.
- Read gufi://indexes first, then choose gufi_du, gufi_stats, summary-family
  views, or SQL depending on what the index supports.

Index-wide total bytes of regular files:
- Use aggregate_sql_query with -I, -E, -K, -J, -G.
- -E inserts raw file sizes from vrpentries or pentries where type = 'f'.
- -J reduces per-thread intermediate rows; -G returns the final total.

Index-wide grouped totals (for example by uid):
- Use aggregate_sql_query when grouping must be correct across the entire index.
- Insert raw rows in -E, merge or partial-aggregate in -J, finish grouping,
  ordering, and LIMIT in -G.

Example aggregate recipes:

Total byte size of regular files:
- -I CREATE TABLE intermediate(size INT64)
- -E INSERT INTO intermediate SELECT size FROM vrpentries WHERE type = 'f'
- -K CREATE TABLE aggregate(total INT64)
- -J INSERT INTO aggregate SELECT SUM(size) FROM intermediate
- -G SELECT SUM(total) FROM aggregate

Top UIDs by total regular-file bytes:
- -I CREATE TABLE intermediate(uid INT64, total_bytes INT64)
- -E INSERT INTO intermediate SELECT uid, size FROM vrpentries WHERE type = 'f'
- -K CREATE TABLE aggregate(uid INT64, total_bytes INT64)
- -J INSERT INTO aggregate SELECT uid, SUM(total_bytes) FROM intermediate GROUP BY uid
- -G SELECT uid, total_bytes FROM aggregate ORDER BY total_bytes DESC LIMIT 10

For aggregate_sql_query, pass SQL phases as sql_options with options like -I,
-E, -K, -J, and -G. Pass gufi_vt configuration such as threads=32 in config.
The delimiter option from gufi_query is not needed because gufi_vt returns typed
rows directly through SQLite.
"""

if __name__ == "__main__":
    # Run the server with HTTP transport
    mcp.run(transport=MCPTRANSPORT, host=MCPSRVHOST, port=MCPSRVPORT)
