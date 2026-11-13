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

"""GUFI MCP server. Which parts it exposes is set by gufi_ablation (all on by default)."""

import os
from typing import Annotated, Any

from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError
from pydantic import Field

import gufi_mcp_util as util
import gufi_plain
from gufi_ablation import CONFIG, AblationConfig
from gufi_mcp_util import GufiQueryResult

load_dotenv()

MCPTRANSPORT = os.getenv("MCPTRANSPORT", "stdio").strip()
MCPSRVHOST = os.getenv("MCPSRVHOST", "127.0.0.1").strip()
MCPSRVPORT = int(os.getenv("MCPSRVPORT", "8000"))


#
# Primer fragments. assemble_primer() keeps only the ones that match the config, so the
# instructions never point at a tool or path form the server does not offer.
#

INTRO = """\
GUFI is a parallel file system indexer: it scans a file
tree into per-directory SQLite databases so metadata questions (sizes, owners,
times, names) are answered from the index instead of walking the file system.
"""

TABLES = """\
Tables and views (prefix and letter meanings)
- entries: files and links of one directory. pentries adds the parent inode.
- vrpentries: v = view, r = rollup-aware, p = with parent. File rows plus their
  directory's metadata{paths}. Prefer it for file questions.
- summary / vrsummary: one row per directory (the directory's own metadata plus
  totals such as totfiles, totsize). Prefer vrsummary. Directories are not in entries.
- treesummary: precomputed totals for a whole subtree. Only present when the
  index has treesummary (see gufi_indexes).
"""

PATHS = {
    "server": """\
Paths
- vrpentries has path (full path of the file or link) and dirpath (its directory);
  vrsummary has path (the directory). Paths start with the index name. They are
  plain TEXT columns, readable only in the per-directory stage; carry them through
  the stage shapes to use them later.
""",
    "rpath": """\
Paths
- Build full paths in the per-directory stage with GUFI's rpath(): rpath(sname, sroll)
  is the directory's path and rpath(sname, sroll, name) the path of a file or link in
  it. They return absolute paths in the index and only work in the per-directory
  stage; carry the result through the stage shapes to use it later.
""",
}

# Names of the pipeline pieces in each submission format.
TERMS = {
    "pydantic": {
        "intro": "How aggregate_sql_query works (per_directory, partial, final)",
        "pd_sql": "per_directory_sql", "pd_table": "per_directory table", "pd_cols": "columns = per_directory_shape",
        "p_sql": "partial_sql", "p_table": "the shared partial table", "p_cols": "columns = partial_shape",
        "f_sql": "final_sql",
    },
    "plain": {
        "intro": ("How aggregate_sql_query works: submit one gufi_query command line (-I, -T/-S/-E, -K, -J, -G,\n"
                  "then the index name)"),
        "pd_sql": "-T/-S/-E", "pd_table": "-I table", "pd_cols": "created by -I",
        "p_sql": "-J", "p_table": "the shared -K table", "p_cols": "created by -K", "f_sql": "-G",
    },
}

PIPELINE = """\
{intro}
- GUFI walks the index with many threads. Per-directory: each {pd_sql}
  statement runs once per directory; rows go into that thread's {pd_table}
  ({pd_cols}).
- Partial: {p_sql} runs once per thread over that {pd_table}, and rows go into
  {p_table} ({p_cols}). That table holds one partial result per
  thread, not the final answer.
- Final: {f_sql} runs once over {p_table}. Re-combine partials there: SUM counts
  and sums, MAX maxima, and so on. A {f_sql} without that returns one row per thread.
- Count raw rows only once. COUNT(*) belongs at the first stage that sees one row
  per file; every later stage SUMs the counts it receives. If {pd_sql}
  already uses COUNT(*) ... GROUP BY, {p_sql} must SUM that column, not COUNT(*) again.
"""

