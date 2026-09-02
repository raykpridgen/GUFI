# Source this file to use GUFI client commands locally.
# Usage: source /home/raykp/research/mcp/gufi/local/env-client.sh

_GUFI_LOCAL="/home/raykp/research/mcp/gufi/local"
export PATH="${_GUFI_LOCAL}/client-bin:${PATH}"
export PYTHONPATH="${_GUFI_LOCAL}/client-bin:${PYTHONPATH:-}"
