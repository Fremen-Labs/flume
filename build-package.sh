#!/usr/bin/env bash
# Flume Package Builder
#
# Assembles a distributable flume-<VERSION>.tar.gz from the pre-patched
# sources inside this directory. Completely self-contained — no external
# directories required.
#
# Usage:
#   bash build-package.sh [VERSION]
#
# Examples:
#   bash build-package.sh             # uses today's date: flume-20250318.tar.gz
#   bash build-package.sh 1.0.0       # uses explicit version: flume-1.0.0.tar.gz
#
# Output: ~/Flume/dist/flume-<VERSION>.tar.gz
#
# All sources live under ~/Flume/src/ (pre-patched, no external deps).
# Install scaffolding (install.sh, .env.template, setup/) lives in ~/Flume/install/.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="${1:-$(date +%Y%m%d)}"
PKG_NAME="flume-${VERSION}"
DIST_DIR="${SCRIPT_DIR}/dist"
STAGE="${DIST_DIR}/_staging/${PKG_NAME}"
SRC="${SCRIPT_DIR}/src"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "  ${CYAN}▸${NC} $*"; }
success() { echo -e "  ${GREEN}✓${NC} $*"; }
warn()    { echo -e "  ${YELLOW}!${NC} $*"; }
error()   { echo -e "  ${RED}✗${NC} $*"; exit 1; }

echo ""
echo -e "${CYAN}${BOLD}╔══════════════════════════════════════╗${NC}"
echo -e "${CYAN}${BOLD}║       Flume Package Builder         ║${NC}"
echo -e "${CYAN}${BOLD}╚══════════════════════════════════════╝${NC}"
echo ""
echo "  Package : ${PKG_NAME}"
echo "  Output  : ${DIST_DIR}/${PKG_NAME}.tar.gz"
echo ""

# =============================================================================
# Preflight checks
# =============================================================================
echo -e "${BOLD}Preflight checks...${NC}"

[ -d "${SRC}" ] || error "src/ directory not found at ${SRC}"
success "src/ present"

[ -d "${SRC}/dashboard" ]      || error "src/dashboard/ not found"
success "src/dashboard/ present"

[ -d "${SRC}/worker-manager" ] || error "src/worker-manager/ not found"
success "src/worker-manager/ present"

[ -d "${SRC}/frontend/dist" ]  || warn "src/frontend/dist/ not found — UI will not be served without rebuild"

[ -f "${SCRIPT_DIR}/install/install.sh" ] || error "install/install.sh not found"
[ -f "${SCRIPT_DIR}/install/README.md" ] || error "install/README.md not found"
if [ ! -f "${SCRIPT_DIR}/install/.env.template" ]; then
    warn "install/.env.template not found — generating a default template"
    cat > "${SCRIPT_DIR}/install/.env.template" << 'EOF'
# =============================================================================
# Flume Configuration (auto-generated fallback)
# =============================================================================
ES_URL=https://localhost:9200
ES_API_KEY=AUTO_GENERATED_BY_INSTALLER
ES_VERIFY_TLS=false
ES_INDEX_TASKS=agent-task-records
ES_INDEX_HANDOFFS=agent-handoff-records
ES_INDEX_FAILURES=agent-failure-records
ES_INDEX_PROVENANCE=agent-provenance-records
ES_INDEX_MEMORY=agent-memory-entries

DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8765

LLM_PROVIDER=ollama
LLM_BASE_URL=http://localhost:11434
LLM_API_KEY=
OPENAI_OAUTH_STATE_FILE=.openai-oauth.json
OPENAI_OAUTH_TOKEN_URL=https://auth.openai.com/oauth/token
LLM_MODEL=llama3.2

GH_TOKEN=
ADO_TOKEN=
ADO_ORG_URL=

OPENBAO_ADDR=
OPENBAO_TOKEN=
OPENBAO_MOUNT=secret
OPENBAO_PATH=flume

GIT_USER_NAME=FlumeAgent
GIT_USER_EMAIL=agent@flume.local
EXECUTION_HOST=localhost
WORKER_MANAGER_POLL_SECONDS=15
EOF
fi
success "install/ scaffolding present"

