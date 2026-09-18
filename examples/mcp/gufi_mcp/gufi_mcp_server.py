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
from gufi_mcp_event_logger import EventLogger, attach_event_logging

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

@mcp.tool()
def gufi_version() -> str:
    """gufi_query -- version"""
    result = subprocess.run(["gufi_query", "--version"], capture_output=True, text=True)
    return str(result.stdout)

#@mcp.tool()
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

@mcp.tool()
def gufi_ls(
        path: Annotated[str, Field(description="Optional subpath of gufi_ls. This path should have the desired index as the root, since the underlying tool resolves from a configured root.")] = None,
        options: Annotated[list[str], Field(description="Options to submit for gufi_ls. submit '--help' to view these options. Do not use --delim, the tool uses its own to give structured output. A value supplied after an option should be a new list entry.")] = None
) -> GufiToolResult:
    ''' Execute user-facing tool: gufi equivalent of ls '''

    cmd = ["gufi_ls"]
    help = False
    if options:
        for option in options:
            if option == "--help":
                help = True
            cmd.append(option)

    if help:
        cmd = ["gufi_ls", "--help"]
    else:
        cmd.extend(["--delim", "\t"])
        if path:
            cmd.append(path)

    result = subprocess.run(cmd, capture_output=True, text=True)
    return util.pack_command_result(result)

@mcp.tool()
def gufi_du(
        subpath: Annotated[str, Field(description="Optional subpath to input for du. This directory must have treesummary active to get results.")] = None,
        options: Annotated[list[str], Field(description="Options to submit for gufi_du. submit '--help' to view these options. A value supplied after an option should be a new list entry.")] = None
) -> GufiToolResult:
    ''' Execute user-facing tool: gufi equivalent of ls '''

    cmd = ["gufi_du"]
    help = False
    if options:
        for option in options:
            if option == "--help":
                help = True
            cmd.append(option)

    if help:
        cmd = ["gufi_du", "--help"]
    elif subpath:
        cmd.append(subpath)

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.stderr and "have treesummary data?" in result.stderr:
        return GufiToolResult(
            rows=[[f"Treesummary error: The subpath {subpath} does not have a treesummary table, cannot use gufi_du."]],
            row_count=1,
            stderr=result.stderr.strip(),
            returncode=result.returncode,
        ).model_dump()

    return util.pack_command_result(result)

@mcp.tool()
def gufi_find(
        options: Annotated[list[str], Field(description="Options to submit for gufi_find. submit '--help' to view these options. Do not use --delim, the tool uses its own to give structured output. A value supplied after an option should be a new list entry.")] = None
) -> GufiToolResult:
    ''' Execute user-facing tool: gufi equivalent of find '''

    cmd = ["gufi_find"]
    help = False
    if options:
        for option in options:
            if option == "--help":
                help = True
            cmd.append(option)

    if help:
        cmd = ["gufi_find", "--help"]
    else:
        cmd.extend(["--delim", "\t"])

    result = subprocess.run(cmd, capture_output=True, text=True)
    return util.pack_command_result(result)


@mcp.tool()
def gufi_stat(
        file: Annotated[str, Field(description="File to stat.")] = None,
        options: Annotated[list[str], Field(description="Options to submit for gufi_stat. submit '--help' to view these options. A value supplied after an option should be a new list entry.")] = None
) -> GufiToolResult:
    ''' Execute user-facing tool: gufi equivalent of ls '''

    cmd = ["gufi_stat"]
    help = False
    if options:
        for option in options:
            if option == "--help":
                help = True
            cmd.append(option)

    if help:
        cmd = ["gufi_stat", "--help"]
    else:
        cmd.append(file)

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.stderr and "No such file or directory" in result.stderr:
        return GufiToolResult(
            rows=[[f"File error: gufi_stat was not able to find the file {file}."]],
            row_count=1,
            stderr=result.stderr.strip(),
            returncode=result.returncode,
        ).model_dump()

    return util.pack_command_result(result)

@mcp.tool()
def gufi_stats(
        path: Annotated[str, Field(description="Path to obtain statistic from. Resolved with tool internally.")] = None,
        stat: Annotated[str, Field(description="Statistic to use. Use the --help option to view these statistics.")] = None,
        options: Annotated[list[str], Field(description="Options to submit for gufi_stats. submit '--help' to view these options. Do not use --delim, the tool uses its own to give structured output. A value supplied after an option should be a new list entry.")] = None
) -> GufiToolResult:
    ''' Execute user-facing tool: gufi equivalent of ls '''

    cmd = ["gufi_stats"]
    help = stat == "--help"
    if options:
        for option in options:
            if option == "--help":
                help = True
            cmd.append(option)

    if help:
        cmd = ["gufi_stats", "--help"]
    else:
        cmd.extend([stat, path, "--delim", "\t"])

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.stderr and "argument stat: invalid choice:" in result.stderr:
        return GufiToolResult(
            rows=[[f"Tool error: gufi_stats does not allow getting statistic {stat}."]],
            row_count=1,
            stderr=result.stderr.strip(),
            returncode=result.returncode,
        ).model_dump()
    if result.stderr and "No such file or directory" in result.stderr:
        return GufiToolResult(
            rows=[[f"Path error: gufi_stats was not able to find the path {path}."]],
            row_count=1,
            stderr=result.stderr.strip(),
            returncode=result.returncode,
        ).model_dump()

    return util.pack_command_result(result)

@mcp.tool()
def gufi_getfattr(
        path: Annotated[str, Field(description="Path to obtain fattrs from. Resolved with tool internally.")],
        options: Annotated[list[str], Field(description="Options to submit for gufi_stats. submit '--help' to view these options. Do not use --delim, the tool uses its own to give structured output. A value supplied after an option should be a new list entry.")] = None
) -> GufiToolResult:
    ''' Execute user-facing tool: gufi equivalent of get_fattr '''

    cmd = ["gufi_getfattr"]
    help = False
    if options:
        for option in options:
            if option == "--help":
                help = True
            cmd.append(option)

    if help:
        cmd = ["gufi_getfattr", "--help"]
    else:
        cmd.extend([path, "--delim", "\t"])

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.stderr and "No such file or directory" in result.stderr:
        return GufiToolResult(
            rows=[[f"Path error: gufi_getfattr was not able to find the path {path}."]],
            row_count=1,
            stderr=result.stderr.strip(),
            returncode=result.returncode,
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
  command wrappers. Use these for high-level searches, quick metadata checks,
  and common filesystem-style questions. Some commands need treesummary. Pass
  --help in options to see usage; help text may appear in stderr.
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
- Start with gufi_find when appropriate, or sql_file_index against vrpentries.
- Filter type = 'f'. Keep output bounded.

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
