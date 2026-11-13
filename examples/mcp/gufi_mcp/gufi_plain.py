"""Plain gufi_query command submission for the ablation (GUFI_ABLATION_SUBMISSION=plain).

The agent writes the aggregate pipeline the way a user would type it for gufi_query:

    gufi_query -I "CREATE TABLE inter(gid INT64, size INT64)" \
               -E "INSERT INTO inter SELECT gid, size FROM vrpentries WHERE type = 'l'" \
               -K "CREATE TABLE agg(gid INT64, links INT64, bytes INT64)" \
               -J "INSERT INTO agg SELECT gid, COUNT(*), SUM(size) FROM inter GROUP BY gid" \
               -G "SELECT gid, SUM(links), SUM(bytes) FROM agg GROUP BY gid" \
               myindex

parse_plain_command() turns that into the same payload the structured tool takes
(per_directory_shape/sql, partial_shape/sql, final_sql, index). The server then validates and
runs it exactly like a structured query, so only the submission format differs.

This module needs no GUFI environment, so the eval harness can import it too.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Optional

import sqlglot
from sqlglot import exp

PER_DIRECTORY_TABLE = "per_directory"
PARTIAL_TABLE = "partial"
PER_DIRECTORY_FLAGS = ("-T", "-S", "-E")
VALUE_FLAGS = ("-I", "-K", "-J", "-G", "-n", *PER_DIRECTORY_FLAGS)
IGNORED_FLAGS = ("-n",)  # thread count: the server picks it

USAGE = (
    "gufi_query -I \"CREATE TABLE <t>(<col> <TYPE>, ...)\" "
    "[-T|-S|-E \"INSERT INTO <t> SELECT ...\"]... "
    "-K \"CREATE TABLE <a>(<col> <TYPE>, ...)\" "
    "-J \"INSERT INTO <a> SELECT ... FROM <t>\" "
    "-G \"SELECT ... FROM <a>\" <index>"
)

_CREATE_RE = re.compile(r"^\s*CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*;?\s*$",
                        re.IGNORECASE | re.DOTALL)
_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*(\([^)]*\))?\s*(.*?)\s*;?\s*$",
                        re.IGNORECASE | re.DOTALL)


class PlainCommandError(ValueError):
    """The command does not follow the gufi_query aggregate form."""


def split_command(command: str) -> tuple[dict[str, list[str]], list[str]]:
    """{flag: [values]} and positional arguments of a gufi_query command line."""
    if not isinstance(command, str) or not command.strip():
        raise PlainCommandError(f"command is empty. Usage: {USAGE}")
    try:
        tokens = shlex.split(command.replace("\\\n", " "))
    except ValueError as e:
        raise PlainCommandError(f"cannot split the command like a shell would ({e}); check the quoting") from e
    if tokens and tokens[0].rsplit("/", 1)[-1] == "gufi_query":
        tokens = tokens[1:]
    flags: dict[str, list[str]] = {}
    positional: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("-") and len(token) > 1 and not token[1:].isdigit():
            if token not in VALUE_FLAGS:
                raise PlainCommandError(f"unsupported flag {token}; the aggregate form takes "
                                        f"{', '.join(VALUE_FLAGS)}. Usage: {USAGE}")
            if i + 1 >= len(tokens):
                raise PlainCommandError(f"{token} needs a value")
            flags.setdefault(token, []).append(tokens[i + 1])
            i += 2
        else:
            positional.append(token)
            i += 1
    return flags, positional


def _index_name(positional: list[str]) -> str:
    if len(positional) != 1:
        found = f"got {positional}" if positional else "got none"
        raise PlainCommandError(f"give exactly one index after the flags ({found}). Usage: {USAGE}")
    return positional[0].rstrip("/").rsplit("/", 1)[-1]


def parse_index(command: Any) -> Optional[str]:
    """Index named by a plain command, or None if it cannot be read."""
    try:
        return _index_name(split_command(command)[1])
    except PlainCommandError:
        return None


def _single(flags: dict[str, list[str]], flag: str, what: str) -> str:
    values = flags.get(flag, [])
    if len(values) != 1:
        raise PlainCommandError(f"give {flag} exactly once ({what}); got {len(values)}. Usage: {USAGE}")
    return values[0]


def _create(sql: str, flag: str) -> tuple[str, str]:
    match = _CREATE_RE.match(sql)
    if not match:
        raise PlainCommandError(f"{flag} must be CREATE TABLE <name>(<col> <TYPE>, ...); got {sql!r}")
    return match.group(1), " ".join(match.group(2).split())


def _insert(sql: str, flag: str, table: str) -> str:
    match = _INSERT_RE.match(sql)
    if not match:
        raise PlainCommandError(f"{flag} must be INSERT INTO {table} SELECT ...; got {sql!r}")
    if match.group(1).lower() != table.lower():
        raise PlainCommandError(f"{flag} inserts into {match.group(1)}, but the table created for it is {table}")
    if match.group(2):
        raise PlainCommandError(f"{flag}: leave out the column list after INSERT INTO {table}; "
                                "select the columns in the table's order instead")
    return match.group(3)


def _rename_table(sql: str, old: str, new: str) -> str:
    """Replace references to table old with new, splicing only the table names into the original text."""
    if old.lower() == new.lower():
        return sql
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except sqlglot.errors.ParseError:
        return sql  # the server's validator reports the syntax error
    edits = []
    for table in tree.find_all(exp.Table):
        if table.name.lower() == old.lower() and table.this.meta.get("start") is not None:
            meta = table.this.meta
            alias = "" if table.alias else f" AS {old}"
            edits.append((meta["start"], meta["end"] + 1, new + alias))
    for start, end, text in sorted(edits, reverse=True):
        sql = sql[:start] + text + sql[end:]
    return sql


def parse_plain_command(command: str) -> dict[str, Any]:
    """The structured aggregate payload equivalent to a gufi_query aggregate command."""
    flags, positional = split_command(command)
    index = _index_name(positional)
    inter, per_directory_shape = _create(_single(flags, "-I", "per-directory table"), "-I")
    agg, partial_shape = _create(_single(flags, "-K", "aggregate table"), "-K")
    if inter.lower() == agg.lower():
        raise PlainCommandError("-I and -K must create two different tables")
    per_directory_sql = [_insert(sql, flag, inter) for flag in PER_DIRECTORY_FLAGS for sql in flags.get(flag, [])]
    if not per_directory_sql:
        raise PlainCommandError(f"give at least one of -T, -S, -E (INSERT INTO {inter} SELECT ...). Usage: {USAGE}")
    partial_sql = _insert(_single(flags, "-J", "aggregate step"), "-J", agg)
    final_sql = _single(flags, "-G", "final query").strip().rstrip(";").strip()
    return {
        "index": index,
        "per_directory_shape": per_directory_shape,
        "per_directory_sql": per_directory_sql,
        "partial_shape": partial_shape,
        "partial_sql": _rename_table(partial_sql, inter, PER_DIRECTORY_TABLE),
        "final_sql": _rename_table(final_sql, agg, PARTIAL_TABLE),
    }


def _flag_for(sql: str) -> str:
    """Conventional gufi_query flag for a per-directory statement (the server assigns phases by table anyway)."""
    try:
        tables = [t.name.lower() for t in sqlglot.parse_one(sql, read="sqlite").find_all(exp.Table)]
    except sqlglot.errors.ParseError:
        tables = []
    name = tables[0] if tables else ""
    if name.startswith(("treesummary", "vtsummary")):
        return "-T"
    if "summary" in name:
        return "-S"
    return "-E"


def _quote(text: str) -> str:
    """Shell-quote SQL readably: double quotes when nothing inside needs escaping (SQL literals use single quotes)."""
    if not re.search(r'["$`\\!]', text):
        return f'"{text}"'
    return shlex.quote(text)


def to_plain_command(query: dict[str, Any], multiline: bool = False) -> str:
    """gufi_query command line for a structured aggregate payload (replay agent, primer examples, tests)."""
    parts = [("gufi_query -I", f"CREATE TABLE {PER_DIRECTORY_TABLE}({query['per_directory_shape']})")]
    for sql in query["per_directory_sql"]:
        parts.append((_flag_for(sql), f"INSERT INTO {PER_DIRECTORY_TABLE} {sql}"))
    parts += [("-K", f"CREATE TABLE {PARTIAL_TABLE}({query['partial_shape']})"),
              ("-J", f"INSERT INTO {PARTIAL_TABLE} {query['partial_sql']}"),
              ("-G", query["final_sql"])]
    pieces = [f"{flag} {_quote(value)}" for flag, value in parts] + [shlex.quote(query["index"])]
    return (" \\\n      " if multiline else " ").join(pieces)


_PATH_SQL = {
    "file": {"path": "rpath(sname, sroll, name)", "dirpath": "rpath(sname, sroll)"},
    "directory": {"path": "rpath(sname, sroll)"},
}


def to_rpath_query(query: dict[str, Any]) -> dict[str, Any]:
    """The same payload with server path columns replaced by GUFI rpath() calls (replay under path=rpath).

    Only per-directory statements can read paths, so only they change; the shapes keep their column names.
    """
    def rewrite(sql: str) -> str:
        try:
            tables = [t.name.lower() for t in sqlglot.parse_one(sql, read="sqlite").find_all(exp.Table)]
        except sqlglot.errors.ParseError:
            return sql
        kind = "file" if "vrpentries" in tables else "directory" if any(t.startswith("vrsummary") for t in tables) else None
        if kind is None:
            return sql
        for column, replacement in _PATH_SQL[kind].items():
            sql = re.sub(rf"(?<![\w.']){column}\b(?!\s*\()", replacement, sql)
        # rpath() returns absolute paths, so index-relative literals ('myindex/dir/%') match anywhere above them.
        return sql.replace(f"'{query['index']}/", f"'%/{query['index']}/")

    return {**query, "per_directory_sql": [rewrite(sql) for sql in query["per_directory_sql"]]}
