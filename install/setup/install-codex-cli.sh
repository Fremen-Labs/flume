#!/usr/bin/env bash
# Flume — OpenAI Codex CLI (@openai/codex)
#
# Ensures Node.js 18+ (via install-nodejs-lts.sh) and runs:
#   npm install -g @openai/codex
#
# Used by Plan New Work (OAuth via codex app-server) and ./flume codex-app-server.
#
# Usage:
#   sudo bash install/setup/install-codex-cli.sh
#
# Non-root: re-invokes with sudo.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${EUID}" -ne 0 ]; then
    info "Requesting sudo to install Codex CLI system-wide..."
    exec sudo bash "$0" "$@"
fi

export PATH="/usr/local/bin:/usr/local/sbin:${PATH}"

if command -v codex >/dev/null 2>&1; then
    success "codex already installed: $(codex --version 2>/dev/null | head -n 1 || echo codex)"
    exit 0
fi

if ! command -v node >/dev/null 2>&1 || ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)' 2>/dev/null; then
    info "Installing Node.js LTS (required for npm / @openai/codex)..."
    bash "${HERE}/install-nodejs-lts.sh"
fi

if ! command -v npm >/dev/null 2>&1; then
    error "npm not found after Node.js install."
fi

info "Installing @openai/codex globally (this may take a minute)..."
npm install -g @openai/codex

hash -r 2>/dev/null || true
if ! command -v codex >/dev/null 2>&1; then
    warn "codex not on PATH immediately; try: hash -r  or  new shell  (expected /usr/local/bin/codex)"
    if [ -x /usr/local/bin/codex ]; then
        success "Found /usr/local/bin/codex"
        exit 0
    fi
    error "npm install finished but codex command not found."
fi

success "Codex CLI: $(codex --version 2>/dev/null | head -n 1 || echo installed)"
