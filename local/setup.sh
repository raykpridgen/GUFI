#!/usr/bin/env bash
# Local GUFI server/client/MCP setup for development on a single machine.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUFI_SRC="$(cd "${ROOT}/.." && pwd)"
BUILD="${GUFI_SRC}/build"
PREFIX="${ROOT}"
SEARCH="${PREFIX}/search"
ETC="${PREFIX}/etc/GUFI"
CLIENT_BIN="${PREFIX}/client-bin"
MCP_DIR="${GUFI_SRC}/examples/mcp/gufi_mcp"
SSH_KEY="${HOME}/.ssh/gufi_local"
INDEX_SRC="${GUFI_SRC}/../notes"
INDEX_NAME="notes"

echo "==> GUFI local prefix: ${PREFIX}"
echo "==> Index source:      ${INDEX_SRC}"
echo "==> Index destination: ${SEARCH}/${INDEX_NAME}"

if [[ ! -d "${INDEX_SRC}" ]]; then
  echo "ERROR: Index source directory not found: ${INDEX_SRC}" >&2
  exit 1
fi

echo "==> Configuring build (CLIENT=On, local prefix)"
cmake -S "${GUFI_SRC}" -B "${BUILD}" \
  -DCLIENT=On \
  -DCMAKE_INSTALL_PREFIX="${PREFIX}" \
  -DSERVER_CONFIG="${ETC}/server.config" \
  -DCLIENT_CONFIG="${ETC}/client.config"

echo "==> Building and installing into ${PREFIX}"
cmake --build "${BUILD}" -j"$(nproc)"
cmake --install "${BUILD}" || true  # bash_completion to /etc may fail without sudo

echo "==> Writing server config"
mkdir -p "${ETC}" "${SEARCH}"
cat > "${ETC}/server.config" <<EOF
# GUFI server configuration (local dev)
Threads=4
Query=${PREFIX}/bin/gufi_query
Sqlite3=${PREFIX}/bin/gufi_sqlite3
Stat=${PREFIX}/bin/gufi_stat_bin
IndexRoot=${SEARCH}
OutputBuffer=4096
EOF

echo "==> Writing client config"
cat > "${ETC}/client.config" <<EOF
# GUFI client configuration (local dev)
Server=127.0.0.1
Port=22
EOF

if [[ ! -f "${SSH_KEY}" ]]; then
  echo "==> Creating passphrase-free SSH key for local client (${SSH_KEY})"
  ssh-keygen -t ed25519 -f "${SSH_KEY}" -N "" -C "gufi-local-dev"
  mkdir -p "${HOME}/.ssh"
  chmod 700 "${HOME}/.ssh"
  touch "${HOME}/.ssh/authorized_keys"
  chmod 600 "${HOME}/.ssh/authorized_keys"
  if ! grep -qF "$(cat "${SSH_KEY}.pub")" "${HOME}/.ssh/authorized_keys" 2>/dev/null; then
    cat "${SSH_KEY}.pub" >> "${HOME}/.ssh/authorized_keys"
  fi
fi

ssh-keyscan -H 127.0.0.1 >> "${HOME}/.ssh/known_hosts" 2>/dev/null || true

if [[ "${INSTALL_SYSTEM_CONFIG:-0}" == "1" ]]; then
  echo "==> Installing server config for SSH remote commands (/etc/GUFI/config)"
  sudo mkdir -p /etc/GUFI
  sudo cp "${ETC}/server.config" /etc/GUFI/config
  sudo chmod 664 /etc/GUFI/config
else
  echo "==> Skipping /etc/GUFI install (set INSTALL_SYSTEM_CONFIG=1 to enable)"
fi

echo "==> Indexing ${INDEX_SRC} -> ${SEARCH}/${INDEX_NAME}"
rm -rf "${SEARCH}/${INDEX_NAME}"
"${PREFIX}/bin/gufi_dir2index" --threads 4 "${INDEX_SRC}" "${SEARCH}"
"${PREFIX}/bin/gufi_treesummary" "${SEARCH}/${INDEX_NAME}"

