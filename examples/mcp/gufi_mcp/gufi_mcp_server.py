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
from gufi_mcp_util import GufiQueryResult, GufiToolResult, GufiQuery, GufiSQLOption
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

SESSION_BRIEF_STUB = (
    "gufi://session-brief was removed. Follow this MCP server's instructions "
    "and read gufi://indexes when you need index names. For user-tool questions "
    "(gufi_find, gufi_ls, gufi_du, gufi_stat, gufi_stats, gufi_getfattr), do not "
    "prefetch gufi://query-guide, gufi://aggregate-guide, gufi://schemas/*, or "
    "gufi://tool-guide/*."
)

ENTRY_CONTEXT = """
You are using a GUFI MCP server. GUFI indexes filesystem metadata for fast
search without walking the live filesystem.

Default to structured user tools: gufi_find, gufi_ls, gufi_du, gufi_stat,
gufi_stats, gufi_getfattr. Use only fields and enum values from each tool
schema; do not invent CLI flags.

Do not read gufi://query-guide, gufi://aggregate-guide, gufi://schemas/*, or
gufi://tool-guide/* for user-tool tasks. Read gufi://indexes only when the
index name is unknown. Use gufi://tool-guide/{tool_name} only if a schema field
is unclear.

Paths: index is the name from gufi://indexes; optional subpath is under that
index—never repeat the index name in subpath. User-tool arguments are flat
top-level fields (not nested under request). Use num_results on gufi_find and
when listing with gufi_stats. GNU find mtime: +n means older than n days; -n
means modified within the last n days.

Common answers without SQL: total files → gufi_stats total-filecount; top-N
largest files → gufi_find largest + ls + num_results; extension totals →
gufi_stats extensions with cumulative (not recursive together with cumulative).

SQL-only: sql_file_index for shard-local scans; aggregate_sql_query for index-wide
totals, grouping, or global ORDER BY/LIMIT. Then read gufi://query-guide,
gufi://aggregate-guide, and gufi://schemas/vrpentries as needed. URIs use the
gufi:// prefix.
""".strip()

mcp = MCPServer(name="gufi-mcp", instructions=ENTRY_CONTEXT)

EVENT_LOGGER = EventLogger.from_env()
attach_event_logging(mcp, EVENT_LOGGER)


def _user_tool_error(exc: ValueError) -> dict[str, Any]:
    return GufiToolResult(error=str(exc), row_count=0).model_dump()


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
        sqlin: Annotated[str, Field(description="SELECT and FROM portion of the SQL query.")],
        wherein: Annotated[str, Field(description="WHERE, ORDER, and LIMIT portion of the SQL query.")],
        index: Annotated[str, Field(description="Index / subpath to query. This will be resolved internally so the name obtained from gufi://indexes should be used.")],
        remote: Annotated[bool, Field(description="Boolean for if this is a remote connection. Leave untouched unless user specifies otherwise. Config will be handled before invokation if remote is needed.")] = False
) -> GufiQueryResult:
    """
        sql query on local file information index
    """

    query_result = GufiQueryResult()

    # Build SQL line
    if remote:
        searchpath = util.resolve_remote_index(index)
        sqlline = '%s(\'%s\',1,0,99,NULL,0,\'ssh\',\'%s\') %s' % (sqlin, searchpath, REMOTEHOST, wherein)
    else:
        searchpath = util.resolve_local_index(index)
        if not searchpath:
            raise RuntimeError(f"Error: Index {index} not found")
        sqlline = '%s(\'%s\',1,1,99,NULL,1) %s' % (sqlin, searchpath, wherein)

    # Execute Query using GUFI_VT
    result = util.execute_sql(sqlline, True)

    if result[0] != "sql error:":
        query_result.rows = [list(res_row) for res_row in result]
        query_result.columns = util.get_columns_from_sqlin(sqlin)
        query_result.row_count = len(query_result.rows)
        return query_result.model_dump()

    query_result.error = str(result[1])
    query_result.sql = sqlline
    return query_result.model_dump()

@mcp.tool()
def aggregate_sql_query(
        query: Annotated[GufiQuery, Field(description="Constructed object for the aggregate SQL query.")],
) -> GufiQueryResult:

    index = query.index
    if not util.subpath_exists(index):
        raise RuntimeError(f"Error: Index or subpath {index} does not exist")

    ok, validation_message = util.validate_aggregate_order(query)
    if not ok:
        return GufiQueryResult(
            error=validation_message or "Aggregate order is not valid",
            execution_mode="aggregate",
        ).model_dump()

    sql_query = util.build_aggregate_create_sql(query)
    result = util.execute_sql(sql_query, True)

    query_result = GufiQueryResult(execution_mode="aggregate", sql=sql_query)
    g_sql = next(
        (item.sql for item in query.sql_options if item.option == GufiSQLOption.G),
        None,
    )
    if g_sql and g_sql.strip().upper().startswith("SELECT"):
        try:
            query_result.columns = util.get_columns_from_sqlin(g_sql)
        except (AttributeError, IndexError):
            query_result.columns = []

    if result[0] != "sql error:":
        query_result.rows = [list(res_row) for res_row in result]
        if not query_result.columns and query_result.rows:
            query_result.columns = [""] * len(query_result.rows[0])
        query_result.row_count = len(query_result.rows)
        return query_result.model_dump()

    query_result.error = str(result[1])
    return query_result.model_dump()

