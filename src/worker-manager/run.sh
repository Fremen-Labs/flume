#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${WORKSPACE_ROOT}/.env"

# Support both layouts:
# - packaged install: <root>/.env next to worker-manager/
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
export WORKER_MANAGER_POLL_SECONDS="${WORKER_MANAGER_POLL_SECONDS:-15}"
set +a

# Check ES_API_KEY before starting (manager.py would fail anyway)
if [ -z "${ES_API_KEY:-}" ] || [ "${ES_API_KEY}" = "AUTO_GENERATED_BY_INSTALLER" ]; then
    echo "ES_API_KEY is missing or invalid in .env"
    echo ""
    echo "Run the installer first:  bash install/install.sh"
    echo "Or bootstrap credentials: ELASTIC_PASSWORD=yourpassword bash install/setup/bootstrap-es-credentials.sh"
    echo "(Get the elastic password from ES install, or reset: sudo /usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -i)"
    exit 1
fi

# Apply git identity from .env
if [ -n "${GIT_USER_NAME:-}" ]; then
    git config --global user.name "${GIT_USER_NAME}" 2>/dev/null || true
fi
if [ -n "${GIT_USER_EMAIL:-}" ]; then
    git config --global user.email "${GIT_USER_EMAIL}" 2>/dev/null || true
fi

exec python3 "${SCRIPT_DIR}/manager.py"
