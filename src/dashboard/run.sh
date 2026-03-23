#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${WORKSPACE_ROOT}/.." && pwd)"

# PYTHONPATH: git clone has code under src/; package layout has code at workspace root
export PYTHONPATH="${WORKSPACE_ROOT}${PYTHONPATH:+:$PYTHONPATH}"

# Require OpenBao bootstrap and/or legacy .env
has_cfg=false
[ -f "${REPO_ROOT}/flume.config.json" ] && has_cfg=true
[ -f "${WORKSPACE_ROOT}/flume.config.json" ] && has_cfg=true
[ -f "${REPO_ROOT}/.env" ] && has_cfg=true
[ -f "${WORKSPACE_ROOT}/.env" ] && has_cfg=true
[ -n "${OPENBAO_ADDR:-}" ] && has_cfg=true

if [ "$has_cfg" = "false" ]; then
    echo "Missing configuration."
    echo "  Preferred: copy install/flume.config.example.json to ${REPO_ROOT}/flume.config.json"
    echo "  and store secrets in OpenBao KV (see install/README.md)."
    echo "  Legacy: create ${REPO_ROOT}/.env from install/.env.template"
    exit 1
fi

# Prefer repo-root .env, then workspace .env (git: flume/.env over flume/src/.env)
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

export LOOM_WORKSPACE="${REPO_ROOT}"
export LOOM_FRONTEND_DIST="${WORKSPACE_ROOT}/frontend/dist"
exec python3 "${SCRIPT_DIR}/server.py"
