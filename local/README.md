# Local GUFI development environment

Single-machine setup for GUFI server tools, SSH client wrappers, and the MCP server.

## First-time setup

From the GUFI repo root:

```bash
./local/setup.sh
```

This will:

- Build and install GUFI into `local/bin/`
- Write `local/etc/GUFI/{server,client}.config`
- Index `../notes/` into `local/search/notes/`
- Regenerate SSH client wrappers in `local/client-bin/`
- Write `examples/mcp/gufi_mcp/.env` for the MCP server

Optional: set `INSTALL_SYSTEM_CONFIG=1` before running setup to copy server config to `/etc/GUFI/config` (requires sudo).

## Daily use

**Server commands** (direct, no SSH):

```bash
source local/env-server.sh
gufi_ls notes
gufi_du notes
```

**Client commands** (SSH to localhost, production-like):

```bash
source local/env-client.sh
gufi_ls notes
```

**MCP server**:

```bash
./local/run-mcp.sh
# or:
source local/env-mcp.sh
cd examples/mcp/gufi_mcp && uv run python3 gufi_mcp_server.py
```

Server URL: `http://127.0.0.1:8000/mcp`

## Layout

| Path | Purpose |
| ---- | ------- |
| `local/bin/` | GUFI executables and server-side Python tools |
| `local/client-bin/` | SSH client wrapper scripts |
| `local/lib/` | Python modules for server tools |
| `local/etc/GUFI/` | Server and client configuration |
| `local/search/` | GUFI index root (`notes` index by default) |
| `local/env-*.sh` | Environment helpers |

## Index source

By default, `setup.sh` indexes `../notes/` (sibling of the `gufi/` repo) into `local/search/notes/`. Edit `INDEX_SRC` in `setup.sh` to index a different tree.
