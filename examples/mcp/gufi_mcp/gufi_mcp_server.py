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

import os
import shutil
from typing import Annotated, Any

from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError
from pydantic import Field

import gufi_mcp_util as util
from gufi_mcp_util import GufiAggregateQuery, GufiQueryResult, GufiToolResult
from gufi_mcp_util import GufiToolArgsLS, args_ls_dict
from gufi_mcp_util import GufiToolArgsDU, args_du_dict
from gufi_mcp_util import GufiToolArgsFind, args_find_dict
from gufi_mcp_util import GufiToolArgsStat, args_stat_dict
from gufi_mcp_util import GufiToolArgsStats, args_stats_dict
from gufi_mcp_util import GufiToolArgsGetfattr, args_getfattr_dict

load_dotenv()

MCPTRANSPORT = os.getenv("MCPTRANSPORT", "stdio").strip()
MCPSRVHOST = os.getenv("MCPSRVHOST", "127.0.0.1").strip()
MCPSRVPORT = int(os.getenv("MCPSRVPORT", "8000"))

DELIM = "\t"

mcp = MCPServer(name="gufi-mcp")


@mcp.tool()
def gufi_version() -> str:
    """Version of the gufi_query binary the server uses."""
    return util.run_gufi_text(["gufi_query", "--version"])


@mcp.tool()
def gufi_location() -> str:
    """Absolute path of the gufi_query binary the server uses."""
    location = shutil.which("gufi_query")
    if location is None:
        raise util.GufiError("gufi_query was not found on PATH")
    return location


@mcp.tool()
def sql_file_index(
        sqlin: Annotated[str, Field(description=(
            "A single SELECT statement over exactly one GUFI table: entries, pentries, vrpentries, "
            "summary, vrsummary, or treesummary (a gufi_vt_ prefix is optional). "
            "Example: SELECT name, size FROM vrpentries WHERE type = 'f' ORDER BY size DESC LIMIT 10"
        ))],
        index: Annotated[str, Field(description="Index name exactly as listed by the gufi://indexes resource.")],
        remote: Annotated[bool, Field(description=(
            "Query the index on the configured remote host over ssh. Leave false unless the user asks for a remote query; "
            "index is then a path on the remote host."
        ))] = False,
) -> GufiQueryResult:
    """Run a SELECT over one GUFI table across the whole index; WHERE, GROUP BY, ORDER BY and LIMIT apply index-wide."""
    sql, params = util.build_gufi_vt_select(sqlin, index, remote)
    return util.run_gufi_vt_select(sql, params)


@mcp.tool()
def aggregate_sql_query(
        query: Annotated[GufiAggregateQuery, Field(description="The aggregate pipeline to run.")],
) -> GufiQueryResult:
    """Run a GUFI scatter/gather aggregate pipeline over an index and return the rows of final_sql."""
    return util.run_aggregate(query)


@mcp.tool()
def gufi_ls(
        args: Annotated[GufiToolArgsLS, Field(default_factory=GufiToolArgsLS, description="Options for gufi_ls.")],
) -> GufiToolResult:
    """GUFI equivalent of ls: list the contents of an indexed directory."""
    cmd = util.build_tool_command("gufi_ls", args, args_ls_dict, trailing=[f"--delim={DELIM}", args.path])
    return util.run_gufi_tool(cmd, DELIM)


@mcp.tool()
def gufi_du(
        args: Annotated[GufiToolArgsDU, Field(default_factory=GufiToolArgsDU, description="Options for gufi_du.")],
) -> GufiToolResult:
    """GUFI equivalent of du: summarize disk usage of indexed directories. Requires treesummary data."""
    cmd = util.build_tool_command("gufi_du", args, args_du_dict, trailing=args.paths)
    result = util.run_gufi_tool(cmd, DELIM)
    # gufi_du exits 0 and prints a size of 0 when treesummary data is missing.
    if "have treesummary data?" in result.stderr:
        raise util.GufiError(
            f"One of {args.paths} has no treesummary data, so gufi_du cannot report its size. "
            "Use aggregate_sql_query instead."
        )
    return result


