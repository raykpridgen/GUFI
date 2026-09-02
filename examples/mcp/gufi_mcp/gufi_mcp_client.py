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


import asyncio
from pathlib import Path

from mcp import Client

from gufi_util import format_tool_result_text, get_settings


async def call_and_print(client: Client, tool: str, arguments: dict | None = None) -> None:
    print(f"\n==> {tool}")
    if arguments:
        print(f"    args: {arguments}")
    result = await client.call_tool(tool, arguments or {})
    print(format_tool_result_text(result))


async def main() -> None:
    settings = get_settings()
    index = settings.default_index
    output_path = Path(__file__).resolve().parent / "mcp.out"

    print(f"Connecting to MCP server at {settings.mcp_server_url}")

    sections: list[str] = []

    async with Client(settings.mcp_server_url) as client:
        tools = await client.list_tools()
        tool_names = sorted(tool.name for tool in tools.tools)
        header = "Available tools:\n  " + "\n  ".join(tool_names)
        print(header)
        sections.append(header)

        await call_and_print(client, "gufi_location")
        sections.append("gufi_location")

        await call_and_print(client, "gufi_version")
        sections.append("gufi_version")

        await call_and_print(
            client,
            "gufi_query_local_index",
            {
                "index": index,
                "sql_query": "SELECT name, size FROM vrpentries ORDER BY size DESC LIMIT 5",
                "return_limit": 10,
            },
        )
        sections.append("gufi_query_local_index")

        await call_and_print(client, "gufi_client_ls", {"index": index})
        sections.append("gufi_client_ls")

        await call_and_print(client, "gufi_client_du", {"index": index})
        sections.append("gufi_client_du")

        await call_and_print(
            client,
            "gufi_client_find",
            {"index": index, "arguments": "-type f"},
        )
        sections.append("gufi_client_find")

        await call_and_print(
            client,
            "gufi_client_stat",
            {"index": f"{index}/doc_min.txt"},
        )
        sections.append("gufi_client_stat")

        await call_and_print(
            client,
            "gufi_client_stats",
            {"index": index, "arguments": "-c total-filecount"},
        )
        sections.append("gufi_client_stats")

        await call_and_print(
            client,
            "gufi_client_query",
            {
                "index": index,
                "arguments": (
                    '-E "SELECT name, size FROM vrpentries '
                    "WHERE type = 'f' ORDER BY size DESC LIMIT 3;\""
                ),
            },
        )
        sections.append("gufi_client_query")

    note = (
        "\nDemo complete. Tools exercised: "
        + ", ".join(sections[1:])
    )
    print(note)
    output_path.write_text(note + "\n", encoding="utf-8")
    print(f"\nWrote summary to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