ORDERING = """\
- Per-group top-N (for example the N largest per owner) needs a window function
  in {f_sql}: SELECT ... FROM (SELECT ..., ROW_NUMBER() OVER (PARTITION BY
  <group> ORDER BY <value> DESC) AS rn FROM ...) WHERE rn <= N.
  A plain LIMIT N in {f_sql} returns N rows in total.
- ORDER BY, LIMIT, and OFFSET belong in {f_sql}. {pd_sql} and {p_sql}
  run once per directory or per thread, so ordering or limiting there drops partial
  results and gives wrong totals; they are rejected there, in subqueries and window
  functions too. Filter with WHERE instead, and sort or limit once in {f_sql}.
- One exception, for a plain global top N such as the N largest or newest entries:
  {pd_sql} may end with ORDER BY <selected columns> LIMIT N so each
  directory keeps only its own top N. It must not aggregate, {p_sql} must pass
  rows through (no WHERE, GROUP BY or aggregates), and {f_sql} must repeat the
  same ORDER BY with LIMIT at most N.
"""

TAIL = """\
Treesummary
- Without treesummary, treesummary tables give empty or zero results;
  use summary/vrsummary or vrpentries instead.

Results
- Results above {max_rows} rows come back with truncated = true and are
  incomplete. Aggregate or filter instead of retrying the same query.
- Use single quotes for string literals; double quotes are rejected in aggregate SQL.
"""


def _example(submission: str, title: str, query: dict[str, Any]) -> str:
    """One pipeline example in the submission format the agent has to write."""
    if submission == "plain":
        command = gufi_plain.to_plain_command({"index": "myindex", **query}, multiline=True)
        return f"- Example, {title}:\n    {command}\n"
    lines = [f"- Example, {title}:"]
    for key in ("per_directory_shape", "per_directory_sql", "partial_shape", "partial_sql", "final_sql"):
        value = query[key]
        shown = "[" + ", ".join(f'"{v}"' for v in value) + "]" if isinstance(value, list) else f'"{value}"'
        lines.append(f"    {key:<20s}{shown}")
    return "\n".join(lines) + "\n"


def examples(cfg: AblationConfig) -> str:
    path_sql = "rpath(sname, sroll, name)" if cfg.path == "rpath" else "path"
    links_per_gid = {
        "per_directory_shape": "gid INT64, size INT64",
        "per_directory_sql": ["SELECT gid, size FROM vrpentries WHERE type = 'l'"],
        "partial_shape": "gid INT64, links INT64, bytes INT64",
        "partial_sql": "SELECT gid, COUNT(*), SUM(size) FROM per_directory GROUP BY gid",
        "final_sql": "SELECT gid, SUM(links) AS links, SUM(bytes) AS bytes FROM partial GROUP BY gid",
    }
    newest_links = {
        "per_directory_shape": "lpath TEXT, mtime INT64",
        "per_directory_sql": [f"SELECT {path_sql}, mtime FROM vrpentries WHERE type = 'l' ORDER BY mtime DESC LIMIT 5"],
        "partial_shape": "lpath TEXT, mtime INT64",
        "partial_sql": "SELECT lpath, mtime FROM per_directory",
        "final_sql": "SELECT lpath, mtime FROM partial ORDER BY mtime DESC LIMIT 5",
    }
    breakdown = {
        "per_directory_shape": "per_value TYPE",
        "per_directory_sql": ["SELECT selection FROM table WHERE whereclause"],
        "partial_shape": "part_value TYPE, count INT64",
        "partial_sql": "SELECT per_value, COUNT(*) FROM per_directory GROUP BY per_value",
        "final_sql": "SELECT part_value, SUM(count) AS count FROM partial GROUP BY part_value",
    }
    return (_example(cfg.submission, "symlinks and their bytes per gid", links_per_gid)
            + _example(cfg.submission, "the 5 most recently modified symlinks (global top-N prefilter)", newest_links)
            + _example(cfg.submission, "a breakdown / histogram (count per value)", breakdown))


def discovery(cfg: AblationConfig) -> str:
    lines = ["Before the first query",
             "- Call gufi_indexes for index names and whether each has treesummary data."]
    if cfg.schema != "none":
        lines.append("- Call gufi_schemas with 'all' for table and view names, then with a table name\n"
                     "  for its columns. Column names are checked before any query runs; do not guess.")
    if cfg.functions:
        hints = "column hints name the ones that apply. " if cfg.schema == "hints" else ""
        lines.append(f"- GUFI adds SQL functions; {hints}Call gufi_functions for the list\n"
                     "  and gufi_function with a name for details.")
    return "\n".join(lines) + "\n"