echo ""

# =============================================================================
# Clean and create staging directory
# =============================================================================
echo -e "${BOLD}Setting up staging area...${NC}"
rm -rf "${STAGE}"
mkdir -p "${STAGE}"
success "Staging: ${STAGE}"
echo ""

# =============================================================================
# Shared runtime (OpenBao bootstrap)
# =============================================================================
if [ -f "${SRC}/flume_secrets.py" ]; then
    cp "${SRC}/flume_secrets.py" "${STAGE}/"
    success "flume_secrets.py (OpenBao / config bootstrap)"
fi

# Copy dashboard
# =============================================================================
echo -e "${BOLD}Copying dashboard...${NC}"
mkdir -p "${STAGE}/dashboard"
cp "${SRC}/dashboard/server.py"    "${STAGE}/dashboard/"
cp "${SRC}/dashboard/llm_client.py" "${STAGE}/dashboard/"
cp "${SRC}/dashboard/llm_settings.py" "${STAGE}/dashboard/"
cp "${SRC}/dashboard/repo_settings.py" "${STAGE}/dashboard/"
cp "${SRC}/dashboard/run.sh"       "${STAGE}/dashboard/"
[ -f "${SRC}/dashboard/index.html" ] && cp "${SRC}/dashboard/index.html" "${STAGE}/dashboard/" || true
DASHBOARD_FILES=$(find "${STAGE}/dashboard" -type f | wc -l)
success "dashboard/ (${DASHBOARD_FILES} files)"

# =============================================================================
# Copy worker-manager
# =============================================================================
echo -e "${BOLD}Copying worker-manager...${NC}"
mkdir -p "${STAGE}/worker-manager"
for f in manager.py worker_handlers.py agent_runner.py llm_client.py run.sh; do
    if [ -f "${SRC}/worker-manager/${f}" ]; then
        cp "${SRC}/worker-manager/${f}" "${STAGE}/worker-manager/"
        success "worker-manager/${f}"
    else
        warn "worker-manager/${f} — not found, skipping"
    fi
done

# =============================================================================
# Copy agents (system prompts)
# =============================================================================
echo -e "${BOLD}Copying agents...${NC}"
if [ -d "${SRC}/agents" ]; then
    cp -r "${SRC}/agents" "${STAGE}/agents"
    AGENT_COUNT=$(find "${STAGE}/agents" -name "SYSTEM_PROMPT.md" | wc -l)
    success "agents/ (${AGENT_COUNT} agent roles)"
else
    warn "src/agents/ not found — skipping"
fi

# =============================================================================
# Copy memory/es (index templates + utility scripts)
# =============================================================================
echo -e "${BOLD}Copying memory/es...${NC}"
if [ -d "${SRC}/memory/es" ]; then
    mkdir -p "${STAGE}/memory"
    if command -v rsync &>/dev/null; then
        rsync -a \
            --exclude='.env' \
            --exclude='.env.local' \
            --exclude='__pycache__' \
            --exclude='*.pyc' \
            "${SRC}/memory/es/" "${STAGE}/memory/es/"
    else
        cp -r "${SRC}/memory/es" "${STAGE}/memory/es"
    fi
    success "memory/es/ (index templates + scripts)"
else
    warn "src/memory/es/ not found — skipping"
fi

# =============================================================================
# Copy frontend
# =============================================================================
echo -e "${BOLD}Copying frontend...${NC}"
mkdir -p "${STAGE}/frontend"

if [ -d "${SRC}/frontend/dist" ]; then
    cp -r "${SRC}/frontend/dist" "${STAGE}/frontend/dist"
    DIST_FILES=$(find "${STAGE}/frontend/dist" -type f | wc -l)
    success "frontend/dist/ (${DIST_FILES} files, pre-built UI)"
else
    warn "src/frontend/dist/ not found — UI not included"
fi

