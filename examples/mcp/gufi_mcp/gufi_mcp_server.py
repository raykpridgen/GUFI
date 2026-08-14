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
from typing import TypedDict
import asyncio
import sqlite3
import sys
import subprocess
import os
import json
import re
from dotenv import load_dotenv
from pathlib import Path


from sqlglot import parse_one, ParseError


class FileEntry(TypedDict):
    name: str
    size: int
    uid: str


load_dotenv()

SCHEMAFILE='./schemas.json'
REMOTEHOST='<remote uri>'
MCPTRANSPORT='streamable-http'
MCPSRVHOST='127.0.0.1'
MCPSRVPORT=8000
GUFIVTLIB='path/to/gufi_vt'
GUFI_EXE=os.getenv('GUFI_EXECUTABLE')
GUFI_INDEXES_ROOT=os.getenv('GUFI_INDEXES_ROOT')

mcp = MCPServer(
    name="server"
)

'''
--------------- HELPER FUNCTIONS ---------------
'''

def is_valid_sql_query(sql_query: str, dialect: str = "snowflake") -> bool:
    try:
        parse_one(sql_query, read=dialect)
        return True
    except ParseError as e:
        print(f"SQL Grammar Error: {str(e)}", file=sys.stderr)
        return False

'''
--------------- TOOLS ---------------
'''

@mcp.tool()
def gufi_version() -> str:
    """Return the version string of the configured gufi_query executable."""
    result = subprocess.run([GUFI_EXE, "--version"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout
    else:
        return result.stderr

@mcp.tool()
def gufi_query_find_largest_files(index: str, return_count: int) -> list[FileEntry]:
    """Find the largest files in a GUFI index. Returns the top return_count files sorted by size descending."""
    index_root = Path(GUFI_INDEXES_ROOT + index)

    if not index_root.is_dir():
        return []

    # Select largest file in each subdir
    result = subprocess.run(
        [GUFI_EXE, "-d", "\t", "-E",
         "SELECT name, size, uid FROM vrpentries ORDER BY size DESC",
         f"{GUFI_INDEXES_ROOT}{index_root.name}/"],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        return []

    # Split entries
    rows: list[FileEntry] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            # Append to a structured format
            try:
                rows.append(FileEntry(
                    name=parts[0],
                    size=int(parts[1]),
                    uid=parts[2] if len(parts) > 2 else ""
                ))
            except ValueError:
                continue

    # Sort rows and return by how many requested
    rows.sort(key=lambda r: r["size"], reverse=True)
    return rows[:return_count]

@mcp.tool()
def gufi_query_local_index(
        index: str,
        sql_query: str,
        return_limit: int = 0
) -> str:
    """Run a SQL query against a local GUFI index. return_limit=0 means no limit."""

    index_root = Path(GUFI_INDEXES_ROOT + index)

    if not index_root.is_dir():
        return f"Error: index '{index}' not found.\n"

    if not is_valid_sql_query(sql_query, dialect="sqlite"):
        return "Error: invalid SQL syntax.\n"

    with open(SCHEMAFILE, "r") as file:
        data = json.load(file)

    match_table = re.search(r"\bFROM\b\s+(\w+)", sql_query, re.IGNORECASE)
    if match_table is None:
        return "Error: could not determine table from query.\n"
    table_found = match_table.group(1)
    if table_found not in data:
        return "Error: invalid table submitted. Inspect schemas for tables to query.\n"

    result = subprocess.run(
        [GUFI_EXE, "-d", "\t", "-E", sql_query, f"{GUFI_INDEXES_ROOT}{index_root.name}/"],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        return f"Error: gufi_query failed: {result.stderr}\n"

    lines = result.stdout.splitlines()
    return "\n".join(lines[:return_limit] if return_limit else lines)

'''
--------------- PROMPTS ---------------
'''

@mcp.prompt()
def find_biggest_files(index: str):
    ''' Prompt agent to find biggest files in an index '''
    return f"Please go find the largest files within the {index} index. Thank you."

'''
--------------- RESOURCES ---------------
'''

@mcp.resource("gufi://indexes")
def gufi_indexes() -> dict[str, str]:
    index_root = Path(GUFI_INDEXES_ROOT)
    indexes = {}

    if index_root.exists() and index_root.is_dir():
        # Check for any GUFI indexes
        for entry in index_root.iterdir():
            # Keep path of index for return
            index_path = GUFI_INDEXES_ROOT + entry.name + "/"
            # For an entry, check for db next layer down
            if entry.is_dir() and Path(index_path + "db.db").is_file():
                indexes[entry.name] = index_path

    return indexes

@mcp.resource("gufi://schemas/{schema}")
def gufi_schemas_search(schema: str = "query_surfaces") -> dict[str, str]:
    ''' Discover common tables and their schemas '''

    with open(SCHEMAFILE, "r") as file:
        data = json.load(file)

    if (schema in data):
        if schema == "query_surfaces":
            return data[schema]
        else:
            return {schema: data[schema]}
    else:
        return {"error": "Schema not found, input query_surfaces for available schemas."}

'''
@mcp.tool()
def local_file_index(sqlin: str, wherein: str, searchpath: str) -> list[str]:
  """
       sql query on local file information index
  """
  conn=sqlite3.connect(':memory:')
  try:
    conn.enable_load_extension(True)
    cursor = conn.cursor()
    conn.load_extension(GUFIVTLIB)
    conn.enable_load_extension(False)
    sqlline='%s(\'%s\',1,1,99,NULL,1) %s' % (sqlin,searchpath,wherein)
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

@mcp.tool()
def remote_file_index(sqlin: str, wherein: str, searchpath: str) -> list[str]:
  """
       sql query on remote file information index
  """
  conn=sqlite3.connect(':memory:')
  try:
    conn.enable_load_extension(True)
    cursor = conn.cursor()
    conn.load_extension(GUFIVTLIB)
    conn.enable_load_extension(False)
    sqlline='%s(\'%s\',1,0,99,NULL,0,\'ssh\',\'%s\') %s' % (sqlin,searchpath,REMOTEHOST,wherein)
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


@mcp.tool()
def gufi_location(a: str) -> str:
    """gufi_query location"""
    result = subprocess.run(["which", GUFI_EXE], capture_output=True, text=True)
    return str(result)
'''



if __name__ == "__main__":
    # Run the server with HTTP transport
    mcp.run(transport=MCPTRANSPORT, host=MCPSRVHOST, port=MCPSRVPORT)
