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
        sqlin: Annotated[str, Field(description="SELECT and FROM portion of the SQL query.")],
        wherein: Annotated[str, Field(description="WHERE, ORDER, and LIMIT portion of the SQL query.")],
        index: Annotated[str, Field(description="Index / subpath to query. This will be resolved internally so the name obtained from the gufi_indexes resource should be used.")],
        remote: Annotated[bool, Field(description="Boolean for if this is a remote connection. Leave untouched unless user specifies otherwise. Config will be handled before invokation if remote is needed.")] = False
) -> GufiQueryResult:
    """
        sql query on local file information index
    """

    query_result = GufiQueryResult()

    # Build SQL line
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
        if not searchpath:
            raise RuntimeError(f"Error: Index {index} not found")
        threads = os.cpu_count() or 1

        vt_config = [
            util.sqlite_string(searchpath),
            f"threads={threads}",
            "min_level=1",
            "max_level=99",
            "verbose=0",
        ]

    logical_sql = util.normalize_gufi_query_sql(f"{sqlin.strip()} {wherein.strip()}".strip())
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
        query: Annotated[GufiQuery, Field(description="Constructed object for the aggregate SQL query.")],
) -> GufiQueryResult:

    index = query.index
    if not util.subpath_exists(index):
        raise RuntimeError(f"Error: Index or subpath {index} does not exist")

    if not util.validate_aggregate_order(query):
        raise RuntimeError(f"Error: Aggregate order is not valid")

    query_result = GufiQueryResult()

    options = [util.sqlite_string(util.resolve_query_index(query.index))]
    for config in query.config:
        options.append(config)
    for item in query.sql_options:
        option = item.option.lstrip("-")
        sql = util.ensure_sql_statement(item.sql)
        options.append(f"{option}={util.sqlite_string(sql, '"')}")

    sql_query = f"""
        CREATE VIRTUAL TABLE temp.gufi
        USING gufi_vt({", ".join(options)})
    """
    query_result.sql = sql_query

    conn = sqlite3.connect(":memory:")
    try:
        conn.enable_load_extension(True)
        conn.load_extension(GUFIVTLIB)
        conn.enable_load_extension(False)

        cursor = conn.cursor()
        cursor.execute(sql_query)
        cursor.execute("SELECT * FROM temp.gufi")
        result = cursor.fetchall()

        # Format result into serialized object
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
        finally:
            conn.close()


'''
gufi_query \
-I "CREATE TABLE intermediate(size INT64);" \
-E "INSERT INTO intermediate SELECT size FROM entries WHERE type = 'f';" \
-K "CREATE TABLE aggregate(total INT64);" \
-J "INSERT INTO aggregate SELECT SUM(size) FROM intermediate;" \
-G "SELECT SUM(total) FROM aggregate;" \
-d '|' -n 32 index
'''
qobj = {
            "index": "Downloads",
            "config": ["threads=32"],
            "sql_options": [
                {
                    "option": "-I",
                    "sql": "CREATE TABLE intermediate(size INT64)"
                },
                {
                    "option": "-E",
                    "sql": "INSERT INTO intermediate SELECT size FROM entries WHERE type='f'"
                },
                {
                    "option": "-K",
                    "sql": "CREATE TABLE aggregate(total INT64)"
                },
                {
                    "option": "-J",
                    "sql": "INSERT INTO aggregate SELECT SUM(size) FROM intermediate"
                },
                {
                    "option": "-G",
                    "sql": "SELECT SUM(total) FROM aggregate"
                }
            ]
        }

@mcp.tool()
def gufi_ls(
        path: Annotated[str, Field(description="Optional subpath of gufi_ls. This path should have the desired index as the root, since the underlying tool resolves from a configured root.")] = None,
        options: Annotated[list[str], Field(description="Options to submit for gufi_ls. submit '--help' to view these options. Do not use --delim, the tool uses its own to give structured output. A value supplied after an option should be a new list entry.")] = None
) -> GufiToolResult:
    ''' Execute user-facing tool: gufi equivalent of ls '''

    tool_result = GufiQueryResult()
    cmd = ["gufi_ls"]
    # Build options string if options passed
    if options:
        for option in options:
            cmd.append(option)

    # Add delimiter explicitly for parsing
    cmd.append("--delim")
    cmd.append("\t")
    if path:
        cmd.append(path)

    result = subprocess.run(cmd, capture_output=True, text=True)

    # Pack object and return
    tool_result.rows = [res_row.split("\t") for res_row in result.stdout.strip().split("\n")]
    tool_result.row_count = len(tool_result.rows)

    return tool_result.model_dump()

@mcp.tool()
def gufi_du(
        subpath: Annotated[str, Field(description="Optional subpath to input for du. This directory must have treesummary active to get results.")] = None,
        options: Annotated[list[str], Field(description="Options to submit for gufi_du. submit '--help' to view these options. A value supplied after an option should be a new list entry.")] = None
) -> GufiToolResult:
    ''' Execute user-facing tool: gufi equivalent of ls '''

    tool_result = GufiQueryResult()

    cmd = ["gufi_du"]
    # Build options string if options passed
    if options:
        for option in options:
            cmd.append(option)

    if subpath:
        cmd.append(subpath)

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.stderr:
        # treesummary existence error at subpath
        if "have treesummary data?" in result.stderr:
            return [[f"Treesummary error: The subpath {subpath} does not have a treesummary table, cannot use gufi_du."]]

    # Pack object and return
    tool_result.rows = [res_row.split("\t") for res_row in result.stdout.strip().split("\n")]
    tool_result.row_count = len(tool_result.rows)
    return tool_result.model_dump()

@mcp.tool()
def gufi_find(
        options: Annotated[list[str], Field(description="Options to submit for gufi_find. submit '--help' to view these options. Do not use --delim, the tool uses its own to give structured output. A value supplied after an option should be a new list entry.")] = None
) -> GufiToolResult:
    ''' Execute user-facing tool: gufi equivalent of find '''

    tool_result = GufiQueryResult()

    cmd = ["gufi_find"]
    # Build options string if options passed
    if options:
        for option in options:
            cmd.append(option)

    cmd.append("--delim")
    cmd.append("\t")

    result = subprocess.run(cmd, capture_output=True, text=True)

    # Pack object and return
    tool_result.rows = [res_row.split("\t") for res_row in result.stdout.strip().split("\n")]
    tool_result.row_count = len(tool_result.rows)
    return tool_result.model_dump()

@mcp.tool()
def gufi_stat(
        file: Annotated[str, Field(description="File to stat.")],
        options: Annotated[list[str], Field(description="Options to submit for gufi_stat. submit '--help' to view these options. A value supplied after an option should be a new list entry.")] = None
) -> GufiToolResult:
    ''' Execute user-facing tool: gufi equivalent of ls '''

    tool_result = GufiQueryResult()

    cmd = ["gufi_stat"]
    # Build options string if options passed
    if options:
        for option in options:
            cmd.append(option)

    cmd.append(file)

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.stderr:
        # treesummary existence error at subpath
        if "No such file or directory" in result.stderr:
            return [[f"File error: gufi_stat was not able to find the file {file}."]]

    # Pack object and return
    tool_result.rows = [res_row.split("\t") for res_row in result.stdout.strip().split("\n")]
    tool_result.row_count = len(tool_result.rows)
    return tool_result.model_dump()

@mcp.tool()
def gufi_stats(
        path: Annotated[str, Field(description="Path to obtain statistic from. Resolved with tool internally.")],
        stat: Annotated[str, Field(description="Statistic to use. Use the --help option to view these statistics.")],
        options: Annotated[list[str], Field(description="Options to submit for gufi_stats. submit '--help' to view these options. Do not use --delim, the tool uses its own to give structured output. A value supplied after an option should be a new list entry.")] = None
) -> GufiToolResult:
    ''' Execute user-facing tool: gufi equivalent of ls '''

    tool_result = GufiQueryResult()

    cmd = ["gufi_stats"]
    help = False
    # Build options string if options passed
    if options:
        for option in options:
            if option == "--help":
                help = True
            cmd.append(option)

    # Override previous build for clean help, since required vars are used
    if help:
        cmd = ['gufi_stats', '--help']
        result = subprocess.run(cmd, capture_output=True, text=True)
    else:
        cmd.append(stat)
        cmd.append(path)
        cmd.append("--delim")
        cmd.append("\t")

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.stderr:
        # treesummary existence error at subpath
        if "argument stat: invalid choice:" in result.stderr:
            return [[f"Tool error: gufi_stats does not allow getting statistic {stat}."]]
        if "No such file or directory" in result.stderr:
            return [[f"Path error: gufi_stats was not able to find the path {path}."]]

    # Pack object and return
    tool_result.rows = [res_row.split("\t") for res_row in result.stdout.strip().split("\n")]
    tool_result.row_count = len(tool_result.rows)
    return tool_result.model_dump()

@mcp.tool()
def gufi_getfattr(
        path: Annotated[str, Field(description="Path to obtain fattrs from. Resolved with tool internally.")],
        options: Annotated[list[str], Field(description="Options to submit for gufi_stats. submit '--help' to view these options. Do not use --delim, the tool uses its own to give structured output. A value supplied after an option should be a new list entry.")] = None
) -> GufiToolResult:
    ''' Execute user-facing tool: gufi equivalent of get_fattr '''

    tool_result = GufiQueryResult()

    cmd = ["gufi_getfattr"]
    help = False
    # Build options string if options passed
    if options:
        for option in options:
            if option == "--help":
                help = True
            cmd.append(option)

    # Override previous build for clean help, since required vars are used
    if help:
        cmd = ['gufi_fattr', '--help']
        result = subprocess.run(cmd, capture_output=True, text=True)
    else:
        cmd.append(path)
        cmd.append("--delim")
        cmd.append("\t")

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.stderr:
        # treesummary existence error at subpath
        if "argument stat: invalid choice:" in result.stderr:
            return [[f"Tool error: gufi_stats does not allow getting statistic {stat}."]]
        if "No such file or directory" in result.stderr:
            return [[f"Path error: gufi_stats was not able to find the path {path}."]]

    # Pack object and return
    tool_result.rows = [res_row.split("\t") for res_row in result.stdout.strip().split("\n")]
    tool_result.row_count = len(tool_result.rows)
    return tool_result.model_dump()

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


@mcp.prompt()
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
- Use the gufi://indexes resource to list known indexes and see whether each
  one has treesummary data.
- Use the gufi://schemas/{schema} resource to inspect available GUFI tables or
  a specific table schema before writing SQL.

Tool guidance:
- gufi_ls, gufi_du, gufi_find, gufi_stat, and gufi_stats are user-facing GUFI
  command wrappers. Use these for high-level searches, quick metadata checks,
  and common filesystem-style questions. Some commands need a rolled-up index
  with treesummary data.
- sql_file_index runs a direct SQL SELECT against gufi_vt table-valued
  functions. Use it for focused one-line queries over entries, pentries,
  summary, treesummary, vrpentries, or vrsummary when a command wrapper is not
  precise enough.
- aggregate_sql_query creates a temporary gufi_vt virtual table and runs GUFI's
  aggregate pipeline. Use it for multi-stage calculations, grouped totals, or
  cases where intermediate tables make the query clearer or faster.

Treesummary nuance:
treesummary exists only when an index has been rolled up. It stores precomputed
subtree summary data and can make directory-level totals very fast. If an index
does not have treesummary, prefer entry-level or pentry-level queries, or use an
aggregate query that computes the needed value from available tables.

Aggregate SQL pipeline:
- I initializes per-thread intermediate tables, usually with CREATE TABLE
  intermediate(...).
- T, S, and E are the per-tree, per-summary, and per-entry SQL phases. These
  usually INSERT rows into intermediate during aggregation. Use only the phases
  needed for the question.
- K initializes the final aggregate table, usually with CREATE TABLE
  aggregate(...).
- J merges each intermediate table into the aggregate table.
- G selects the final result rows from aggregate. The MCP tool returns these
  rows as typed SQLite values.
- F is optional final SQL/cleanup used after query processing.

For aggregate_sql_query, pass SQL phases as sql_options with options like -I,
-E, -K, -J, and -G. Pass gufi_vt configuration such as threads=32 in config.
The delimiter option from gufi_query is not needed because gufi_vt returns typed
rows directly through SQLite.
"""

if __name__ == "__main__":
    # Run the server with HTTP transport
    mcp.run(transport=MCPTRANSPORT, host=MCPSRVHOST, port=MCPSRVPORT)
