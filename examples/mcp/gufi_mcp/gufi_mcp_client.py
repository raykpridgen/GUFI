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

"""Demo client: start the GUFI MCP server twice over stdio, once running gufi_query on this
host and once on a remote host through gufi_vt + ssh, and use the same resources and
aggregate queries with each.

    uv run python3 gufi_mcp_client.py [--index vault] [--remote-args 127.0.0.1]

The remote run defaults to ssh 127.0.0.1, which needs key-based ssh to this host and
gufi_query on the PATH of a non-interactive login.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from mcp import Client
from mcp.client.stdio import StdioServerParameters

HERE = Path(__file__).resolve().parent
SERVER = HERE / "gufi_mcp_server.py"

load_dotenv(HERE / ".env")


def total_size_query(index: str) -> dict[str, Any]:
    """Number of files and their total size."""
    return {
        "index": index,
        "per_directory_shape": "size INT64",
        "per_directory_sql": ["SELECT size FROM vrpentries WHERE type = 'f'"],
        "partial_shape": "files INT64, bytes INT64",
        "partial_sql": "SELECT COUNT(*), SUM(size) FROM per_directory",
        "final_sql": "SELECT SUM(files) AS files, SUM(bytes) AS bytes FROM partial",
    }


def largest_files_query(index: str, limit: int = 5) -> dict[str, Any]:
    """Paths and sizes of the largest files."""
    return {
        "index": index,
        "per_directory_shape": "path TEXT, size INT64",
        "per_directory_sql": ["SELECT path, size FROM vrpentries WHERE type = 'f'"],
        "partial_shape": "path TEXT, size INT64",
        "partial_sql": "SELECT path, size FROM per_directory",
        "final_sql": f"SELECT path, size FROM partial ORDER BY size DESC, path LIMIT {limit}",
    }


def section(title: str) -> None:
    print(f"\n--- {title}")


async def read_json(client: Client, uri: str) -> Any:
    result = await client.read_resource(uri)
    return json.loads(result.contents[0].text)


async def call(client: Client, tool: str, arguments: dict[str, Any]) -> Any:
    result = await client.call_tool(tool, arguments)
    text = "\n".join(getattr(item, "text", "") or "" for item in result.content)
    if result.is_error:
        raise RuntimeError(f"{tool} failed: {text}")
    structured = result.structured_content
    if structured is None:
        return text
    return structured.get("result", structured) if set(structured) == {"result"} else structured


async def run(label: str, index: str, remote_cmd: Optional[str], remote_args: str, index_root: Optional[str]) -> list:
    env = {**os.environ, "MCPTRANSPORT": "stdio"}
    env.pop("GUFI_REMOTE_CMD", None)
    env.pop("GUFI_REMOTE_ARGS", None)
    if remote_cmd:
        env.update(GUFI_REMOTE_CMD=remote_cmd, GUFI_REMOTE_ARGS=remote_args)
    if index_root:
        env["GUFI_INDEX_ROOT"] = index_root
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=env, cwd=str(HERE))

    print(f"\n===== {label}: gufi_query runs "
          f"{f'through {remote_cmd} {remote_args}' if remote_cmd else 'on this host'}")
    async with Client(params, read_timeout_seconds=600) as client:
        section("resources")
        resources = (await client.list_resources()).resources
        templates = (await client.list_resource_templates()).resource_templates
        for uri in [str(r.uri) for r in resources] + [t.uri_template for t in templates]:
            print(uri)

        section("gufi_query")
        print(await call(client, "gufi_location", {}))
        print(await call(client, "gufi_version", {}))

        section("gufi://indexes")
        indexes = await read_json(client, "gufi://indexes")
        for entry in indexes:
            print(f"{entry['index']:20} treesummary={entry['treesummary']}")
        if index not in {entry["index"] for entry in indexes}:
            raise SystemExit(f"index {index!r} is not listed; pass --index with one of the names above")

        section("gufi://schemas/all")
        objects = await read_json(client, "gufi://schemas/all")
        print(", ".join(f"{name} ({kind})" for name, kind in objects.items()))

        section("gufi://schemas/vrpentries")
        columns = await read_json(client, "gufi://schemas/vrpentries")
        print(f"{len(columns)} columns: {', '.join(columns)}")
        for column in ("path", "size"):
            print(f"{column}: {columns[column]['type']}, {columns[column]['hint']}")

        section("gufi://functions")
        print(", ".join(await read_json(client, "gufi://functions")))

        results = []
        for title, query in (("total size of files", total_size_query(index)),
                             ("largest files", largest_files_query(index))):
            section(f"aggregate_sql_query: {title} in {index}")
            result = await call(client, "aggregate_sql_query", {"query": query})
            print(result["columns"])
            for row in result["rows"]:
                print(row)
            results.append(result["rows"])
        return results


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--index", default=os.getenv("DEFAULT_INDEX") or "vault", help="index to query")
    parser.add_argument("--remote-cmd", default="ssh", help="command that reaches the GUFI host (default: ssh)")
    parser.add_argument("--remote-args", default="127.0.0.1",
                        help="arguments for --remote-cmd, shell-quoted (default: 127.0.0.1)")
    parser.add_argument("--remote-index-root", help="GUFI_INDEX_ROOT on the GUFI host (default: GUFI_INDEX_ROOT)")
    parser.add_argument("--skip-remote", action="store_true", help="only run the local server")
    args = parser.parse_args()

    local = await run("local", args.index, None, "", None)
    if args.skip_remote:
        return
    remote = await run("remote", args.index, args.remote_cmd, args.remote_args, args.remote_index_root)
    print("\n===== local and remote aggregate results "
          f"{'match' if local == remote else 'DIFFER (expected only if the hosts hold different indexes)'}")


if __name__ == "__main__":
    asyncio.run(main())