for _user_tool_name in util.WRAPPER_TOOLS:
    _user_tool_fn = util.flat_mcp_tool_function(_user_tool_name, _user_tool_error)
    mcp.tool(description=util.tool_description(_user_tool_name))(_user_tool_fn)

# resource that returns indexes available
@mcp.resource("gufi://indexes")
def gufi_indexes() -> list[list[Any]]:
    ''' Access list of available gufi indexes, as well as a boolean for if treesummary is active in this index '''

    output = []
    indexes = util.get_gufi_indexes()
    for index in indexes:
        output.append([index, util.has_treesummary(index)])

    return output

@mcp.resource("gufi://tool-guide/{tool_name}")
def gufi_tool_guide(tool_name: str) -> str:
    ''' Return detailed help text for one user-facing GUFI tool. '''

    if tool_name not in util.WRAPPER_TOOLS:
        allowed = ", ".join(util.WRAPPER_TOOLS)
        raise RuntimeError(f"Unknown GUFI tool {tool_name}. Expected one of: {allowed}")

    return util.get_tool_help(tool_name)

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
            rowsd = {row[0]: row[1] for row in rows}
            return rowsd

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

        # Attach hints and convert to dict
        schema_with_hints = util.load_schema_hints(schema, rows)
        return schema_with_hints

@mcp.resource("gufi://session-brief")
def gufi_session_brief() -> str:
    ''' Redirect legacy session brief readers to MCP instructions. '''
    return SESSION_BRIEF_STUB

@mcp.resource("gufi://session-brief/")
def gufi_session_brief_slash() -> str:
    ''' Same stub for clients that append a trailing slash. '''
    return SESSION_BRIEF_STUB

@mcp.resource("gufi://query-guide")
def gufi_query_guide() -> str:
    ''' Return guidance for direct sql_file_index queries. '''

    return """
Read this only when calling sql_file_index or aggregate_sql_query.

Use sql_file_index for simple filtered row scans when shard-local semantics are
acceptable. Prefer views over base tables:
- vrpentries: best default for file rows with directory context.
- pentries: file metadata with parent inode fields; use path() when you need a path.
- vrsummary and summary-family views: directory-level metadata.
- entries: low-level base table; avoid it for routine path questions.

If you need column names, read gufi://schemas/vrpentries once—not on every session.

Important semantics:
- ORDER BY, LIMIT, OFFSET, GROUP BY, and aggregate functions in sql_file_index
  apply per GUFI subtree, not globally across the whole index.
- For globally correct totals, grouping, or top-N ordering, use
  aggregate_sql_query and read gufi://aggregate-guide.
- Do not SELECT path FROM entries; entries has no path column.
""".strip()

@mcp.resource("gufi://aggregate-guide")
def gufi_aggregate_guide() -> str:
    ''' Return guidance for aggregate_sql_query pipelines. '''

    return """
Only read when using aggregate_sql_query.

Use aggregate_sql_query when the answer must be correct across the whole index:
global totals, grouped totals, global ORDER BY/LIMIT, or top-N results.

Pipeline phases:
- I creates per-thread intermediate tables.
- E, S, and T read entries, summary, and treesummary data into intermediate.
- K creates the final aggregate table.
- J merges intermediate rows into aggregate.
- G returns final rows. Put global ORDER BY and LIMIT here.
- F is cleanup only; do not use it for final sorted output.

Critical rules:
- Do not put ORDER BY, LIMIT, or OFFSET in E, S, or T phases.
- Use K plus J plus G for index-wide sorted or top-N answers.
- Pass the index name from gufi://indexes unless intentionally querying an
  absolute index path.

Example: total regular-file bytes
- I: CREATE TABLE intermediate(size INT64)
- E: INSERT INTO intermediate SELECT size FROM vrpentries WHERE type = 'f'
- K: CREATE TABLE aggregate(total INT64)
- J: INSERT INTO aggregate SELECT SUM(size) FROM intermediate
- G: SELECT SUM(total) FROM aggregate

Example: top UIDs by regular-file bytes
- I: CREATE TABLE intermediate(uid INT64, total_bytes INT64)
- E: INSERT INTO intermediate SELECT uid, size FROM vrpentries WHERE type = 'f'
- K: CREATE TABLE aggregate(uid INT64, total_bytes INT64)
- J: INSERT INTO aggregate SELECT uid, SUM(total_bytes) FROM intermediate GROUP BY uid
- G: SELECT uid, total_bytes FROM aggregate ORDER BY total_bytes DESC LIMIT 10
""".strip()

if __name__ == "__main__":
    # Run the server with HTTP transport
    mcp.run(transport=MCPTRANSPORT, host=MCPSRVHOST, port=MCPSRVPORT)