@mcp.tool()
def gufi_find(
        args: Annotated[GufiToolArgsFind, Field(default_factory=GufiToolArgsFind, description="Options for gufi_find.")],
) -> GufiToolResult:
    """GUFI equivalent of find: search indexed directory trees."""
    # gufi_find reads starting paths before any expression flags.
    cmd = util.build_tool_command("gufi_find", args, args_find_dict, leading=args.paths, trailing=[f"--delim={DELIM}"])
    return util.run_gufi_tool(cmd, DELIM)


@mcp.tool()
def gufi_stat(
        args: Annotated[GufiToolArgsStat, Field(default_factory=GufiToolArgsStat, description="Options for gufi_stat.")],
) -> GufiToolResult:
    """GUFI equivalent of stat: display the status of indexed files or directories."""
    cmd = util.build_tool_command("gufi_stat", args, args_stat_dict, trailing=args.files)
    return util.run_gufi_tool(cmd, DELIM)


@mcp.tool()
def gufi_stats(
        args: Annotated[GufiToolArgsStats, Field(default_factory=GufiToolArgsStats, description="Options for gufi_stats.")],
) -> GufiToolResult:
    """Compute an index-level filesystem statistic with gufi_stats."""
    cmd = util.build_tool_command(
        "gufi_stats", args, args_stats_dict,
        trailing=[f"--delim={DELIM}", args.statistic.value, args.path],
    )
    return util.run_gufi_tool(cmd, DELIM)


@mcp.tool()
def gufi_get_fattr(
        args: Annotated[GufiToolArgsGetfattr, Field(default_factory=GufiToolArgsGetfattr, description="Options for gufi_getfattr.")],
) -> GufiToolResult:
    """GUFI equivalent of getfattr: read extended attributes of indexed paths."""
    cmd = util.build_tool_command("gufi_getfattr", args, args_getfattr_dict, trailing=[f"--delim={DELIM}", *args.paths])
    return util.run_gufi_tool(cmd, DELIM)


@mcp.resource("gufi://indexes")
def gufi_indexes() -> list[dict[str, Any]]:
    """Available GUFI indexes and whether each has treesummary data."""
    try:
        return [{"index": index, "treesummary": util.has_treesummary(index)} for index in util.get_gufi_indexes()]
    except util.GufiError as e:
        raise ResourceError(str(e)) from e


@mcp.resource("gufi://schemas/{schema}")
def gufi_schemas(schema: str) -> dict[str, Any]:
    """Use 'all' for every table and view name with its type, or a table/view name for its columns, types, and hints."""
    try:
        if schema == "all":
            return util.list_schema_objects()
        return util.describe_schema_object(schema)
    except util.GufiError as e:
        raise ResourceError(str(e)) from e


