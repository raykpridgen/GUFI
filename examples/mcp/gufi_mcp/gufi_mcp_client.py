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
from mcp import Client
from mcp.server import MCPServer
import sys
import os
from dotenv import load_dotenv
import json

load_dotenv()

MCPSRVHOST = os.getenv('MCPSRVHOST')
MCPSRVPORT = os.getenv('MCPSRVPORT')
MCP_SERVER=f'http://{MCPSRVHOST}:{MCPSRVPORT}/mcp'
LOCALSELECT='select path,name,size from gufi_vt_pentries'
LOCALWHERE='where name like \'%\' limit 50'
LOCALSEARCHPATH='documents'
REMOTESELECT='select path,name,size from gufi_vt_pentries'
REMOTEWHERE='where name like \'%\' order by size desc limit 50'
REMOTESEARCHPATH='/home/raykprid/search/documents/'

async def main():
    # Connect to FastMCP server
    client = Client(MCP_SERVER)

    async with client:

        print(f"Connected to Server: {client.server_info.name}")
        print(f"Connected to Server")
        print("-" * 20)

        ''' List available resources '''

        avail_resources = await client.list_resources()
        print("Resources Available")
        for resource in avail_resources.resources:
            print("-" * 20)
            print(f"Resource Name: {resource.name}")
            print(f"Description: {resource.description}")

        # get available indexes
        print("read gufi_indexes")
        result = await client.read_resource("gufi://indexes")
        print(f"Result: {result.contents[0].text}")
        print("-" * 20)

        # get schema
        print("read gufi_schemas")
        result = await client.read_resource("gufi://schemas/{balls}")
        deliminate = result.contents[0].text.replace("],[", "|")[2:-2]
        outlines = deliminate.split("|")
        outlen = len(outlines)
        for outi in range(outlen):
            print(outlines[outi])

        # gufi local query
        print(f"query local gufi  index")
        result_stream= await client.call_tool("sql_local_file_index", {"sqlin": 'select name, type, sql from sqlite_master', "wherein": 'where sql is not null order by name, type',"index": LOCALSEARCHPATH})
        deliminate=result_stream.content[0].text.replace("],[","|")[2:-2]
        outlines=deliminate.split("|")
        outlen=len(outlines)
        for outi in range(outlen):
            print (outlines[outi])


        ''' List available tools '''

        avail_tools = await client.list_tools()
        print("Tools Available")
        for tool in avail_tools.tools:
            print("-" * 20)
            print(f"Tool Name: {tool.name}")
            print(f"Description: {tool.description}")

        # locate gufi_query
        print("run locate gufi_query")
        result = await client.call_tool("gufi_location", {"a": "gufi_query location please"})
        print(f"Result: {result.content[0].text}")
        print("-" * 20)

        # locate gufi_query
        print("run gufi_query version")
        result = await client.call_tool("gufi_version", {"a": "gufi_query version please"})
        print(f"Result: {result.content[0].text}")
        print("-" * 20)

        # list vt_pentries schema
        print("run vt_pentries list schema")
        result = await client.call_tool("local_file_index_schema", {"schema": "gufi_query schema please"})
        print(f"Result: {result.content[0].text}")
        print("-" * 20)

        # gufi local query
        print(f"query local gufi  index")
        result_stream= await client.call_tool("sql_local_file_index", {"sqlin": LOCALSELECT, "wherein": LOCALWHERE,"index": LOCALSEARCHPATH})
        res = json.loads(result_stream.content[0].text)
        for row in range(res["row_count"]):
            print (res["rows"][row])

        # gufi remote query
        print(f"query remote gufi  index")
        result_stream= await client.call_tool("sql_remote_file_index", {"sqlin": REMOTESELECT, "wherein": REMOTEWHERE,"searchpath": REMOTESEARCHPATH})
        deliminate=result_stream.content[0].text.replace("],[","|")[2:-2]
        outlines=deliminate.split("|")
        outlen=len(outlines)
        for outi in range(outlen):
            print (outlines[outi])



if __name__ == "__main__":
    asyncio.run(main())