echo "==> Generating client wrapper scripts in ${CLIENT_BIN}"
mkdir -p "${CLIENT_BIN}"
for tool in du find getfattr ls stat stats; do
  cat > "${CLIENT_BIN}/gufi_${tool}" <<EOF
#!/usr/bin/env python3
import subprocess, sys
from shlex import quote as sanitize
import gufi_config

PREFIX = "${PREFIX}"
SSH_KEY = "${SSH_KEY}"

def run(args):
    config = gufi_config.Client(gufi_config.PATH)
    remote = "PYTHONPATH={}/lib {}/bin/gufi_{} {}".format(
        PREFIX, PREFIX, "${tool}",
        " ".join(sanitize(a) for a in args),
    )
    cmd = [
        "ssh", "-i", SSH_KEY, "-o", "IdentitiesOnly=yes",
        config.server, "-p", str(config.port), "--", remote,
    ]
    proc = subprocess.Popen(cmd)
    proc.communicate()
    return proc.returncode

if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
EOF
  chmod +x "${CLIENT_BIN}/gufi_${tool}"
done

cp "${BUILD}/scripts/client_gufi_config.py" "${CLIENT_BIN}/gufi_config.py"
cp "${BUILD}/scripts/gufi_common.py" "${CLIENT_BIN}/gufi_common.py"

# Make client config path portable regardless of install prefix.
python3 - <<PY
from pathlib import Path
import re
path = Path("${CLIENT_BIN}") / "gufi_config.py"
text = path.read_text()
text = re.sub(
    r"PATH = .*",
    "PATH = __import__('os').path.join(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__))), 'etc', 'GUFI', 'client.config')",
    text,
    count=1,
)
path.write_text(text)
PY

python3 "${PREFIX}/lib/gufi_config.py" server "${ETC}/server.config" || \
  echo "WARN: server config validation skipped (python grp module unavailable?)"
PYTHONPATH="${CLIENT_BIN}" python3 "${CLIENT_BIN}/gufi_config.py" client "${ETC}/client.config" || \
  echo "WARN: client config validation skipped (python grp module unavailable?)"

echo "==> Writing MCP environment (${MCP_DIR}/.env)"
mkdir -p "${MCP_DIR}/plots"
cat > "${MCP_DIR}/.env" <<EOF
GUFI_INDEXES_ROOT=${SEARCH}/
GUFI_EXECUTABLE=${PREFIX}/bin/gufi_query
GUFI_SERVER_PREFIX=${PREFIX}
GUFI_CLIENT_BIN=${CLIENT_BIN}
GUFI_CLIENT_CONFIG=${ETC}/client.config
GUFI_SSH_IDENTITY=${SSH_KEY}
DEFAULT_INDEX=${INDEX_NAME}
SCHEMAFILE=./schemas.json
REMOTEHOST=127.0.0.1
MCPTRANSPORT=streamable-http
MCPSRVHOST=127.0.0.1
MCPSRVPORT=8000
MCP_SERVER_URL=http://127.0.0.1:8000/mcp
GUFI_PLOT_DIR=${MCP_DIR}/plots
EOF

echo
echo "Setup complete."
echo
echo "Server commands (run directly on this machine):"
echo "  source ${PREFIX}/env-server.sh"
echo "  gufi_ls ${INDEX_NAME}"
echo "  gufi_du ${INDEX_NAME}"
echo "  gufi_find ${INDEX_NAME} -type f"
echo
echo "Client commands (SSH to localhost, same as production client):"
echo "  source ${PREFIX}/env-client.sh"
echo "  gufi_ls ${INDEX_NAME}"
echo
echo "MCP server:"
echo "  source ${PREFIX}/env-mcp.sh"
echo "  cd ${MCP_DIR} && uv sync && uv run python3 gufi_mcp_server.py"
echo
echo "Quick test:"
source "${PREFIX}/env-server.sh"
echo "  [server] $(gufi_ls "${INDEX_NAME}" | tr '\n' ' ')"
source "${PREFIX}/env-client.sh"
echo "  [client] $(gufi_ls "${INDEX_NAME}" | tr '\n' ' ')"