@mcp.resource("gufi://session-brief")
def gufi_session_briefing() -> str:
    """How to use this server's tools and resources."""
    return """
You are working with a GUFI MCP server. GUFI is a parallel file system
indexer: it scans a file tree into SQLite databases so file metadata can be
searched quickly without walking the live file system for every question.

Prefer the simplest tool that can answer the user. You do not need to use every
tool in a session. Start with high-level GUFI tools for quick inspection, move
to SQL when the user needs a more specific query, and use aggregate SQL only
when a simple tool or one-statement SQL query cannot answer the request cleanly.

Available context:
- Read gufi://indexes to list known indexes and whether each has treesummary.
- Read gufi://schemas/all to discover query surfaces, then gufi://schemas/{name}
  for one table or view (for example vrpentries, pentries, summary, vrsummary).
- Read gufi://session-brief for this guide. Resource URIs must include the
  gufi:// prefix; gufi_indexes and session-brief alone are not valid URIs.

Query surfaces: prefer views over base tables
- vrpentries: best default for file rows that need directory context. Includes
  file columns plus summary path context (sname, dname). Prefer this over entries.
- pentries: file metadata with parent inode fields. No path column.
- vrsummary / vsummary* views: directory-level rolled-up metadata.
- summary / treesummary: directory rollup tables when available.
- entries: low-level base table. Avoid for routine agent queries. It has no
  path column.

Before writing SQL, inspect gufi://schemas/vrpentries or gufi://schemas/pentries
instead of guessing column names. Column names are checked before a query runs.
Use single quotes for string literals ('f'); double quotes are rejected in
aggregate SQL.

Tool guidance:
- gufi_ls, gufi_du, gufi_find, gufi_stat, gufi_stats, and gufi_get_fattr wrap
  the GUFI command-line tools. Pass options in the args object; paths are
  relative to the GUFI index root and start with the index name (for example
  "Downloads" or "Downloads/subdir"). gufi_du needs treesummary. Results include
  stderr; non-empty stderr may mean results are incomplete.
- sql_file_index runs one SELECT over one table (entries, pentries, vrpentries,
  summary, vrsummary, or treesummary) for a whole index. WHERE, GROUP BY,
  ORDER BY, LIMIT and aggregate functions apply across the entire index.
  Subqueries, joins, and WITH are not supported.
- aggregate_sql_query runs GUFI's parallel scatter/gather pipeline. Use it for
  index-wide totals or grouping over large indexes, or when treesummary and
  summary data should be combined with file rows.
- Results larger than the row limit come back with truncated = true. Add
  filters or a LIMIT rather than retrying the same query.

Treesummary nuance:
treesummary exists only when an index has been rolled up. It stores precomputed
subtree summary data and can make directory-level totals very fast. Read
gufi://indexes first. If treesummary is false for an index, do not use gufi_du
or treesummary tables; use SQL over summary or entries tables instead.

aggregate_sql_query fields (the query object):
- index: index name from gufi://indexes.
- scatter_shape: columns of the per-thread intermediate table, as
  "name TYPE, name TYPE" (TYPE: INT64, INTEGER, INT, BIGINT, REAL, FLOAT,
  DOUBLE, NUMERIC, TEXT, BLOB).
- scatter_sql: one to three SELECT statements, each reading exactly one table.
  The table decides when it runs: treesummary/vtsummary* once per tree,
  summary/vsummary*/vrsummary* once per directory, entries/pentries/vrpentries
  over each directory's files. At most one statement per kind. Each must
  return exactly the scatter_shape columns, in order. Rows are inserted into
  intermediate for you; do not write INSERT.
- gather_shape: columns of the aggregate table, same format.
- gather_sql: SELECT from intermediate returning exactly the gather_shape
  columns. Merges each thread's intermediate rows into aggregate.
- final_sql: SELECT from aggregate producing the answer. Put global ORDER BY
  and LIMIT here.

Critical rules:
- Do not put ORDER BY, LIMIT, or OFFSET in scatter_sql; it is rejected.
- Gather and final SQL can only read the columns their shape defines.
- Prefer vrpentries over entries for file/path questions.

Common task patterns:

Name-filtered file listing:
- Start with gufi_find, or sql_file_index against vrpentries.
- Filter type = 'f'. Keep output bounded with LIMIT.

Directory or index summary:
- Read gufi://indexes first, then choose gufi_du, gufi_stats, summary-family
  views, or SQL depending on what the index supports.

Example aggregate queries:

Total byte size of regular files:
  {"index": "<name>",
   "scatter_shape": "size INT64",
   "scatter_sql": ["SELECT size FROM vrpentries WHERE type = 'f'"],
   "gather_shape": "total INT64",
   "gather_sql": "SELECT SUM(size) FROM intermediate",
   "final_sql": "SELECT SUM(total) AS total FROM aggregate"}

Top UIDs by total regular-file bytes:
  {"index": "<name>",
   "scatter_shape": "uid INT64, size INT64",
   "scatter_sql": ["SELECT uid, size FROM vrpentries WHERE type = 'f'"],
   "gather_shape": "uid INT64, total_bytes INT64",
   "gather_sql": "SELECT uid, SUM(size) FROM intermediate GROUP BY uid",
   "final_sql": "SELECT uid, SUM(total_bytes) AS total_bytes FROM aggregate GROUP BY uid ORDER BY total_bytes DESC LIMIT 10"}
"""


if __name__ == "__main__":
    if MCPTRANSPORT == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=MCPTRANSPORT, host=MCPSRVHOST, port=MCPSRVPORT)
