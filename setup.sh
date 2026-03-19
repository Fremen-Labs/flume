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

CYAN='\033[0;36m'
GREEN='\033[0;32m'
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

echo ""
echo -e "${GREEN}${BOLD}Setup complete.${NC}"
echo ""
