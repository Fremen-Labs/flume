#!/usr/bin/env bash
# Push Elasticsearch (and related) bootstrap values from .es-bootstrap.env into OpenBao KV.
#
# Prerequisites:
#   - openbao CLI on PATH
#   - OpenBao reachable (see flume.config.json openbao.addr)
#   - Authenticated: export BAO_TOKEN=...  OR token in tokenFile from flume.config.json
#
# Usage (from Flume repo root):
#   BAO_TOKEN=s.xxx bash install/setup/sync-bootstrap-to-openbao.sh
#   BAO_TOKEN=s.xxx bash install/setup/sync-bootstrap-to-openbao.sh /path/to/.es-bootstrap.env

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# install/ -> repo root
FLUME_ROOT="$(cd "${INSTALL_DIR}/.." && pwd)"

BOOT="${1:-${INSTALL_DIR}/.es-bootstrap.env}"
CONFIG="${FLUME_ROOT}/flume.config.json"

if ! command -v openbao >/dev/null 2>&1; then
  echo "openbao CLI not found; skipping sync-bootstrap-to-openbao."
  exit 0
fi

if [ ! -f "${BOOT}" ]; then
  echo "No bootstrap file at ${BOOT}; nothing to sync."
  exit 0
fi

# shellcheck source=/dev/null
set -a
source "${BOOT}"
set +a

if [ -z "${ES_API_KEY:-}" ]; then
  echo "ES_API_KEY missing in ${BOOT}; nothing to sync."
  exit 0
fi

MOUNT=secret
PATH_SEC=flume
ADDR="${BAO_ADDR:-${VAULT_ADDR:-${OPENBAO_ADDR:-http://127.0.0.1:8200}}}"

if [ -f "${CONFIG}" ] && command -v python3 >/dev/null 2>&1; then
  readarray -t OB < <(python3 - <<PY
import json
from pathlib import Path
p = Path("${CONFIG}")
if p.is_file():
    d = json.loads(p.read_text())
    ob = d.get("openbao") or {}
    print(ob.get("mount", "secret"))
    print(ob.get("path", "flume"))
    print(ob.get("addr", "${ADDR}"))
PY
)
  if [ "${#OB[@]}" -ge 3 ]; then
    MOUNT="${OB[0]}"
    PATH_SEC="${OB[1]}"
    ADDR="${OB[2]}"
  fi
fi

export BAO_ADDR="${ADDR}"
export VAULT_ADDR="${ADDR}"

if [ -z "${BAO_TOKEN:-}" ] && [ -z "${VAULT_TOKEN:-}" ] && [ -z "${OPENBAO_TOKEN:-}" ]; then
  if [ -f "${CONFIG}" ] && command -v python3 >/dev/null 2>&1; then
    TF="$(python3 -c "import json;from pathlib import Path;p=Path('${CONFIG}');d=json.loads(p.read_text()) if p.is_file() else {};print((d.get('openbao')or{}).get('tokenFile',''))")"
    if [ -n "${TF}" ]; then
      EXP="$(eval echo "${TF}")"
      if [ -f "${EXP}" ]; then
        export BAO_TOKEN="$(cat "${EXP}")"
        export VAULT_TOKEN="${BAO_TOKEN}"
      fi
    fi
  fi
fi

if [ -z "${BAO_TOKEN:-}" ] && [ -z "${VAULT_TOKEN:-}" ]; then
  echo "Set BAO_TOKEN (or VAULT_TOKEN) or configure openbao.tokenFile in flume.config.json"
  exit 1
fi

export BAO_TOKEN="${BAO_TOKEN:-${VAULT_TOKEN}}"
export VAULT_TOKEN="${BAO_TOKEN}"

REF="${MOUNT}/${PATH_SEC}"
echo "Writing ES_* to OpenBao at ${REF} (${ADDR})..."
openbao kv put "${REF}" \
  ES_URL="${ES_URL:-https://localhost:9200}" \
  ES_API_KEY="${ES_API_KEY}" \
  ES_VERIFY_TLS="${ES_VERIFY_TLS:-false}"

echo "OK. Remove ES_API_KEY from .env if you use OpenBao-only mode."
