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

import logging
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

PRIMER = f"""\
GUFI is a parallel file system indexer: it scans a file
tree into per-directory SQLite databases so metadata questions (sizes, owners,
times, names) are answered from the index instead of walking the file system.

Before the first query
- Read gufi://indexes for index names and whether each has treesummary data.
- Read gufi://schemas/all for table and view names, then gufi://schemas/<table>
  for its columns. Column names are checked before any query runs; do not guess.

Tables and views (prefix and letter meanings)
- entries: files and links of one directory. pentries adds the parent inode.
- vrpentries: v = view, r = rollup-aware, p = with parent. File rows plus their
  directory's metadata and paths. Prefer it for file questions.
- summary / vrsummary: one row per directory (the directory's own metadata plus
  totals such as totfiles, totsize). Prefer vrsummary. Directories are not in entries.
- treesummary: precomputed totals for a whole subtree. Only present when the
  index has treesummary (see gufi://indexes).
- type is 'f' regular file, 'l' symlink, 'd' directory. Times (atime, mtime,
  ctime) are Unix epoch seconds; size is bytes.

Paths
- Paths start with the index name, then the path inside the index, e.g.
  myindex/dir/file.txt. Every tool uses this form: the gufi_* tools print it and
  take it as path arguments, and the path columns hold it.
- vrpentries has path (the file's full path) and dirpath (its directory's);
  vrsummary has path (the directory's). name is only the base name.
- Path columns are ordinary TEXT columns: select, filter (path LIKE
  'myindex/dir/%'), or group by them, in sql_file_index or in scatter_sql.
  To use one in gather_sql or final_sql, carry it through scatter_shape and
  gather_shape like any other column.
- Other functions include ext(name) (text after the last '.'), bytecat(size) and
  agecat(time) (GUFI size and age categories).

Choosing a tool
- gufi_ls, gufi_find, gufi_du, gufi_stat, gufi_stats, gufi_get_fattr wrap GUFI's
  command-line tools; paths start with an index name (e.g. "myindex/sub").
- sql_file_index runs one SELECT over one table across the whole index.
- aggregate_sql_query runs GUFI's parallel scatter/gather pipeline.

How aggregate_sql_query works (scatter/gather)
- GUFI walks the index with many threads. Scatter: each scatter_sql statement
  runs once per directory and its rows go into that thread's intermediate table
  (columns = scatter_shape).
- Gather: gather_sql runs once per thread over its intermediate table, and the
  rows go into one shared aggregate table (columns = gather_shape). The aggregate
  table therefore holds one partial result per thread, not a final answer.
- Final: final_sql runs once over aggregate. Re-combine partials there: SUM the
  counts and sums, MAX the maxima, and so on. A final_sql without that returns
  one row per thread.
- Count raw rows only once. COUNT(*) belongs at the first stage that sees one row
  per file; every later stage SUMs the counts it receives. If scatter_sql already
  uses COUNT(*) ... GROUP BY, gather_sql must SUM that column, not COUNT(*) again.
- Example, symlinks per gid:
    scatter_shape "gid INT64, size INT64"
    scatter_sql   ["SELECT gid, size FROM vrpentries WHERE type = 'l'"]
    gather_shape  "gid INT64, links INT64, bytes INT64"
    gather_sql    "SELECT gid, COUNT(*), SUM(size) FROM intermediate GROUP BY gid"
    final_sql     "SELECT gid, SUM(links) AS links, SUM(bytes) AS bytes FROM aggregate GROUP BY gid"
- Per-group top-N (for example the N largest per owner) needs a window function
  in final_sql: SELECT ... FROM (SELECT ..., ROW_NUMBER() OVER (PARTITION BY
  <group> ORDER BY <value> DESC) AS rn FROM aggregate) WHERE rn <= N.
  A plain LIMIT N in final_sql returns N rows in total.
- ORDER BY, LIMIT, and OFFSET are allowed only in final_sql, not anywhere in
  scatter_sql or gather_sql (subqueries and window functions included). Those
  stages run once per directory or per thread, so ordering or limiting there
  drops partial results and gives wrong totals. Filter with WHERE instead, and
  sort or limit once in final_sql.

Time questions
- Compute cutoffs inside SQL from the current time you were given, e.g.
  mtime < <now_epoch> - 30 * 86400 for "not modified in 30 days". Do not
  precompute epoch values by hand.

Treesummary
- Without treesummary, gufi_du and treesummary tables give empty or zero results;
  use summary/vrsummary or vrpentries instead.

Results
- Results above {util.MAX_ROWS} rows come back with truncated = true and are
  incomplete. Aggregate or filter instead of retrying the same query.
- Use single quotes for string literals; double quotes are rejected in aggregate SQL.
"""

if util.GUFI_SOURCE_ROOT is not None:
    PRIMER = PRIMER.replace(
        "vrsummary has path (the directory's). name is only the base name.",
        "vrsummary has path (the directory's). name is only the base name.\n"
        "- spath is the same entry's path on the source file system that was indexed\n"
        f"  (e.g. {util.GUFI_SOURCE_ROOT}/myindex/dir/file.txt); tools do not accept it.",
    )

_cli_root = util.cli_index_root()
if _cli_root is not None and _cli_root != util.GUFI_INDEX_ROOT:
    logging.getLogger(__name__).warning(
        "GUFI_INDEX_ROOT %s differs from the GUFI tools' IndexRoot %s; paths from SQL and from the "
        "gufi_* tools will not match", util.GUFI_INDEX_ROOT, _cli_root)

mcp = MCPServer(name="gufi-mcp", instructions=PRIMER)


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
            "summary, vrsummary, or treesummary (a gufi_vt_ prefix is optional). Read gufi://schemas/<table> "
            "for its columns first. Full paths are the path columns of vrpentries (path, dirpath) and vrsummary (path). "
            "Example: SELECT path, linkname FROM vrpentries WHERE type = 'l' LIMIT 20"
        ))],
        index: Annotated[str, Field(description="Index name exactly as listed by the gufi://indexes resource.")],
        remote: Annotated[bool, Field(description=(
            "Query the index on the configured remote host over ssh. Leave false unless the user asks for a remote query; "
            "index is then a path on the remote host."
        ))] = False,
) -> GufiQueryResult:
    """Run a SELECT over one GUFI table across the whole index; WHERE, GROUP BY, ORDER BY and LIMIT apply index-wide.

    Subqueries, joins and WITH are not supported. Table and column names: gufi://schemas/all and gufi://schemas/<table>.
    """
    sql, params = util.build_gufi_vt_select(sqlin, index, remote)
    return util.run_gufi_vt_select(sql, params)


@mcp.tool()
def aggregate_sql_query(
        query: Annotated[GufiAggregateQuery, Field(description="The aggregate pipeline to run.")],
) -> GufiQueryResult:
    """Run a GUFI scatter/gather aggregate pipeline over an index and return the rows of final_sql.

    Scatter SQL runs per directory in parallel threads, gather SQL merges each thread's rows into the aggregate
    table, and final SQL combines those partial results once. Read gufi://indexes for the index name and
    gufi://schemas/<table> for column names before writing the pipeline.
    """
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


if __name__ == "__main__":
    if MCPTRANSPORT == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=MCPTRANSPORT, host=MCPSRVHOST, port=MCPSRVPORT)
