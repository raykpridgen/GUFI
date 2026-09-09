# Source this file to use GUFI server commands locally (no SSH).
# Usage: source /path/to/gufi/local/env-server.sh

_GUFI_LOCAL="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export PATH="${_GUFI_LOCAL}/bin:${PATH}"
export PYTHONPATH="${_GUFI_LOCAL}/lib:${PYTHONPATH:-}"
