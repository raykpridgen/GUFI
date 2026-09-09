# Source this file to use GUFI client commands locally (via SSH to localhost).
# Usage: source /path/to/gufi/local/env-client.sh

_GUFI_LOCAL="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export PATH="${_GUFI_LOCAL}/client-bin:${PATH}"
export PYTHONPATH="${_GUFI_LOCAL}/client-bin:${PYTHONPATH:-}"
