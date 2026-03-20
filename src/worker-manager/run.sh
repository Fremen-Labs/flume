#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${WORKSPACE_ROOT}/.." && pwd)"

export PYTHONPATH="${WORKSPACE_ROOT}${PYTHONPATH:+:$PYTHONPATH}"

has_cfg=false
[ -f "${REPO_ROOT}/flume.config.json" ] && has_cfg=true
[ -f "${WORKSPACE_ROOT}/flume.config.json" ] && has_cfg=true
[ -f "${REPO_ROOT}/.env" ] && has_cfg=true
[ -f "${WORKSPACE_ROOT}/.env" ] && has_cfg=true
[ -n "${OPENBAO_ADDR:-}" ] && has_cfg=true

if [ "$has_cfg" = "false" ]; then
    echo "Missing configuration. Add flume.config.json (OpenBao) or .env — see install/flume.config.example.json"
    exit 1
fi

ENV_FILE=""
if [ -f "${REPO_ROOT}/.env" ]; then
    ENV_FILE="${REPO_ROOT}/.env"
elif [ -f "${WORKSPACE_ROOT}/.env" ]; then
    ENV_FILE="${WORKSPACE_ROOT}/.env"
fi

if [ -n "${ENV_FILE}" ]; then
    set -a
    # shellcheck source=/dev/null
    source "${ENV_FILE}"
    set +a
fi

export LOOM_WORKSPACE="${WORKSPACE_ROOT}"
export WORKER_MANAGER_POLL_SECONDS="${WORKER_MANAGER_POLL_SECONDS:-15}"

# Apply git identity when present (non-secret; may come from OpenBao KV or .env)
if [ -n "${GIT_USER_NAME:-}" ]; then
    git config --global user.name "${GIT_USER_NAME}" 2>/dev/null || true
fi
if [ -n "${GIT_USER_EMAIL:-}" ]; then
    git config --global user.email "${GIT_USER_EMAIL}" 2>/dev/null || true
fi

exec python3 "${SCRIPT_DIR}/manager.py"