def assemble_primer(cfg: AblationConfig) -> str:
    """Server instructions for cfg ("" when the primer is ablated)."""
    if not cfg.primer:
        return ""
    terms = TERMS[cfg.submission]
    sections = [
        INTRO,
        discovery(cfg),
        TABLES.format(paths=" and paths" if cfg.path == "server" else ""),
        PATHS[cfg.path],
        PIPELINE.format(**terms) + examples(cfg) + ORDERING.format(**terms),
        TAIL.format(max_rows=util.MAX_ROWS),
    ]
    return "\n".join(sections)


#
# Tools
#

AGGREGATE_DOC = """Run a GUFI per_directory / partial / final pipeline over an index and return the rows of final_sql.

per_directory_sql runs once per directory, partial_sql merges each thread's rows into the partial table,
and final_sql combines those partial results once. Use gufi_indexes for the index name{schemas} before
writing the pipeline."""

PLAIN_DOC = f"""Run a GUFI aggregate query written as a gufi_query command line and return the rows of the -G query.

Usage: {gufi_plain.USAGE}"""


def _tool_error(e: Exception) -> util.GufiError:
    return util.GufiError(str(e))


def build_server(cfg: AblationConfig) -> MCPServer:
    """An MCP server exposing exactly the parts cfg turns on."""
    server = MCPServer(name="gufi-mcp", instructions=assemble_primer(cfg))
    model = util.query_model(cfg)

    if cfg.submission == "plain":
        def aggregate_sql_query(
                command: Annotated[str, Field(description="The full gufi_query command line.")],
        ) -> GufiQueryResult:
            try:
                payload = gufi_plain.parse_plain_command(command)
            except gufi_plain.PlainCommandError as e:
                raise _tool_error(e) from e
            try:
                query = model.model_validate(payload)
            except ValueError as e:  # pydantic ValidationError: same messages as the structured tool
                raise _tool_error(e) from e
            return util.run_aggregate(query)

        server.tool(description=PLAIN_DOC)(aggregate_sql_query)
    else:
        def aggregate_sql_query(  # noqa: F811  (one of the two definitions is registered)
                query: Annotated[model, Field(description="The aggregate pipeline to run.")],
        ) -> GufiQueryResult:
            return util.run_aggregate(query)

        schemas = " and gufi_schemas with a table name for column names" if cfg.schema != "none" else ""
        server.tool(description=AGGREGATE_DOC.format(schemas=schemas))(aggregate_sql_query)

    @server.tool()
    def gufi_indexes() -> list[dict[str, Any]]:
        """Available GUFI indexes and whether each has treesummary data."""
        try:
            return [{"index": index, "treesummary": util.has_treesummary(index)} for index in util.get_gufi_indexes()]
        except util.GufiError as e:
            raise ResourceError(str(e)) from e

    if cfg.schema != "none":
        details = "columns, types, and hints" if cfg.schema == "hints" else "columns and types"

        def gufi_schemas(
                schema: Annotated[str, Field(description="'all' for schemas, enter schema for details.")],
        ) -> dict[str, Any]:
            try:
                if schema == "all":
                    return util.list_schema_objects()
                return util.describe_schema_object(schema)
            except util.GufiError as e:
                raise ResourceError(str(e)) from e

        server.tool(description=f"Use 'all' for every table and view name with its type, or a table/view name for "
                                f"its {details}.")(gufi_schemas)

    if cfg.functions:
        @server.tool()
        def gufi_functions() -> dict[str, str]:
            """GUFI SQL functions usable in every aggregate stage, with their signatures."""
            return util.list_functions()

        @server.tool()
        def gufi_function(name: str) -> dict[str, Any]:
            """One GUFI SQL function: signature, behavior, columns it applies to, and an example."""
            try:
                return util.describe_function(name)
            except util.GufiError as e:
                raise ResourceError(str(e)) from e

    return server


PRIMER = assemble_primer(CONFIG)
mcp = build_server(CONFIG)


if __name__ == "__main__":
    if MCPTRANSPORT == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=MCPTRANSPORT, host=MCPSRVHOST, port=MCPSRVPORT)
