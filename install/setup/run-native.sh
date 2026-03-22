#!/usr/bin/env bash
# Run Flume dashboard + worker-manager against Elasticsearch and OpenBao on the host
# (no Docker Compose). Use when ES already runs locally (e.g. systemd) and OpenBao
# is reachable on localhost — avoids port clashes with compose’s bundled Elasticsearch.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
mkdir -p "$ROOT/logs"

export FLUME_CONFIG="${FLUME_CONFIG:-$ROOT/config.toml}"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
# Defaults match a typical local TLS ES + dev OpenBao; override via environment.
export ES_URL="${ES_URL:-https://127.0.0.1:9200}"
export OPENBAO_ADDR="${OPENBAO_ADDR:-http://127.0.0.1:8200}"
export ES_VERIFY_TLS="${ES_VERIFY_TLS:-false}"
export DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
export DASHBOARD_PORT="${DASHBOARD_PORT:-8765}"
export PATH="${HOME}/.local/bin:${PATH}"

if ! command -v uv >/dev/null 2>&1; then
	echo "uv not found. Run ./flume install first (it bootstraps uv) or add ~/.local/bin to PATH."
	exit 1
fi

if command -v ss >/dev/null 2>&1 && ss -tln 2>/dev/null | grep -q ':8765 '; then
	echo "Port 8765 is already in use. Stop the other process or set DASHBOARD_PORT and ensure nothing else binds 8765."
	exit 1
fi

if [[ -z "${ES_API_KEY:-}" ]] && [[ ! -f "$ROOT/.env" ]] && [[ ! -f "$ROOT/flume.config.json" ]]; then
	echo "Warning: No ES_API_KEY in the environment, no .env, and no flume.config.json."
	echo "  Set ES_API_KEY (or use .env / OpenBao) or the UI will load but Elasticsearch calls will 401."
fi

echo "Starting Flume (native)…"
echo "  ES_URL=$ES_URL  OPENBAO_ADDR=$OPENBAO_ADDR  ES_VERIFY_TLS=$ES_VERIFY_TLS"

nohup uv run python -u "$ROOT/src/dashboard/server.py" >>"$ROOT/logs/dashboard.log" 2>&1 &
D_PID=$!
echo "  dashboard pid=$D_PID  → $ROOT/logs/dashboard.log"

nohup uv run python -u "$ROOT/src/worker-manager/manager.py" >>"$ROOT/logs/worker-manager.log" 2>&1 &
M_PID=$!
echo "  worker-manager pid=$M_PID  → $ROOT/logs/worker-manager.log"

echo ""
echo "Dashboard: http://127.0.0.1:${DASHBOARD_PORT:-8765}"
echo "If you see Elasticsearch 401s in the logs, set a valid ES_API_KEY (see install/README.md)."
