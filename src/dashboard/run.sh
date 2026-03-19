#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${WORKSPACE_ROOT}/.env"

# Support both layouts:
# - packaged install: <root>/.env next to dashboard/
# - git clone:       <repo>/.env with app under <repo>/src/
if [ ! -f "${ENV_FILE}" ] && [ -f "${WORKSPACE_ROOT}/../.env" ]; then
    ENV_FILE="${WORKSPACE_ROOT}/../.env"
fi

if [ ! -f "${ENV_FILE}" ]; then
    echo "Missing .env file."
    echo "Expected one of:"
    echo "  ${WORKSPACE_ROOT}/.env"
    echo "  ${WORKSPACE_ROOT}/../.env"
    echo "Create it with: cp install/.env.template .env"
    exit 1
fi

set -a
source "${ENV_FILE}"
export LOOM_WORKSPACE="${WORKSPACE_ROOT}"
export LOOM_FRONTEND_DIST="${WORKSPACE_ROOT}/frontend/dist"
set +a
exec python3 "${SCRIPT_DIR}/server.py"
