#!/usr/bin/env bash
# Flume — One-command setup
#
# Run this once to install everything and get a working Flume.
# Works for both git clone and extracted package.
#
# Usage:
#   cd ~/flume
#   bash setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
WORKSPACE_ROOT="${SCRIPT_DIR}"
ENV_FILE="${WORKSPACE_ROOT}/.env"

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

echo ""
echo -e "${CYAN}${BOLD}╔══════════════════════════════════════╗${NC}"
echo -e "${CYAN}${BOLD}║     Flume — One-command setup        ║${NC}"
echo -e "${CYAN}${BOLD}╚══════════════════════════════════════╝${NC}"
echo ""

ensure_codex_runtime() {
    if command -v codex &>/dev/null; then
        return 0
    fi
    if command -v npm &>/dev/null; then
        echo ""
        echo "Installing Codex CLI..."
        npm install -g @openai/codex || true
    fi
}

# Run installer (install.sh or install/install.sh)
if [ -f "install/install.sh" ]; then
    echo "Running installer (git clone layout)..."
    bash install/install.sh
    # Build frontend for git clone
    if [ -d "src/frontend/src" ]; then
        if command -v npm &>/dev/null; then
            echo ""
            echo "Building frontend..."
            (cd src/frontend/src && npm install && npm run build)
            echo -e "${GREEN}Frontend built.${NC}"
        else
            echo ""
            echo "Skipping frontend build (npm not found). Use pre-built dist if available."
        fi
    fi
elif [ -f "install.sh" ]; then
    echo "Running installer (package layout)..."
    bash install.sh
else
    echo "Error: install.sh not found. Run from Flume root."
    exit 1
fi

# =============================================================================
# Ensure ES credentials (baked in — setup must not complete without valid ES)
# =============================================================================
es_credentials_valid() {
    [ -f "${ENV_FILE}" ] || return 1
    local key
    key=$(grep -E '^ES_API_KEY=' "${ENV_FILE}" | cut -d= -f2- || true)
    [ -n "${key}" ] && [ "${key}" != "AUTO_GENERATED_BY_INSTALLER" ]
}

ES_INSTALL_SCRIPT=""
BOOTSTRAP_SCRIPT=""
CREATE_INDICES_SCRIPT=""
if [ -f "install/setup/install-elasticsearch.sh" ]; then
    ES_INSTALL_SCRIPT="install/setup/install-elasticsearch.sh"
    BOOTSTRAP_SCRIPT="install/setup/bootstrap-es-credentials.sh"
    CREATE_INDICES_SCRIPT="install/setup/create-es-indices.sh"
    BOOTSTRAP_FILE="install/.es-bootstrap.env"
elif [ -f "setup/install-elasticsearch.sh" ]; then
    ES_INSTALL_SCRIPT="setup/install-elasticsearch.sh"
    BOOTSTRAP_SCRIPT="setup/bootstrap-es-credentials.sh"
    CREATE_INDICES_SCRIPT="setup/create-es-indices.sh"
    BOOTSTRAP_FILE=".es-bootstrap.env"
fi

