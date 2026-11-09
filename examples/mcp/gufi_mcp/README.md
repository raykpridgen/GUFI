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
cp .env .env
```

Key variables:

| Variable | Purpose |
| -------- | ------- |
| `GUFI_INDEX_ROOT` | Parent directory containing GUFI indexes, on the host that runs `gufi_query`; must equal `IndexRoot` in GUFI's server config |
| `GUFIVTLIB` | Path of the `gufi_vt` SQLite extension on this host (no file extension) |
| `GUFI_REMOTE_CMD` | Optional command that runs `gufi_query` on another host, e.g. `ssh` (see [Remote GUFI host](#remote-gufi-host)) |
| `GUFI_REMOTE_ARGS` | Arguments for `GUFI_REMOTE_CMD`, shell-quoted, e.g. `-p 2222 user@gufi-host` |
| `GUFI_SOURCE_ROOT` | Optional parent of the indexed source trees; enables the `spath` column |
| `GUFI_MCP_THREADS` | `gufi_query` threads per aggregate (default: CPU count of this host) |
| `GUFI_MCP_CACHE_TTL` | Seconds to reuse index lists, treesummary checks and schema metadata (default 300; 0 disables) |
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

```bash
uv run python3 gufi_mcp_client.py [--index vault] [--remote-args 127.0.0.1] [--skip-remote]
```

The client starts the server itself over stdio twice: once with `gufi_query` on this
host, then with `GUFI_REMOTE_CMD=ssh` and `GUFI_REMOTE_ARGS` (default `127.0.0.1`).
Each run reads the resources, calls `gufi_location` and `gufi_version`, and runs two
aggregates (total file size, largest files); the client then reports whether the local
and remote aggregate results match. `--remote-index-root` sets `GUFI_INDEX_ROOT` for a
GUFI host that keeps its indexes elsewhere.

## MCP tools

| Tool | Description |
| ---- | ----------- |
| `aggregate_sql_query` | Run a per_directory / partial / final pipeline over an index through `gufi_vt` |
| `gufi_version` | `gufi_query --version`, on the GUFI host when queries run remotely |
| `gufi_location` | Path of that `gufi_query` |

### Example tool call

```python
await client.call_tool("aggregate_sql_query", {"query": {
    "index": "vault",
    "per_directory_shape": "size INT64",
    "per_directory_sql": ["SELECT size FROM vrpentries WHERE type = 'f'"],
    "partial_shape": "files INT64, bytes INT64",
    "partial_sql": "SELECT COUNT(*), SUM(size) FROM per_directory",
    "final_sql": "SELECT SUM(files) AS files, SUM(bytes) AS bytes FROM partial",
}})
```

## Paths

Every tool uses GUFI's command-line form: the index name, then the path inside the
index (`eval_master/groups/grp03/shared/archive.c`), i.e. relative to `IndexRoot`.
`gufi_find`, `gufi_ls -R` and `gufi_stats` print it; the server rewrites the absolute
paths that `gufi_stat` and `gufi_du --summarize` print into the same form.

In SQL, agents never see `sname`, `sroll` or `rpath()`. The rollup-aware views get
path columns instead:

| View | Column | Value |
| ---- | ------ | ----- |
| `vrpentries` | `path` | full path of the file or link |
| `vrpentries` | `dirpath` | full path of its directory |
| `vrsummary`, `vrsummarylong` | `path` | full path of the directory |
| all three | `spath` | path on the source file system (only with `GUFI_SOURCE_ROOT`) |

`aggregate_sql_query` replaces each index-table reference in per_directory_sql that uses path columns with a
derived table computed by GUFI itself, e.g.
`(SELECT 'idx' || substr(rpath(sname, sroll, name), N) AS path, * FROM vrpentries) AS vrpentries`,
and leaves the rest of the SQL unchanged; `spath` uses GUFI's `spath()` with
`p=<GUFI_SOURCE_ROOT>/<index>`. `sql_file_index` builds the same values from the
`rpath` column of `gufi_vt_*`. Index `<GUFI_INDEX_ROOT>/<name>` is assumed to have
been built from `<GUFI_SOURCE_ROOT>/<name>`, as `gufi_dir2index` names it.
`test_paths_live.py` checks the columns against `gufi_find` and a rolled-up copy of the index.

## Remote GUFI host

Every index read (index list, treesummary check, schema introspection,
`aggregate_sql_query`) goes through the `gufi_vt` SQLite extension, which runs
`gufi_query`. Without `GUFI_REMOTE_CMD`, `gufi_query` runs on this host. With it,
`gufi_vt` runs `GUFI_REMOTE_CMD GUFI_REMOTE_ARGS 'gufi_query ...'` and reads the
results back, so the GUFI host needs only GUFI installed: no Python and no MCP code.

```bash
GUFI_REMOTE_CMD='ssh'
GUFI_REMOTE_ARGS='-p 2222 user@gufi-host'
GUFI_INDEX_ROOT='/path/on/gufi-host/indexes'
```

- This host still needs `GUFIVTLIB`, the extension that sends the command.
- The GUFI host needs `gufi_query` on the PATH of a non-interactive login
  (`ssh user@gufi-host 'command -v gufi_query'` must print it). `gufi_version`
  and `gufi_location` report that binary.
- With `ssh`, the server adds `-n` (gufi_vt does not redirect the child's stdin, so
  ssh would read the MCP stdio stream) and `-o BatchMode=yes` (no password prompts)
  unless you pass them. Use key-based authentication.
- `gufi_vt` sends the query to the remote shell as one string, so index paths may
  use only letters, digits and `. _ + - /`, and aggregate SQL sent remotely may not
  contain `$`, `` ` `` or `\`.
- A failed connection reaches the server only as a generic SQLite read error; the
  server adds the remote command to the message. `gufi_version` shows ssh's own error.
- On timeout the server kills its `ssh` child; a `gufi_query` already running on the
  GUFI host may run until it notices the closed connection.

`test_remote.py` compares every resource and an aggregate run locally and through
`ssh 127.0.0.1` (set `GUFI_TEST_SSH_HOST` for another host).

## MCP resources

| URI | Description |
| --- | ----------- |
| `gufi://indexes` | Indexes under `GUFI_INDEX_ROOT` and whether each has a treesummary table |
| `gufi://schemas/{schema}` | `all` for table and view names, or one table's columns, types and hints |
| `gufi://functions`, `gufi://functions/{name}` | GUFI SQL functions and their details |

## MCP prompts

| Prompt | Description |
| ------ | ----------- |
| `find_biggest_files` | Ask an agent to locate large files in an index |

## Cursor integration

**stdio (recommended):** enable MCP server `gufi_mcp` in Cursor Settings. Config lives at
the repo root (`.cursor/mcp.json`) or in this package (`.cursor/mcp.json`); Cursor spawns
the server via `mcp run` — no separate terminal needed.

**HTTP:** run `uv run python3 gufi_mcp_server.py` first, then point Cursor at
`http://127.0.0.1:8000/mcp`. See [checkpoint.md](../../../notes/development/checkpoint.md)
for the full agent setup guide.
