#!/usr/bin/env python3
"""GUFI MCP server with a tool allowlist, for agent evaluation (eval_design.md Part 2).

GUFI_EVAL_TOOLS is a comma-separated list of tool names to keep, or "all".
Every other tool is removed before serving, so it is absent from tools/list
and cannot be called. Resources are always kept. Serves over stdio.
"""

import asyncio
import logging
import os
import sys

from gufi_mcp_server import mcp


def apply_allowlist(spec: str) -> list[str]:
    """Remove tools not named in spec; return the tool names that remain."""
    names = [tool.name for tool in asyncio.run(mcp.list_tools())]
    if spec.strip() == "all":
        return names
    allowed = {name.strip() for name in spec.split(",") if name.strip()}
    unknown = allowed - set(names)
    if unknown:
        raise SystemExit(f"GUFI_EVAL_TOOLS names unknown tools: {sorted(unknown)}; known: {names}")
    if not allowed:
        raise SystemExit("GUFI_EVAL_TOOLS is empty; use 'all' or a comma-separated list of tool names")
    for name in names:
        if name not in allowed:
            mcp.remove_tool(name)
    return [name for name in names if name in allowed]


if __name__ == "__main__":
    level = os.getenv("GUFI_EVAL_LOG_LEVEL", "WARNING").upper()
    for name in ("", "mcp", "gufi_mcp_util"):
        logging.getLogger(name).setLevel(level)
    kept = apply_allowlist(os.getenv("GUFI_EVAL_TOOLS", "all"))
    print(f"gufi_eval_server: serving tools {kept}", file=sys.stderr, flush=True)
    mcp.run(transport="stdio")
