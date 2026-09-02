#!/usr/bin/env bash
# Local GUFI server/client setup for development on a single machine.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUFI_SRC="$(cd "${ROOT}/.." && pwd)"
BUILD="${GUFI_SRC}/build"
PREFIX="${ROOT}"
SEARCH="${PREFIX}/search"
ETC="${PREFIX}/etc/GUFI"
CLIENT_BIN="${PREFIX}/client-bin"
SSH_KEY="${HOME}/.ssh/gufi_local"
INDEX_SRC="${GUFI_SRC}/../notes"

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
  cat "${SSH_KEY}.pub" >> "${HOME}/.ssh/authorized_keys"
  chmod 600 "${HOME}/.ssh/authorized_keys"
fi

ssh-keyscan -H 127.0.0.1 >> "${HOME}/.ssh/known_hosts" 2>/dev/null || true

echo "==> Installing server config for SSH remote commands"
sudo cp "${ETC}/server.config" /etc/GUFI/config
sudo chmod 664 /etc/GUFI/config

echo "==> Indexing ${INDEX_SRC} -> ${SEARCH}/notes"
rm -rf "${SEARCH}/notes"
"${PREFIX}/bin/gufi_dir2index" --threads 4 "${INDEX_SRC}" "${SEARCH}"
"${PREFIX}/bin/gufi_treesummary" "${SEARCH}/notes"

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

python3 "${PREFIX}/lib/gufi_config.py" server "${ETC}/server.config"
PYTHONPATH="${CLIENT_BIN}" python3 "${CLIENT_BIN}/gufi_config.py" client "${ETC}/client.config"

echo
echo "Setup complete."
echo
echo "Server commands (run directly on this machine):"
echo "  export PATH=\"${PREFIX}/bin:\$PATH\""
echo "  export PYTHONPATH=\"${PREFIX}/lib:\$PYTHONPATH\""
echo "  gufi_ls notes"
echo "  gufi_du notes"
echo "  gufi_find notes -type f"
echo
echo "Client commands (SSH to localhost, same as production client):"
echo "  export PATH=\"${CLIENT_BIN}:\$PATH\""
echo "  export PYTHONPATH=\"${CLIENT_BIN}:\$PYTHONPATH\""
echo "  gufi_ls notes"
echo "  gufi_du notes"
echo
echo "Environment helpers:"
echo "  source ${PREFIX}/env-server.sh   # server-side commands"
echo "  source ${PREFIX}/env-client.sh   # client-side commands (via SSH)"
echo
echo "Quick test:"
export PATH="${PREFIX}/bin:$PATH"
export PYTHONPATH="${PREFIX}/lib:$PYTHONPATH"
echo "  [server] $(gufi_ls notes | tr '\n' ' ')"
export PATH="${CLIENT_BIN}:$PATH"
export PYTHONPATH="${CLIENT_BIN}:$PYTHONPATH"
echo "  [client] $(gufi_ls notes | tr '\n' ' ')"
