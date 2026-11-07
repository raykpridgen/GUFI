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
from typing import Annotated, Any

from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError
from pydantic import Field

import gufi_mcp_util as util
from gufi_mcp_util import GufiAggregateQuery, GufiQueryResult

load_dotenv()

MCPTRANSPORT = os.getenv("MCPTRANSPORT", "stdio").strip()
MCPSRVHOST = os.getenv("MCPSRVHOST", "127.0.0.1").strip()
MCPSRVPORT = int(os.getenv("MCPSRVPORT", "8000"))

PRIMER = f"""\
GUFI is a parallel file system indexer: it scans a file
tree into per-directory SQLite databases so metadata questions (sizes, owners,
times, names) are answered from the index instead of walking the file system.

Before the first query
- Read gufi://indexes for index names and whether each has treesummary data.
- Read gufi://schemas/all for table and view names, then gufi://schemas/<table>
  for its columns. Column names are checked before any query runs; do not guess.
- GUFI adds SQL functions; column hints name the ones that apply. Read
  gufi://functions/<name> for details, or gufi://functions for the list.

Tables and views (prefix and letter meanings)
- entries: files and links of one directory. pentries adds the parent inode.
- vrpentries: v = view, r = rollup-aware, p = with parent. File rows plus their
  directory's metadata and paths. Prefer it for file questions.
- summary / vrsummary: one row per directory (the directory's own metadata plus
  totals such as totfiles, totsize). Prefer vrsummary. Directories are not in entries.
- treesummary: precomputed totals for a whole subtree. Only present when the
  index has treesummary (see gufi://indexes).

How aggregate_sql_query works (per_directory, partial, final)
- GUFI walks the index with many threads. Per-directory: each per_directory_sql
  statement runs once per directory; rows go into that thread's per_directory table
  (columns = per_directory_shape).
- Partial: partial_sql runs once per thread over per_directory, and rows go into
  the shared partial table (columns = partial_shape). That table holds one partial
  result per thread, not the final answer.
- Final: final_sql runs once over partial. Re-combine partials there: SUM counts
  and sums, MAX maxima, and so on. A final_sql without that returns one row per thread.
- Count raw rows only once. COUNT(*) belongs at the first stage that sees one row
  per file; every later stage SUMs the counts it receives. If per_directory_sql
  already uses COUNT(*) ... GROUP BY, partial_sql must SUM that column, not COUNT(*) again.
- Example, symlinks per gid:
    per_directory_shape "gid INT64, size INT64"
    per_directory_sql   ["SELECT gid, size FROM vrpentries WHERE type = 'l'"]
    partial_shape  "gid INT64, links INT64, bytes INT64"
    partial_sql    "SELECT gid, COUNT(*), SUM(size) FROM per_directory GROUP BY gid"
    final_sql      "SELECT gid, SUM(links) AS links, SUM(bytes) AS bytes FROM partial GROUP BY gid"
- Per-group top-N (for example the N largest per owner) needs a window function
  in final_sql: SELECT ... FROM (SELECT ..., ROW_NUMBER() OVER (PARTITION BY
  <group> ORDER BY <value> DESC) AS rn FROM partial) WHERE rn <= N.
  A plain LIMIT N in final_sql returns N rows in total.
- ORDER BY, LIMIT, and OFFSET are allowed only in final_sql, not in
  per_directory_sql or partial_sql (subqueries and window functions included).
  Those stages run once per directory or per thread, so ordering or limiting there
  drops partial results and gives wrong totals. Filter with WHERE instead, and
  sort or limit once in final_sql.
  
If a situation arises where some sort of breakdown / histogram is expected, use the following format:

per_directory_shape: "per_value TYPE"
per_directory_sql: ["SELECT selection FROM table WHERE whereclause"]
partial_shape: "part_value TYPE, count INT64"
partial_sql: "SELECT per_value, COUNT(*) FROM per_directory GROUP BY per_value"
final_sql: "SELECT part_value, SUM(count) AS count FROM partial GROUP BY part_value"

Treesummary
- Without treesummary, treesummary tables give empty or zero results;
  use summary/vrsummary or vrpentries instead.

Results
- Results above {util.MAX_ROWS} rows come back with truncated = true and are
  incomplete. Aggregate or filter instead of retrying the same query.
- Use single quotes for string literals; double quotes are rejected in aggregate SQL.
"""

mcp = MCPServer(name="gufi-mcp", instructions=PRIMER)

@mcp.tool()
def aggregate_sql_query(
        query: Annotated[GufiAggregateQuery, Field(description="The aggregate pipeline to run.")],
) -> GufiQueryResult:
    """Run a GUFI per_directory / partial / final pipeline over an index and return the rows of final_sql.

    per_directory_sql runs once per directory, partial_sql merges each thread's rows into the partial table,
    and final_sql combines those partial results once. Read gufi://indexes for the index name and
    gufi://schemas/<table> for column names before writing the pipeline.
    """
    return util.run_aggregate(query)


@mcp.tool()
def gufi_indexes() -> list[dict[str, Any]]:
    """Available GUFI indexes and whether each has treesummary data."""
    try:
        return [{"index": index, "treesummary": util.has_treesummary(index)} for index in util.get_gufi_indexes()]
    except util.GufiError as e:
        raise ResourceError(str(e)) from e


@mcp.tool()
def gufi_schemas(
        schema: Annotated[str, Field(description="'all' for schemas, enter schema for details.")],
) -> dict[str, Any]:
    """Use 'all' for every table and view name with its type, or a table/view name for its columns, types, and hints."""
    try:
        if schema == "all":
            return util.list_schema_objects()
        return util.describe_schema_object(schema)
    except util.GufiError as e:
        raise ResourceError(str(e)) from e


@mcp.tool()
def gufi_functions() -> dict[str, str]:
    """GUFI SQL functions usable in every aggregate stage, with their signatures."""
    return util.list_functions()


@mcp.tool()
def gufi_function(name: str) -> dict[str, Any]:
    """One GUFI SQL function: signature, behavior, columns it applies to, and an example."""
    try:
        return util.describe_function(name)
    except util.GufiError as e:
        raise ResourceError(str(e)) from e


if __name__ == "__main__":
    if MCPTRANSPORT == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=MCPTRANSPORT, host=MCPSRVHOST, port=MCPSRVPORT)
