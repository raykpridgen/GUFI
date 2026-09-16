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
import asyncio
import sqlite3
import sys
import subprocess
import shutil
from pathlib import Path
from typing import Any, TypedDict
from dataclasses import dataclass, field
from sqlglot import parse_one, ParseError
import sqlglot.expressions as exp
import os
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from gufi_mcp_util import GufiQueryResult, GufiQuery, GufiOption
import gufi_mcp_util as util
import re

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
def local_file_index_schema() -> str:
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

@mcp.tool()
def sql_local_file_index(sqlin: str, wherein: str, index: str, remote: bool = False) -> GufiQueryResult:
    """
        sql query on local file information index
    """

    query_result = GufiQueryResult()

    # Verify index existence on local queries
    if not remote:
        searchpath = util.resolve_index(index)
        if not searchpath:
            raise RuntimeError(f"Error: Index {index} not found")

    conn=sqlite3.connect(':memory:')
    try:
        # Load GUFI_VT extension and connect
        conn.enable_load_extension(True)
        cursor = conn.cursor()
        conn.load_extension(GUFIVTLIB)
        conn.enable_load_extension(False)

        # Build SQL line
        if remote:
            sqlline = '%s(\'%s\',1,0,99,NULL,0,\'ssh\',\'%s\') %s' % (sqlin, searchpath, REMOTEHOST, wherein)
        else:
            sqlline = '%s(\'%s\',1,1,99,NULL,1) %s' % (sqlin, searchpath, wherein)

        print(sqlline, file=sys.stderr)

        # Call GUFI_VT
        cursor.execute(sqlline)

        # Format result into serialized object
        query_result.columns = util.get_columns_from_sqlin(sqlin)
        query_result.rows = [list(res_row) for res_row in cursor.fetchall()]
        query_result.row_count = len(query_result.rows)
        return query_result
        conn.close()

    except sqlite3.Error as e:
        print(f"An SQLite error occurred: {e}",file=sys.stderr)
        conn.close()
        return f"Error executing query: {str(e)}"

    finally:
        conn.close()

# resource that returns indexes available
@mcp.resource("gufi://indexes")
def gufi_indexes() -> list[str]:
    ''' Access list of available gufi indexes '''
    return util.get_gufi_indexes()

@mcp.resource("gufi://schemas/{schema}")
def gufi_schemas(schema: str = "all") -> list[str]:
    """
        Return schemas of each gufi index table
    """

    # Pick first index to get schemas from
    index = f"{util.resolve_index(util.get_gufi_indexes()[0])}" + "/db.db"

    conn=sqlite3.connect(index)

    try:
        conn.enable_load_extension(True)
        cursor = conn.cursor()
        conn.load_extension(GUFIVTLIB)
        conn.enable_load_extension(False)
        # select path,name,size from gufi_vt_pentries(\'%s\',1,0,99,NULL,0,\'ssh\',\'%s\') where name like \'%\' limit 50
        # LOCALWHERE='where name like \'%\' limit 50'
        # LOCALSEARCHPATH='documents'
        sqlline=f'SELECT name, type, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name'
        print(sqlline, file=sys.stderr)
        cursor.execute(sqlline)
        rows = cursor.fetchall()
        for row in rows:
            yield row
        conn.close()
    except sqlite3.Error as e:
        print(f"An SQLite error occurred: {e}",file=sys.stderr)
        conn.close()
        return f"Error executing query: {str(e)}"
    finally:
        conn.close()
        x=1
        return ''



for row in gufi_schemas("asd"):
    print(row)

if __name__ == "__main__":
    # Run the server with HTTP transport
    mcp.run(transport=MCPTRANSPORT, host=MCPSRVHOST, port=MCPSRVPORT)
