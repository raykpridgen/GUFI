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

# Object to handle query returns
class GufiQueryResult(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0

# Object for GUFI options
class GufiOption(BaseModel):
    option: str
    sql: str

# Object to handle query construction
class GufiQuery(BaseModel):
    index: str
    options: list[GufiOption] = Field(default_factory=list)
    delimiter: str = "\t"


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

def get_columns_from_sqlin(sqlin) -> list[str]:
    ''' Separate out column names into a list '''
    col_string = re.match(r'(?i)SELECT\s+(.+?)\s+FROM', sqlin, flags=re.IGNORECASE | re.DOTALL).group(1)
    return [col.strip() for col in col_string.split(',')]

def resolve_index(index: str) -> str:
    ''' resolve name of an index to the full path '''

    if index not in get_gufi_indexes():
        raise RuntimeError("Error: Index provided not found at index root.")

    return f'{GUFI_INDEX_ROOT}{index}'

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

def execute_gufi_query(query: GufiQuery) -> GufiQueryResult:
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

    # is_allowed_query = any(query.startswith(prefix) for prefix in allowed_prefixes)
    # if not is_allowed_query:
    #    raise RuntimeError(f"Query {query} not allowed, must use a read-only prefix.")

    # Dependency validation

    # GUFI validation

    # Execution
