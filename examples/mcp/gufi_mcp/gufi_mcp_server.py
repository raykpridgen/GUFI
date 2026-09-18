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
import subprocess
from typing import Any
import os
from dotenv import load_dotenv
from gufi_mcp_util import GufiQueryResult, GufiToolResult
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
def sql_file_index(sqlin: str, wherein: str, index: str, remote: bool = False) -> GufiQueryResult:
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
        # Format result into serialized object
        query_result.rows = [list(res_row) for res_row in result]
        query_result.columns = util.get_columns_from_sqlin(sqlin)
        query_result.row_count = len(query_result.rows)
        return query_result

    else:
        raise RuntimeError(f"Error executing SQL: {result[1]}")

@mcp.tool()
def gufi_ls(path: str = None, options: list[str] = None) -> GufiToolResult:
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

    return tool_result

@mcp.tool()
def gufi_du(subpath: str = None, options: list[str] = None) -> GufiToolResult:
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
    return tool_result

@mcp.tool()
def gufi_find(options: list[str] = None) -> GufiToolResult:
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
    return tool_result

@mcp.tool()
def gufi_stat(file: str, options: list[str] = None) -> GufiToolResult:
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
    return tool_result

@mcp.tool()
def gufi_stats(path: str, stat: str, options: list[str] = None) -> GufiToolResult:
    ''' Execute user-facing tool: gufi equivalent of ls '''

    tool_result = GufiQueryResult()

    cmd = ["gufi_stats"]
    # Build options string if options passed
    if options:
        for option in options:
            cmd.append(option)

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
    return tool_result

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
def gufi_schemas(schema: str = "all") -> list[list[str]]:
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

if __name__ == "__main__":
    # Run the server with HTTP transport
    mcp.run(transport=MCPTRANSPORT, host=MCPSRVHOST, port=MCPSRVPORT)
