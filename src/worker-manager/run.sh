#!/usr/bin/env bash
# AP-10 / P6b: .env detection and sourcing removed. Bootstrap config comes
# from process env (Docker/CLI injects ES_URL, OPENBAO_TOKEN etc. at container
# start) or from ES flume-settings via apply_runtime_config() in manager.py.
# Secrets are read from OpenBao by hydrate_secrets_from_openbao() at startup.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${WORKSPACE_ROOT}/.." && pwd)"

export PYTHONPATH="${WORKSPACE_ROOT}${PYTHONPATH:+:$PYTHONPATH}"

# Config is present when any of the following are available:
#   1. flume.config.json (legacy installer artifact)
#   2. OPENBAO_ADDR + OPENBAO_TOKEN in process env (Docker injects these)
#   3. ES_URL in process env (compose injects ES_URL=http://elasticsearch:9200)
has_cfg=false
[ -f "${REPO_ROOT}/flume.config.json" ]  && has_cfg=true
[ -f "${WORKSPACE_ROOT}/flume.config.json" ] && has_cfg=true
[ -n "${OPENBAO_ADDR:-}" ] && [ -n "${OPENBAO_TOKEN:-}" ] && has_cfg=true
[ -n "${ES_URL:-}" ] && has_cfg=true

if [ "$has_cfg" = "false" ]; then
    echo "Missing configuration. Set OPENBAO_ADDR + OPENBAO_TOKEN in process environment, or provide flume.config.json — see install/flume.config.example.json"
    exit 1
fi

export FLUME_WORKSPACE="${WORKSPACE_ROOT}"
export WORKER_MANAGER_POLL_SECONDS="${WORKER_MANAGER_POLL_SECONDS:-15}"

# Apply git identity when present (non-secret; comes from OpenBao KV or process env)
if [ -n "${GIT_USER_NAME:-}" ]; then
    git config --global user.name "${GIT_USER_NAME}" 2>/dev/null || true
fi
if [ -n "${GIT_USER_EMAIL:-}" ]; then
    git config --global user.email "${GIT_USER_EMAIL}" 2>/dev/null || true
fi

python3 "${SCRIPT_DIR}/worker_handlers.py" &
exec python3 "${SCRIPT_DIR}/manager.py"
