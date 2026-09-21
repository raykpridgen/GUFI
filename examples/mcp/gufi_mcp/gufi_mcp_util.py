import sqlite3
import sys
from pathlib import Path
from typing import Any
from sqlglot import parse_one, ParseError
import os
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import re
import subprocess

load_dotenv()

SCHEMAFILE = os.getenv('SCHEMAFILE')
REMOTEHOST = os.getenv('REMOTEHOST')
MCPTRANSPORT = os.getenv('MCPTRANSPORT')
MCPSRVHOST = os.getenv('MCPSRVHOST')
MCPSRVPORT = os.getenv('MCPSRVPORT')
GUFIVTLIB = os.getenv('GUFIVTLIB')
GUFI_INDEX_ROOT = os.getenv('GUFI_INDEX_ROOT')
GUFI_QUERY = os.getenv('GUFI_QUERY')
from enum import Enum

class GufiSQLOption(str, Enum):
    I = "-I"
    T = "-T"
    S = "-S"
    E = "-E"
    K = "-K"
    J = "-J"
    G = "-G"
    F = "-F"

# Object for GUFI options
class GufiOption(BaseModel):
    option: GufiSQLOption
    sql: str

# Object to handle query construction
class GufiQuery(BaseModel):
    index: str
    sql_options: list[GufiOption] = Field(default_factory=list)
    config: list[str] = Field(default_factory=list)
    delimiter: str = "\t"

# Object to handle query returns
class GufiQueryResult(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0

# Object for tool results
class GufiToolResult(BaseModel):
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0

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

def resolve_local_index(index: str) -> str:
    ''' resolve name of a local index to the full path '''

    if index not in get_gufi_indexes():
        raise RuntimeError("Error: Index provided not found at index root.")

    return f'{GUFI_INDEX_ROOT}{index}'

def resolve_remote_index(index: str) -> str:
    ''' resolve name of a remote index to the full path '''

    # SELECT * FROM paths WHERE path ~ '^/[^/]+/?$';

    return f'{index}'

def get_gufi_indexes() -> list[str]:
    index_root = Path(GUFI_INDEX_ROOT).resolve()
    if not index_root.exists():
        raise RuntimeError("Error: GUFI index root does not exist.")
    indexes = []

    # Confirm path is a directory with other dirs inside
    if index_root.is_dir():
        for entry in index_root.iterdir():
            if not entry.is_dir():
                continue
            indexes.append(entry.name)

    return indexes

def execute_sql(sqlline: str, gufi_vt: bool, plain_index: str = None) -> list[Any]:
    ''' Wrapper to execute SQL queries '''

    # GUFI_VT uses memory and operates through the function
    if gufi_vt:
        conn = sqlite3.connect(':memory:')
    # Pulling schema needs a path to an index
    else:
        # Specific path / table to connect to
        if plain_index:
            index = f"{plain_index}" + "/db.db"
        else:
            index = f"{GUFI_INDEX_ROOT}" + "/db.db"
        conn = sqlite3.connect(index)

    # Handle SQLITE3 errors
    try:
        # Using gufi_vt extension
        if gufi_vt:
            conn.enable_load_extension(True)
            cursor = conn.cursor()
            conn.load_extension(GUFIVTLIB)
            conn.enable_load_extension(False)
        # Plain SQL query
        else:
            cursor = conn.cursor()

        print(sqlline, file=sys.stderr)

        # Call GUFI_VT
        cursor.execute(sqlline)

        return cursor.fetchall()

    except sqlite3.Error as e:
        print(f"An SQLite error occurred: {e}",file=sys.stderr)
        conn.close()
        return [f"sql error:", f"{str(e)}"]

    finally:
        conn.close()

def has_treesummary(index: str) -> bool:

    if index == GUFI_INDEX_ROOT:
        response = execute_sql("SELECT name, type FROM sqlite_master WHERE sql IS NOT NULL", False)

    elif index not in get_gufi_indexes():
        raise RuntimeError("Error: Index provided not found at index root.")

    else:
        response = execute_sql("SELECT name FROM sqlite_master WHERE sql IS NOT NULL AND name == 'treesummary'", False, resolve_local_index(index))

    if len(response) == 0:
        return False
    else:
        return True

def subpath_exists(path: str) -> bool:
    ''' Confirm a subpath exists within gufi index root '''
    result = subprocess.run(["gufi_ls", f"{path}"], capture_output=True, text=True)
    if result.stdout:
        return True
    else:
        return False

def validate_aggregate_order(query: GufiQuery) -> bool:
    ''' Validate the aggregate order of gufi queries '''

    return True
