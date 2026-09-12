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
import errno

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
from dotenv import load_dotenv

load_dotenv()

SCHEMAFILE = os.getenv('SCHEMAFILE')
REMOTEHOST = os.getenv('REMOTEHOST')
MCPTRANSPORT = os.getenv('MCPTRANSPORT')
MCPSRVHOST = os.getenv('MCPSRVHOST')
MCPSRVPORT = os.getenv('MCPSRVPORT')
GUFIVTLIB = os.getenv('GUVIVTLIB')
GUFI_INDEX_ROOT = os.getenv('GUFI_INDEX_ROOT')
GUFI_QUERY = os.getenv('GUFI_QUERY')

mcp = MCPServer(name="gufi_mcp_server")

# Object to handle query returns
@dataclass
class GufiQueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0

    def parse_result(self, stdout: str, delimiter: str) -> None:
        lines = stdout.strip().splitlines()

        if not lines:
            return

        self.columns = lines[0].split(delimiter)
        self.rows = [line.split(delimiter) for line in lines[1:]]
        self.row_count = len(self.rows)

    def get_columns(self) -> list[str]:
        return self.columns

    def get_row_count(self) -> int:
        return self.row_count

    def get_rows(self, start, stop) -> list[list[Any]]:
        if stop > self.row_count or start < 0:
            return None

        return self.rows[start:stop]
        

# Object to handle query construction
@dataclass
class GufiQuery():
    index: str
    options: list[tuple[str, str]] = field(default_factory=list)
    delimiter: str = "\t"

    sql_specifiers: set[str] = field(
        default_factory=lambda: {"-I", "-T", "-S", "-E", "-J", "-K", "-G", "-F"}
    )

    config: set[str] = field(
        default_factory=lambda: {"-a"}
    )

    result: GufiQueryResult = field(default_factory=GufiQueryResult)


    def __init_validate__(self):
        # Check that index exists
        if self.index not in get_gufi_indexes():
            raise RuntimeError("Failed to create query, index does not exist.")

    def add_option(self, tag: str, option: str):
        ''' Add an option to a query '''

        # Check for valid specifier
        if tag in self.sql_specifiers:
            # Check that SQL supplied is valid
            if not is_valid_sql_query(option, dialect="sqlite"):
                raise RuntimeError(f'Query: {option} is not a valid SQL query')

        # Check for config
        elif tag in self.config:
            # Invalid short circuit specifier
            if tag == "-a" and option not in ("0", "1", "2"):
                raise RuntimeError(f"Config for -a must be 0, 1, or 2")

        else:
            raise RuntimeError(f'Specifier: {tag} is not a valid option')

        self.options.append((tag,option))
        

    def validate_query(self) -> bool:
        return True

    def build_query_command(self) -> list[str]:

        # Add delimiter at the end to parse correctly
        self.options.append(("-d",self.delimiter))

        cmd = []
        cmd.append(GUFI_QUERY)
        for option in self.options:
            cmd.append(option[0])
            cmd.append(option[1])
        cmd.append(f"{GUFI_INDEX_ROOT}{self.index}")

        return cmd


'''   NEW   '''

def is_valid_sql_query(sql_query: str, dialect: str = "sqlite") -> bool:
    try:
        parse_one(sql_query, read=dialect)
        return True
    except ParseError as e:
        print(f"SQL Grammar Error: {str(e)}", file=sys.stderr)
        return False