if [ -d "${SRC}/frontend/src" ]; then
    info "Copying frontend source..."
    if command -v rsync &>/dev/null; then
        rsync -a \
            --exclude='node_modules' \
            --exclude='dist' \
            --exclude='.git' \
            --exclude='.env*' \
            "${SRC}/frontend/src/" "${STAGE}/frontend/src/"
    else
        cp -r "${SRC}/frontend/src" "${STAGE}/frontend/src"
        rm -rf "${STAGE}/frontend/src/node_modules" \
               "${STAGE}/frontend/src/dist" \
               "${STAGE}/frontend/src/.git" 2>/dev/null || true
    fi
    success "frontend/src/ (source for rebuilding)"
fi

# =============================================================================
# Copy install scaffolding
# =============================================================================
echo -e "${BOLD}Copying install scaffolding...${NC}"
cp "${SCRIPT_DIR}/install/install.sh"    "${STAGE}/install.sh"
cp "${SCRIPT_DIR}/setup.sh"              "${STAGE}/setup.sh"
cp "${SCRIPT_DIR}/flume"                 "${STAGE}/flume"
cp "${SCRIPT_DIR}/install/.env.template" "${STAGE}/.env.template"
cp "${SCRIPT_DIR}/install/README.md"     "${STAGE}/README.md"
mkdir -p "${STAGE}/setup"
cp "${SCRIPT_DIR}/install/setup/"*.sh   "${STAGE}/setup/"
[ -f "${SCRIPT_DIR}/install/setup/hydrate-openbao-env.py" ] && cp "${SCRIPT_DIR}/install/setup/hydrate-openbao-env.py" "${STAGE}/setup/"
[ -f "${SCRIPT_DIR}/install/flume.config.example.json" ] && cp "${SCRIPT_DIR}/install/flume.config.example.json" "${STAGE}/flume.config.example.json"
[ -f "${SCRIPT_DIR}/install/setup/flume-dashboard.service.template" ] && cp "${SCRIPT_DIR}/install/setup/flume-dashboard.service.template" "${STAGE}/setup/"
success "install.sh, setup.sh, flume, .env.template, README.md, setup/"

# =============================================================================
# Write clean data files (no user data, no API keys)
# =============================================================================
echo -e "${BOLD}Writing clean data files...${NC}"
echo '{"projects": []}' > "${STAGE}/projects.json"
success "projects.json (empty)"

echo '{}' > "${STAGE}/sequence_counters.json"
success "sequence_counters.json (reset)"

mkdir -p "${STAGE}/plan-sessions"
success "plan-sessions/ (empty)"

mkdir -p "${STAGE}/worker-manager"
echo '{"workers": []}' > "${STAGE}/worker-manager/state.json"
success "worker-manager/state.json (clean)"

# =============================================================================
# Scrub any residual sensitive files
# =============================================================================
echo -e "${BOLD}Scrubbing sensitive files...${NC}"
find "${STAGE}" -name ".env"        -not -name ".env.template" -delete 2>/dev/null || true
find "${STAGE}" -name ".env.local"  -delete 2>/dev/null || true
find "${STAGE}" -name "*.log"       -delete 2>/dev/null || true
find "${STAGE}" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "${STAGE}" -name "*.pyc"       -delete 2>/dev/null || true
find "${STAGE}" -name ".DS_Store"   -delete 2>/dev/null || true
find "${STAGE}/plan-sessions" -name "*.json" -delete 2>/dev/null || true

# Defensive: prune any accidentally-copied git repos into staging.
# This prevents project clones (like your Project-Site-IQ checkout) from
# ending up inside the final distributable tarball.
if command -v find &>/dev/null; then
    while IFS= read -r -d '' git_dir; do
        repo_dir="$(dirname "${git_dir}")"
        rm -rf "${repo_dir}" 2>/dev/null || true
    done < <(find "${STAGE}" -type d -name ".git" -prune -print0 2>/dev/null || true)
fi
success "Sensitive files removed"

# =============================================================================
# Set file permissions
# =============================================================================
echo -e "${BOLD}Setting permissions...${NC}"
chmod +x "${STAGE}/install.sh"
chmod +x "${STAGE}/setup.sh"
chmod +x "${STAGE}/flume"
chmod +x "${STAGE}/setup/"*.sh
chmod +x "${STAGE}/setup/hydrate-openbao-env.py" 2>/dev/null || true
chmod +x "${STAGE}/dashboard/run.sh"      2>/dev/null || true
chmod +x "${STAGE}/worker-manager/run.sh" 2>/dev/null || true
chmod +x "${STAGE}/memory/es/scripts/"*.sh 2>/dev/null || true
success "Executable bits set"

