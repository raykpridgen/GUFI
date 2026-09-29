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

"""Smoke-test client for the GUFI MCP server.

Run the MCP server first (same .env as this script), then:

    uv run python gufi_mcp_client.py
    uv run python gufi_mcp_client.py --index work --full

SQL tools (sql_file_index, aggregate_sql_query) need GUFIVTLIB and a working
gufi_vt install. If you see ``libsqlite3-pcre.so: cannot open shared object
file``, install GUFI deps (sqlite3-pcre) and start the server with
``LD_LIBRARY_PATH`` including that library directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from dotenv import load_dotenv
from mcp import Client

load_dotenv()

MCPSRVHOST = os.getenv("MCPSRVHOST", "127.0.0.1")
MCPSRVPORT = os.getenv("MCPSRVPORT", "8000")
MCP_SERVER = f"http://{MCPSRVHOST}:{MCPSRVPORT}/mcp"


def parse_tool_payload(result: Any) -> dict[str, Any]:
    """Normalize a call_tool result into a dict."""
    if getattr(result, "structured_content", None):
        data = result.structured_content
        if isinstance(data, dict):
            return data
    if getattr(result, "content", None):
        for block in result.content:
            text = getattr(block, "text", None)
            if not text:
                continue
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return {"raw_text": text}
    return {}


def report_step(name: str, payload: dict[str, Any], *, expect_rows: bool = False) -> bool:
    """Print one step; return True if the step looks successful."""
    err = payload.get("error")
    if err:
        print(f"  FAIL {name}: {err}")
        if payload.get("sql"):
            print(f"       sql: {str(payload['sql'])[:200]}...")
        return False

    row_count = payload.get("row_count")
    rows = payload.get("rows")
    if expect_rows and not row_count and not rows:
        print(f"  WARN {name}: ok but no rows (row_count={row_count})")
        return True

    preview = ""
    if rows:
        preview = f" first_row={rows[0]!r}"
    elif payload.get("raw_text"):
        preview = f" text={payload['raw_text'][:80]!r}"
    print(f"  OK   {name}: row_count={row_count}{preview}")
    return True


async def run_smoke(index: str, full: bool) -> int:
    failures = 0
    client = Client(MCP_SERVER)

    async with client:
        print(f"Connected: {client.server_info.name} @ {MCP_SERVER}")
        print("-" * 60)

        # --- core ---
        print("[gufi_version]")
        version_result = await client.call_tool("gufi_version", {})
        r = parse_tool_payload(version_result)
        if r.get("raw_text"):
            print(f"  OK   version: {r['raw_text'].strip()[:80]}")
        elif r:
            print(f"  OK   version: {r}")
        elif version_result.content:
            print(f"  OK   version: {version_result.content[0].text.strip()[:80]}")
        else:
            print("  FAIL gufi_version: empty response")
            failures += 1

        print("[gufi://indexes]")
        res = await client.read_resource("gufi://indexes")
        print(f"  OK   {res.contents[0].text[:120]}...")

        # --- user tools (flat top-level arguments) ---
        print(f"[gufi_find top-N largest on index={index!r}]")
        r = parse_tool_payload(
            await client.call_tool(
                "gufi_find",
                {
                    "index": index,
                    "type": ["f"],
                    "largest": True,
                    "ls": True,
                    "num_results": 5,
                },
            )
        )
        if not report_step("gufi_find largest+ls", r, expect_rows=True):
            failures += 1

        print("[gufi_find ls+print coercion: print should be dropped server-side]")
        r = parse_tool_payload(
            await client.call_tool(
                "gufi_find",
                {
                    "index": index,
                    "type": ["f"],
                    "largest": True,
                    "ls": True,
                    "print": True,
                    "num_results": 3,
                },
            )
        )
        if not report_step("gufi_find ls+print", r, expect_rows=True):
            failures += 1

        print(f"[gufi_stats total-filecount index={index!r}]")
        r = parse_tool_payload(
            await client.call_tool(
                "gufi_stats",
                {"index": index, "stat": "total-filecount"},
            )
        )
        if not report_step("gufi_stats total-filecount", r, expect_rows=True):
            failures += 1

        print(f"[gufi_ls index={index!r}]")
        r = parse_tool_payload(
            await client.call_tool(
                "gufi_ls",
                {"index": index, "long_listing": True, "human_readable": True},
            )
        )
        if not report_step("gufi_ls", r, expect_rows=True):
            failures += 1

        # --- SQL / aggregate (same gufi_vt path as the server) ---
        print(f"[aggregate_sql_query file count index={index!r}]")
        aggregate_count = {
            "query": {
                "index": index,
                "config": ["threads=4"],
                "sql_options": [
                    {"option": "-I", "sql": "CREATE TABLE intermediate(n INT64)"},
                    {
                        "option": "-E",
                        "sql": "INSERT INTO intermediate SELECT 1 FROM vrpentries WHERE type = 'f'",
                    },
                    {"option": "-K", "sql": "CREATE TABLE aggregate(n INT64)"},
                    {"option": "-J", "sql": "INSERT INTO aggregate SELECT COUNT(*) FROM intermediate"},
                    {"option": "-G", "sql": "SELECT SUM(n) AS file_count FROM aggregate"},
                ],
            }
        }
        r = parse_tool_payload(await client.call_tool("aggregate_sql_query", aggregate_count))
        if not report_step("aggregate_sql_query count", r, expect_rows=True):
            failures += 1
            if r.get("error") and "libsqlite3-pcre" in str(r["error"]):
                print(
                    "       hint: build/install sqlite3-pcre (GUFI deps) and restart the "
                    "MCP server with LD_LIBRARY_PATH set to its lib directory."
                )
            if r.get("error") and "gufi_vt.so.so" in str(r["error"]):
                print("       hint: restart MCP server after GUFIVTLIB / load_extension fix.")

        print(f"[sql_file_index LIMIT 3 index={index!r}]")
        r = parse_tool_payload(
            await client.call_tool(
                "sql_file_index",
                {
                    "index": index,
                    "sqlin": "SELECT name, size FROM vrpentries",
                    "wherein": "WHERE type = 'f' LIMIT 3",
                },
            )
        )
        if not report_step("sql_file_index", r, expect_rows=True):
            failures += 1

        if full:
            print(f"[gufi_du index={index!r}]")
            r = parse_tool_payload(
                await client.call_tool("gufi_du", {"index": index, "human_readable": True})
            )
            if not report_step("gufi_du", r, expect_rows=True):
                failures += 1

            print(f"[gufi_stats extensions cumulative index={index!r}]")
            r = parse_tool_payload(
                await client.call_tool(
                    "gufi_stats",
                    {
                        "index": index,
                        "stat": "extensions",
                        "cumulative": True,
                        "order": "DESC",
                        "num_results": 5,
                    },
                )
            )
            if not report_step("gufi_stats extensions", r, expect_rows=True):
                failures += 1

        print("-" * 60)
        if failures:
            print(f"Finished with {failures} failed step(s).")
        else:
            print("All smoke steps passed.")
        return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the GUFI MCP server.")
    parser.add_argument(
        "--index",
        default=os.getenv("GUFI_MCP_TEST_INDEX", "personal_data"),
        help="GUFI index name from gufi://indexes (default: personal_data)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Also run gufi_du and gufi_stats extensions",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run_smoke(args.index, args.full)))


if __name__ == "__main__":
    main()
