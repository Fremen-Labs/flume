#!/usr/bin/env bash
# Flume dependency verifier
# Checks all required and optional system dependencies.
# Exits with code 0 if all required deps are present, 1 otherwise.

set -uo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ERRORS=0

ok()   { echo -e "  ${GREEN}[OK]${NC}  $1"; }
warn() { echo -e "  ${YELLOW}[WARN]${NC} $1"; }
fail() { echo -e "  ${RED}[FAIL]${NC} $1"; ERRORS=$((ERRORS + 1)); }

echo "Checking Flume dependencies..."
echo ""

# --- Required ---
echo "Required:"

# Python 3.9+
if command -v python3 &>/dev/null; then
    PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
    PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 9 ]; then
        ok "python3 ${PY_VER}"
    else
        fail "python3 ${PY_VER} — version 3.9 or higher required"
    fi
else
    fail "python3 — not found (install: sudo dnf install python3  OR  sudo apt install python3)"
fi

# git
if command -v git &>/dev/null; then
    ok "git $(git --version | awk '{print $3}')"
else
    fail "git — not found (install: sudo dnf install git  OR  sudo apt install git)"
fi

# pgrep (procps)
if command -v pgrep &>/dev/null; then
    ok "pgrep (procps)"
else
    fail "pgrep — not found (install: sudo dnf install procps-ng  OR  sudo apt install procps)"
fi

# curl (needed by ES setup scripts)
if command -v curl &>/dev/null; then
    ok "curl $(curl --version | head -1 | awk '{print $2}')"
else
    fail "curl — not found (install: sudo dnf install curl  OR  sudo apt install curl)"
fi

echo ""

# --- Optional ---
echo "Optional:"

# gh (GitHub CLI) — only needed for PR creation feature
if command -v gh &>/dev/null; then
    ok "gh (GitHub CLI) $(gh --version | head -1 | awk '{print $3}') — PR creation available"
else
    warn "gh (GitHub CLI) — not found. PR creation will be unavailable."
    echo "         Install: https://cli.github.com/"
fi

# node / npm — frontend build + Codex CLI
if command -v node &>/dev/null; then
    ok "node $(node --version) — frontend can be rebuilt from source"
else
    warn "node — not found. Run install.sh (installs Node LTS + Codex) or see https://nodejs.org/"
fi

if command -v codex &>/dev/null; then
    ok "codex CLI — OpenAI Codex ($(codex --version 2>/dev/null | head -n 1 || echo present))"
else
    warn "codex CLI — not found. Plan New Work (OAuth) needs it; re-run: sudo bash install/setup/install-codex-cli.sh"
fi

# openbao — optional secrets CLI
if command -v openbao &>/dev/null; then
    ok "openbao CLI — available"
else
    warn "openbao CLI — not found. Optional; installer can add it."
fi

# Elasticsearch — check if already running
if curl -sk "https://localhost:9200/_cluster/health" -u "elastic:*" &>/dev/null 2>&1 || \
   curl -sk "https://localhost:9200/" &>/dev/null 2>&1; then
    ok "Elasticsearch — appears to be running at https://localhost:9200"
else
    warn "Elasticsearch — not detected at https://localhost:9200"
    echo "         Run setup/install-elasticsearch.sh to install it."
fi

echo ""

if [ "$ERRORS" -gt 0 ]; then
    echo -e "${RED}${ERRORS} required dependency/dependencies missing. Please install them and re-run.${NC}"
    exit 1
else
    echo -e "${GREEN}All required dependencies are satisfied.${NC}"
    exit 0
fi