# =============================================================================
# Security scan
# =============================================================================
echo -e "${BOLD}Security scan...${NC}"
SECRET_HITS=0

if grep -rn "ES_API_KEY=[A-Za-z0-9+/=]\{20,\}" "${STAGE}" 2>/dev/null | grep -v "PASTE_YOUR"; then
    warn "Possible real ES_API_KEY found — verify before distributing"
    SECRET_HITS=$((SECRET_HITS + 1))
fi

if grep -rn "10\.10\.1\.15" "${STAGE}" 2>/dev/null | grep -v "\.md:"; then
    warn "Original hardcoded IP 10.10.1.15 still present"
    SECRET_HITS=$((SECRET_HITS + 1))
fi

OPENCLAW_HITS=$(grep -rn "\.openclaw" "${STAGE}" 2>/dev/null \
    --include="*.py" --include="*.sh" \
    | grep -v "memory/es/examples" \
    | grep -v "bootstrap_memory" \
    | grep -v "LOOM_WORKSPACE" || true)
if [ -n "$OPENCLAW_HITS" ]; then
    echo "$OPENCLAW_HITS"
    warn "Reference to .openclaw still present in core files above"
    SECRET_HITS=$((SECRET_HITS + 1))
fi

if [ "$SECRET_HITS" -eq 0 ]; then
    success "No secrets or hardcoded paths detected"
else
    warn "${SECRET_HITS} potential issue(s) found. Review before distributing."
fi

# =============================================================================
# Create tarball
# =============================================================================
echo ""
echo -e "${BOLD}Creating archive...${NC}"
mkdir -p "${DIST_DIR}"
STAGING_PARENT="${DIST_DIR}/_staging"
(cd "${STAGING_PARENT}" && tar czf "${DIST_DIR}/${PKG_NAME}.tar.gz" "${PKG_NAME}")
success "Created: ${DIST_DIR}/${PKG_NAME}.tar.gz"

TAR_SIZE=$(du -sh "${DIST_DIR}/${PKG_NAME}.tar.gz" | cut -f1)
FILE_COUNT=$(tar -tzf "${DIST_DIR}/${PKG_NAME}.tar.gz" | wc -l)
info "Archive size: ${TAR_SIZE} | Files: ${FILE_COUNT}"

CHECKSUM=$(sha256sum "${DIST_DIR}/${PKG_NAME}.tar.gz" | cut -d' ' -f1)
echo "${CHECKSUM}  ${PKG_NAME}.tar.gz" > "${DIST_DIR}/${PKG_NAME}.tar.gz.sha256"
success "SHA256: ${CHECKSUM}"

# =============================================================================
# Clean up staging
# =============================================================================
rm -rf "${STAGING_PARENT}"

# =============================================================================
# Done
# =============================================================================
echo ""
echo -e "${GREEN}${BOLD}Package ready!${NC}"
echo ""
echo "  Archive : ${DIST_DIR}/${PKG_NAME}.tar.gz"
echo "  SHA256  : ${CHECKSUM}"
echo ""
echo "To install on another machine:"
echo "  scp ${DIST_DIR}/${PKG_NAME}.tar.gz user@host:~/"
echo "  ssh user@host"
echo "  tar -xzf ${PKG_NAME}.tar.gz"
echo "  cd ${PKG_NAME}/"
echo "  bash setup.sh"
echo ""
echo "After install completes on that machine, run:"
echo "  # Extracted package layout (flume-<VERSION>/):"
echo "  bash dashboard/run.sh"
echo "  # In another terminal:"
echo "  bash worker-manager/run.sh"
echo ""
echo "If running directly from a git clone layout:"
echo "  bash src/dashboard/run.sh"
echo "  # In another terminal:"
echo "  bash src/worker-manager/run.sh"
echo ""
echo "Then open: http://<host>:8765"
echo ""