def table_exists(index: str, table_name: str) -> bool:
    ''' Confirm existence of a table based on an index and table name '''

    conn = sqlite3.connect(f'{GUFI_INDEX_ROOT}{index}/db.db')
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
            AND name = ?
        """, (table_name,))

        return cursor.fetchone() is not None

    finally:
        conn.close()

def get_gufi_indexes() -> list[str]:
    index_root = Path(GUFI_INDEX_ROOT).resolve()
    if not index_root.exists():
        raise RuntimeError("Error: GUFI index root does not exist")
    indexes = []

    # Confirm path is a directory with other dirs inside
    if index_root.is_dir():
        for entry in index_root.iterdir():
            if not entry.is_dir():
                continue
            indexes.append(entry.name)

    return indexes

def execute_gufi_query(query: GufiQuery) -> GufiQuery:
    ''' Helper function to execute gufi queries '''

    allowed_prefixes = ('SELECT', 'SHOW', 'DESC', 'DESCRIBE', 'USE')

    # Do validation for a query to ensure safety

    '''
    ### Get folders size from index
        gufi_query \
        -I "CREATE TABLE intermediate(size INT64);" \
        -E "INSERT INTO intermediate SELECT size FROM entries WHERE type = 'd';" \
        -K "CREATE TABLE aggregate(total INT64);" \
        -J "INSERT INTO aggregate SELECT SUM(size) FROM intermediate;" \
        -G "SELECT SUM(total) FROM aggregate;" \
        -d '|' -n 32 index
    
    Flags that dictate SQL passed
    -E entries tables
    -S summary tables
    -T tresummary table
    -I init intermediate table
    -K init final aggregate table
    -J insert from intermediate to aggregate table
    -G final select from aggregate table
    -F SQL cleanup
    
    Validation paradigms for valid gufi_query execution
        - I flag must preceed every other flag
            This will run once per thread, making per-thread tables
            Anything references by future steps must exist in an I statement preceeding it
        - E, S, T can occur multiple times and can also contain multiple SQL statements separated by semicolons
        - However by default
            If -T does not return, stop otherwise execute S
            If -S does not return stop otherwise go to E
            etc
            -a changes short circuit behavior, BUT this should not be rigorously checked and should be left to agent to confirm through schema and other resources
        - using T flag is only allowed when index has a treesummary table at the root
        - K flag must precede J, it creates aggregation before insertion
        - J flag aggregates parallel intermediate tables into aggregate table
        - G runs SQL against final aggregation table, should be one statement
        - F executes once per thread, after every other operation only
            Should not be required for ordinary queries, but for aggregates like being detailed
    
    Potential Rule Structure
    - I supplied first, any after others causes rejection
    - At least one S, T, E
    - Order for -T -> -S -> -E
    - Last statement in T, S, E accounts for short circuiting
    - T can encounter dirs without treesummary
    - K precedes J
    - J is dependent on existing intermediate and aggregate tables
    - G operates on aggregate results
    - F occurs after traversal only
    - -a changes short circuiting semantics
    
    Potential Stages:
    - CLI validation: are things arranged sensibly?
    - SQL validation: is each statement valid according to SQL semantics?
    - Dependency validation: do all components have existing dependencies when they are called?
    - GUFI validation: Do requested tables and views adhere to GUFI?
    
    '''

    # CLI validation

    # SQL validation

    #is_allowed_query = any(query.startswith(prefix) for prefix in allowed_prefixes)
    #if not is_allowed_query:
    #    raise RuntimeError(f"Query {query} not allowed, must use a read-only prefix.")

    # Dependency validation

    # GUFI validation


    # Execution
    execution_result = subprocess.run(query.build_query_command(), capture_output=True, text=True)
    if execution_result.stderr:
        print(execution_result.stderr)

    query.result.parse_result(execution_result.stdout, query.delimiter)

    tmp = 1 + 2

idx = get_gufi_indexes()
query = GufiQuery('pictures')
query.add_option("-E", "SELECT name, size FROM entries WHERE size > 1048576;")
execute_gufi_query(query)
print(query.result.get_columns())
print(query.result.get_row_count())
print(query.result.get_rows(0, 5))

# manage path for isolation

# automated churn for schema dump -> resource for agent

# resource that returns indexes available
@mcp.resource("gufi://indexes")
def gufi_indexes() -> list[str]:
    ''' Access list of available gufi indexes '''
    return get_gufi_indexes()


# tool that accesses treesummary of an index
@mcp.tool()
def gufi_treesummary(index: str, query: str) -> GufiQueryResult:
    ''' Query the treesummary table of an index - if present '''

    # Check that index requested exists
    if index not in get_gufi_indexes():
        raise RuntimeError(f"Index {index} not found.")

    # confirm existence of treesummary in the index
    if not table_exists(index, "treesummary"):
        raise RuntimeError(f"Index {index} does not have treesummary indexed.")




# tool that exposes path discovery

# resource scheme for schemas etc

# prompt that brief per session

'''   KEEP   '''

@mcp.tool()
def gufi_version() -> str:
    """gufi_query -- version"""
    result = subprocess.run(["gufi_query", "--version"], capture_output=True, text=True)
    return str(result.stdout)


'''   DISCARD   '''


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
def local_file_index(sqlin: str, searchpath: str, wherein: str = '') -> list[str]:
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
def gufi_location() -> str:
    """gufi_query location"""
    result = subprocess.run(["which", "gufi_query"], capture_output=True, text=True)
    return str(result.stdout)

if __name__ == "__main__":
    # Run the server with HTTP transport
    mcp.run(transport=MCPTRANSPORT, host=MCPSRVHOST, port=MCPSRVPORT)