while ! es_credentials_valid; do
    if [ "${FLUME_SKIP_ELASTIC_INSTALL:-}" == "auto" ] || [ "${FLUME_SKIP_ELASTIC_INSTALL:-}" == "true" ]; then
        echo ""
        echo -e "${GREEN}MATRIX ASSIMILATION OVERRIDE: Bypassing localized Elasticsearch sequence...${NC}"
        break
    fi
    if ! curl -sk "https://localhost:9200/" &>/dev/null && ! curl -sk "http://localhost:9200/" &>/dev/null; then
        echo ""
        echo -e "${YELLOW}Elasticsearch is not running. Start it and re-run setup.sh${NC}"
        exit 1
    fi
    if [ -z "${ES_INSTALL_SCRIPT}" ]; then
        echo ""
        echo -e "${YELLOW}ES credentials missing and no installer found.${NC}"
        exit 1
    fi
    echo ""
    echo -e "${YELLOW}Ensuring Elasticsearch credentials...${NC}"
    # Try install-elasticsearch (generates key via batch password reset)
    sudo bash "${ES_INSTALL_SCRIPT}" 2>/dev/null || bash "${ES_INSTALL_SCRIPT}" 2>/dev/null || true
    # Apply bootstrap if it was created
    if [ -f "${BOOTSTRAP_FILE}" ]; then
        BOOTSTRAP_KEY=$(grep -E '^ES_API_KEY=' "${BOOTSTRAP_FILE}" | cut -d= -f2- || true)
        if [ -n "${BOOTSTRAP_KEY}" ]; then
            if grep -qE '^ES_API_KEY=' "${ENV_FILE}" 2>/dev/null; then
                tmp=$(mktemp)
                while IFS= read -r line; do
                    if [[ "$line" == ES_API_KEY=* ]]; then
                        echo "ES_API_KEY=${BOOTSTRAP_KEY}"
                    else
                        echo "$line"
                    fi
                done < "${ENV_FILE}" > "${tmp}"
                mv "${tmp}" "${ENV_FILE}"
            else
                echo "ES_API_KEY=${BOOTSTRAP_KEY}" >> "${ENV_FILE}"
            fi
            echo -e "${GREEN}Applied ES credentials from bootstrap.${NC}"
            break
        fi
    fi
    # Interactive bootstrap as last resort
    if ! es_credentials_valid && [ -t 0 ] && [ -n "${BOOTSTRAP_SCRIPT}" ]; then
        echo ""
        echo "Enter the 'elastic' superuser password to generate an API key:"
        ELASTIC_PASSWORD="" bash "${BOOTSTRAP_SCRIPT}" || true
    fi
    if ! es_credentials_valid; then
        echo ""
        echo -e "${YELLOW}Could not obtain ES credentials automatically.${NC}"
        echo "Run: ELASTIC_PASSWORD=yourpassword bash ${BOOTSTRAP_SCRIPT}"
        exit 1
    fi
    break
done

# Create indices if we have credentials
if es_credentials_valid && [ -n "${CREATE_INDICES_SCRIPT}" ]; then
    echo ""
    echo "Creating Elasticsearch indices..."
    ENV_FILE="${ENV_FILE}" bash "${CREATE_INDICES_SCRIPT}" 2>/dev/null || true
fi

ensure_codex_runtime

# Install and start dashboard as background service (only if ES is configured)
if [ -f "flume" ] && es_credentials_valid; then
    chmod +x flume 2>/dev/null || true
    if [ -f "install/setup/install-flume-service.sh" ]; then
        echo ""
        echo "Installing dashboard service..."
        bash install/setup/install-flume-service.sh
        systemctl --user daemon-reload 2>/dev/null || true
        echo ""
        echo "Starting dashboard in background..."
        ./flume start 2>/dev/null || bash flume start
        ./flume codex-app-server start 2>/dev/null || true
    elif [ -f "setup/install-flume-service.sh" ]; then
        echo ""
        echo "Installing dashboard service..."
        bash setup/install-flume-service.sh
        systemctl --user daemon-reload 2>/dev/null || true
        echo ""
        echo "Starting dashboard in background..."
        ./flume start 2>/dev/null || bash flume start
        ./flume codex-app-server start 2>/dev/null || true
    fi
fi

echo ""
echo -e "${GREEN}${BOLD}Setup complete.${NC}"
echo ""
echo "Dashboard is running in the background. Use the flume CLI to control it:"
echo "  ./flume status   — Check status"
echo "  ./flume stop     — Stop dashboard"
echo "  ./flume restart  — Restart dashboard"
echo "  ./flume logs     — View logs"
echo "  ./flume codex-app-server status — Check Codex app-server background status"
echo ""
echo "Next step for OpenAI subscription usage (Codex OAuth):"
echo "  ./flume codex-oauth login-browser"
echo ""
echo "Then open the dashboard, choose Settings → LLM → OpenAI → OAuth, and restart if prompted."
echo ""
