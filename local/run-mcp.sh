#!/usr/bin/env bash
# Start the GUFI MCP server using the local dev environment.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_DIR="$(cd "${ROOT}/../examples/mcp/gufi_mcp" && pwd)"

# shellcheck source=/dev/null
source "${ROOT}/env-mcp.sh"

cd "${MCP_DIR}"
uv sync
exec uv run python3 gufi_mcp_server.py
