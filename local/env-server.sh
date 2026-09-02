# Source this file to use GUFI server commands locally (no SSH).
# Usage: source /home/raykp/research/mcp/gufi/local/env-server.sh

_GUFI_LOCAL="/home/raykp/research/mcp/gufi/local"
export PATH="${_GUFI_LOCAL}/bin:${PATH}"
export PYTHONPATH="${_GUFI_LOCAL}/lib:${PYTHONPATH:-}"
