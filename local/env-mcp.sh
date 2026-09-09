# Source this file before running the GUFI MCP server or demo client.
# Usage: source /path/to/gufi/local/env-mcp.sh

_GUFI_LOCAL="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
_GUFI_SRC="$(cd "${_GUFI_LOCAL}/.." && pwd)"
_MCP_DIR="${_GUFI_SRC}/examples/mcp/gufi_mcp"

export GUFI_INDEXES_ROOT="${_GUFI_LOCAL}/search/"
export GUFI_EXECUTABLE="${_GUFI_LOCAL}/bin/gufi_query"
export GUFI_SERVER_PREFIX="${_GUFI_LOCAL}"
export GUFI_CLIENT_BIN="${_GUFI_LOCAL}/client-bin"
export GUFI_CLIENT_CONFIG="${_GUFI_LOCAL}/etc/GUFI/client.config"
export GUFI_SSH_IDENTITY="${HOME}/.ssh/gufi_local"
export DEFAULT_INDEX="notes"
export SCHEMAFILE="${_MCP_DIR}/schemas.json"
export REMOTEHOST="127.0.0.1"
export MCPTRANSPORT="streamable-http"
export MCPSRVHOST="127.0.0.1"
export MCPSRVPORT="8000"
export MCP_SERVER_URL="http://127.0.0.1:8000/mcp"
export GUFI_PLOT_DIR="${_MCP_DIR}/plots"
export GUFIVTLIB="${_GUFI_LOCAL}/lib/gufi_vt.so"
export GUFI_MCP_EVENT_LOG="${_MCP_DIR}/logs/gufi_mcp_events.jsonl"
