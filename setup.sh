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

# Fallback: if ES credentials still missing (e.g. password reset failed), run interactive bootstrap
BOOTSTRAP_SCRIPT=""
CREATE_INDICES_SCRIPT=""
if [ -f "install/setup/bootstrap-es-credentials.sh" ]; then
    BOOTSTRAP_SCRIPT="install/setup/bootstrap-es-credentials.sh"
    CREATE_INDICES_SCRIPT="install/setup/create-es-indices.sh"
elif [ -f "setup/bootstrap-es-credentials.sh" ]; then
    BOOTSTRAP_SCRIPT="setup/bootstrap-es-credentials.sh"
    CREATE_INDICES_SCRIPT="setup/create-es-indices.sh"
fi
if [ -f "${ENV_FILE}" ] && [ -n "${BOOTSTRAP_SCRIPT}" ]; then
    ES_KEY=$(grep -E '^ES_API_KEY=' "${ENV_FILE}" | cut -d= -f2- || true)
    if [ -z "${ES_KEY}" ] || [ "${ES_KEY}" = "AUTO_GENERATED_BY_INSTALLER" ]; then
        if curl -sk "https://localhost:9200/" &>/dev/null && [ -t 0 ]; then
            echo ""
            echo -e "${YELLOW}Elasticsearch credentials could not be auto-generated.${NC}"
            echo "Enter the 'elastic' superuser password to generate an API key:"
            echo ""
            if bash "${BOOTSTRAP_SCRIPT}"; then
                echo ""
                echo "Creating Elasticsearch indices..."
                ENV_FILE="${ENV_FILE}" bash "${CREATE_INDICES_SCRIPT}" || true
            fi
        fi
    fi
fi

echo ""
echo -e "${GREEN}${BOLD}Setup complete.${NC}"
echo ""
