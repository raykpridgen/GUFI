# GUFI MCP Server

An MCP server built on the official Python SDK `MCPServer` (mcp 2.x). It exposes
GUFI indexing and client-wrapper commands to MCP hosts such as Cursor.

## Prerequisites

- GUFI built with client support and a local index (see `gufi/local/setup.sh`)
- Python 3.14+ with [uv](https://docs.astral.sh/uv/)
- SSH access from this machine to the GUFI server host configured in
  `GUFI_CLIENT_CONFIG` (for the `gufi_client_*` tools)

## Configure

Copy the example environment file and edit paths for your machine:

```bash
cp .env.example .env
```

Key variables:

| Variable | Purpose |
| -------- | ------- |
| `GUFI_INDEXES_ROOT` | Parent directory containing GUFI indexes |
| `GUFI_EXECUTABLE` | Path to `gufi_query` |
| `GUFI_CLIENT_BIN` | Directory with client wrapper scripts |
| `GUFI_CLIENT_CONFIG` | Client SSH config (`Server`, `Port`) |
| `GUFI_SSH_IDENTITY` | SSH private key for non-interactive client access |
| `DEFAULT_INDEX` | Default index name for demos (e.g. `notes`) |
| `MCP_SERVER_URL` | URL used by the demo client |
| `MCPSRVHOST` / `MCPSRVPORT` | Bind address for the MCP server |

All settings are loaded from `.env` via `gufi_util.get_settings()`.

## Install

```bash
uv sync
```

## Run the server

```bash
uv run python3 gufi_mcp_server.py
```

The server starts with Streamable HTTP transport at `http://127.0.0.1:8000/mcp`.

## Run the demo client

In another terminal, with the server running:

```bash
uv run python3 gufi_mcp_client.py
```

Output is also written to `mcp.out`.

## MCP tools

| Tool | Description |
| ---- | ----------- |
| `gufi_version` | Return `gufi_query --version` |
| `gufi_location` | Return path to configured `gufi_query` |
| `gufi_query_local_index` | Run SQL against a local index via `gufi_query -E` |
| `gufi_client_ls` | Remote `gufi_ls` via SSH client wrapper |
| `gufi_client_du` | Remote `gufi_du` via SSH client wrapper |
| `gufi_client_find` | Remote `gufi_find` via SSH client wrapper |
| `gufi_client_stat` | Remote `gufi_stat` via SSH client wrapper |
| `gufi_client_stats` | Remote `gufi_stats` via SSH client wrapper |
| `gufi_client_getfattr` | Remote `gufi_getfattr` via SSH client wrapper |
| `gufi_client_query` | Remote `gufi_query` via SSH |

### Example tool calls

```python
# Local SQL query
await client.call_tool("gufi_query_local_index", {
    "index": "notes",
    "sql_query": "SELECT name, size FROM vrpentries ORDER BY size DESC LIMIT 5",
})

# Remote ls (uses GUFI client SSH wrappers)
await client.call_tool("gufi_client_ls", {"index": "notes"})

# Remote find with extra flags
await client.call_tool("gufi_client_find", {
    "index": "notes",
    "arguments": "-type f",
})
```

## MCP resources

| URI | Description |
| --- | ----------- |
| `gufi://indexes` | List indexes under `GUFI_INDEXES_ROOT` |
| `gufi://schemas/{schema}` | Return schema metadata from `schemas.json` |

## MCP prompts

| Prompt | Description |
| ------ | ----------- |
| `find_biggest_files` | Ask an agent to locate large files in an index |

## Cursor integration

`.cursor/mcp.json` can point Cursor at this server once it is running locally.
