#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
set -a
source "${WORKSPACE_ROOT}/.env"
export LOOM_WORKSPACE="${WORKSPACE_ROOT}"
export LOOM_FRONTEND_DIST="${WORKSPACE_ROOT}/frontend/dist"
set +a
exec python3 "${SCRIPT_DIR}/server.py"
