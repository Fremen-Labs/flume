#!/usr/bin/env bash
# Flume Interactive Installer
#
# Guides you through a complete Flume setup:
#   Step 1: Verify dependencies
#   Step 2: Install Elasticsearch (optional)
#   Step 3: Install OpenBao (optional)
#   Step 4: Configure .env
#   Step 5: Create Elasticsearch indices
#   Step 6: Set up workspace directories
#   Step 7: Final instructions
#
# Usage:
#   cd /path/to/extracted/flume/
#   bash install.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

banner() {
    echo ""
    echo -e "${CYAN}${BOLD}╔══════════════════════════════════════╗${NC}"
    echo -e "${CYAN}${BOLD}║        Flume Installer              ║${NC}"
    echo -e "${CYAN}${BOLD}╚══════════════════════════════════════╝${NC}"
    echo ""
}

step() {
    echo ""
    echo -e "${BOLD}${CYAN}──── Step $1: $2 ────${NC}"
    echo ""
}

prompt_yn() {
    local MSG="$1"
    local DEFAULT="${2:-y}"
    local OPTS
    if [ "$DEFAULT" = "y" ]; then OPTS="[Y/n]"; else OPTS="[y/N]"; fi
    while true; do
        read -r -p "$(echo -e "${YELLOW}?${NC} ${MSG} ${OPTS}: ")" REPLY
        REPLY="${REPLY:-$DEFAULT}"
        case "${REPLY,,}" in
            y|yes) return 0 ;;
            n|no)  return 1 ;;
            *)     echo "  Please answer y or n." ;;
        esac
    done
}

prompt_value() {
    local MSG="$1"
    local DEFAULT="${2:-}"
    local RESULT
    if [ -n "$DEFAULT" ]; then
        read -r -p "$(echo -e "${YELLOW}?${NC} ${MSG} [${DEFAULT}]: ")" RESULT
        echo "${RESULT:-$DEFAULT}"
    else
        while true; do
            read -r -p "$(echo -e "${YELLOW}?${NC} ${MSG}: ")" RESULT
            if [ -n "$RESULT" ]; then echo "$RESULT"; return; fi
            echo "  Value is required."
        done
    fi
}

replace_env_value() {
    local KEY="$1"
    local VALUE="$2"
    sed -i "s|^${KEY}=.*|${KEY}=${VALUE}|" "$ENV_FILE"
}

banner

echo "This installer will set up Flume on this machine."
echo "Install location: ${SCRIPT_DIR}"
echo ""
echo "Press Enter to continue or Ctrl+C to abort."
read -r

# =============================================================================
# Step 1: Verify dependencies
# =============================================================================
step 1 "Check Dependencies"

bash "${SCRIPT_DIR}/setup/verify-deps.sh" || {
    echo ""
    echo -e "${RED}Required dependencies are missing. Install them and re-run this installer.${NC}"
    exit 1
}

# =============================================================================
# Step 2: Install Elasticsearch
# =============================================================================
step 2 "Elasticsearch"

echo "Flume requires Elasticsearch 8 as its primary data store."
echo ""

# Check if ES seems to be running already
ES_RUNNING=false
if curl -sk "https://localhost:9200/" &>/dev/null 2>&1; then
    ES_RUNNING=true
    echo -e "${GREEN}Elasticsearch appears to already be running at https://localhost:9200.${NC}"
fi

if [ "$ES_RUNNING" = "false" ]; then
    if prompt_yn "Install Elasticsearch natively on this machine?" "y"; then
        if [ "$EUID" -ne 0 ]; then
            echo ""
            echo "Elasticsearch installation requires root privileges."
            echo "Re-running installer step with sudo..."
            sudo bash "${SCRIPT_DIR}/setup/install-elasticsearch.sh"
        else
            bash "${SCRIPT_DIR}/setup/install-elasticsearch.sh"
        fi
    else
        echo ""
        echo "Skipping Elasticsearch installation."
        echo "If you skip this step, ES credentials must already be available."
    fi
fi

# =============================================================================
# Step 3: Install OpenBao (optional)
# =============================================================================
step 3 "OpenBao (Optional)"

if command -v openbao >/dev/null 2>&1; then
    echo -e "${GREEN}OpenBao already installed:${NC} $(openbao version 2>/dev/null | head -n 1 || echo 'openbao')"
else
    echo "OpenBao is optional, but recommended if you want to use secrets management."
    if prompt_yn "Install OpenBao CLI on this machine?" "n"; then
        if [ "$EUID" -ne 0 ]; then
            echo ""
            echo "OpenBao installation requires root privileges."
            echo "Re-running installer step with sudo..."
            sudo bash "${SCRIPT_DIR}/setup/install-openbao.sh"
        else
            bash "${SCRIPT_DIR}/setup/install-openbao.sh"
        fi
    else
        echo "Skipping OpenBao installation."
    fi
fi

# =============================================================================
# Step 4: Configure .env
# =============================================================================
step 4 "Configure .env"

ENV_FILE="${SCRIPT_DIR}/.env"
TEMPLATE_FILE="${SCRIPT_DIR}/.env.template"

if [ -f "$ENV_FILE" ]; then
    echo -e "${YELLOW}.env already exists at ${ENV_FILE}.${NC}"
    if ! prompt_yn "Overwrite it?" "n"; then
        echo "Keeping existing .env."
    else
        cp "$TEMPLATE_FILE" "$ENV_FILE"
        echo "Copied .env.template → .env"
    fi
else
    cp "$TEMPLATE_FILE" "$ENV_FILE"
    echo "Copied .env.template → .env"
fi

echo ""
echo "Applying default bootstrap configuration..."
echo ""

# Auto-apply ES credentials from bootstrap output when available.
BOOTSTRAP_FILE="${SCRIPT_DIR}/.es-bootstrap.env"
if [ -f "${BOOTSTRAP_FILE}" ]; then
    BOOTSTRAP_ES_URL="$(grep -E '^ES_URL=' "${BOOTSTRAP_FILE}" | cut -d= -f2- || true)"
    BOOTSTRAP_ES_API_KEY="$(grep -E '^ES_API_KEY=' "${BOOTSTRAP_FILE}" | cut -d= -f2- || true)"
    BOOTSTRAP_ES_VERIFY_TLS="$(grep -E '^ES_VERIFY_TLS=' "${BOOTSTRAP_FILE}" | cut -d= -f2- || true)"

    if [ -n "${BOOTSTRAP_ES_URL}" ]; then replace_env_value "ES_URL" "${BOOTSTRAP_ES_URL}"; fi
    if [ -n "${BOOTSTRAP_ES_API_KEY}" ]; then replace_env_value "ES_API_KEY" "${BOOTSTRAP_ES_API_KEY}"; fi
    if [ -n "${BOOTSTRAP_ES_VERIFY_TLS}" ]; then replace_env_value "ES_VERIFY_TLS" "${BOOTSTRAP_ES_VERIFY_TLS}"; fi

    echo -e "  ${GREEN}Applied Elasticsearch credentials from ${BOOTSTRAP_FILE}.${NC}"
else
    echo -e "  ${YELLOW}No ${BOOTSTRAP_FILE} found. Leaving ES credentials as-is in .env.${NC}"
fi

# ES_URL
CURRENT_ES_URL=$(grep -E '^ES_URL=' "$ENV_FILE" | cut -d= -f2-)
ES_URL=$(prompt_value "ES_URL" "${CURRENT_ES_URL:-https://localhost:9200}")
replace_env_value "ES_URL" "${ES_URL}"

# LLM_PROVIDER
echo ""
echo "Select your LLM provider:"
echo "  1) ollama           (local, no API key needed)"
echo "  2) openai           (OpenAI API)"
echo "  3) openai_compatible (Groq, Together, Mistral, Azure, etc.)"
echo "  4) anthropic        (Claude API)"
echo "  5) gemini           (Google Gemini API)"
echo "  6) openai_oauth     (OpenAI OAuth via refresh token)"
echo ""
read -r -p "$(echo -e "${YELLOW}?${NC} Choose provider [1-6, default 1]: ")" PROVIDER_CHOICE
case "${PROVIDER_CHOICE:-1}" in
    2) LLM_PROVIDER="openai" ;;
    3) LLM_PROVIDER="openai_compatible" ;;
    4) LLM_PROVIDER="anthropic" ;;
    5) LLM_PROVIDER="gemini" ;;
    6) LLM_PROVIDER="openai_oauth" ;;
    *) LLM_PROVIDER="ollama" ;;
esac
if [ "$LLM_PROVIDER" = "openai_oauth" ]; then
    replace_env_value "LLM_PROVIDER" "openai"
    echo -e "  ${GREEN}LLM_PROVIDER=openai (OAuth mode)${NC}"
else
    replace_env_value "LLM_PROVIDER" "${LLM_PROVIDER}"
    echo -e "  ${GREEN}LLM_PROVIDER=${LLM_PROVIDER}${NC}"
fi

# Provider-specific settings
if [ "$LLM_PROVIDER" = "ollama" ]; then
    CURRENT_BASE=$(grep -E '^LLM_BASE_URL=' "$ENV_FILE" | cut -d= -f2-)
    LLM_BASE_URL=$(prompt_value "Ollama base URL" "${CURRENT_BASE:-http://localhost:11434}")
    replace_env_value "LLM_BASE_URL" "${LLM_BASE_URL}"
    LLM_MODEL=$(prompt_value "Ollama model name" "llama3.2")
    replace_env_value "LLM_MODEL" "${LLM_MODEL}"
    replace_env_value "LLM_API_KEY" ""

elif [ "$LLM_PROVIDER" = "openai_compatible" ]; then
    LLM_BASE_URL=$(prompt_value "Provider base URL (e.g. https://api.groq.com/openai)")
    replace_env_value "LLM_BASE_URL" "${LLM_BASE_URL}"
    replace_env_value "LLM_API_KEY" ""
    echo -e "  ${YELLOW}LLM_API_KEY left blank. Add it later from Settings/OpenBao.${NC}"
    LLM_MODEL=$(prompt_value "Model name")
    replace_env_value "LLM_MODEL" "${LLM_MODEL}"

elif [ "$LLM_PROVIDER" = "openai" ]; then
    replace_env_value "LLM_API_KEY" ""
    echo -e "  ${YELLOW}LLM_API_KEY left blank. Add it later from Settings/OpenBao.${NC}"
    LLM_MODEL=$(prompt_value "Model name" "gpt-4o")
    replace_env_value "LLM_MODEL" "${LLM_MODEL}"

elif [ "$LLM_PROVIDER" = "openai_oauth" ]; then
    replace_env_value "LLM_API_KEY" ""
    replace_env_value "OPENAI_OAUTH_STATE_FILE" "${SCRIPT_DIR}/.openai-oauth.json"
    replace_env_value "OPENAI_OAUTH_TOKEN_URL" "https://auth.openai.com/oauth/token"
    LLM_MODEL=$(prompt_value "Model name" "gpt-4o")
    replace_env_value "LLM_MODEL" "${LLM_MODEL}"
    echo ""
    echo "OpenAI OAuth selected. After install, run:"
    echo "  bash setup/openai-oauth.sh bootstrap"
    echo "This imports/refreshes tokens and updates .env."

elif [ "$LLM_PROVIDER" = "anthropic" ]; then
    replace_env_value "LLM_API_KEY" ""
    echo -e "  ${YELLOW}LLM_API_KEY left blank. Add it later from Settings/OpenBao.${NC}"
    LLM_MODEL=$(prompt_value "Model name" "claude-opus-4-5")
    replace_env_value "LLM_MODEL" "${LLM_MODEL}"

elif [ "$LLM_PROVIDER" = "gemini" ]; then
    replace_env_value "LLM_API_KEY" ""
    echo -e "  ${YELLOW}LLM_API_KEY left blank. Add it later from Settings/OpenBao.${NC}"
    LLM_MODEL=$(prompt_value "Model name" "gemini-2.0-flash")
    replace_env_value "LLM_MODEL" "${LLM_MODEL}"
fi

# Git identity
echo ""
GIT_USER_NAME=$(prompt_value "Git user name for agent commits" "Flume Agent")
GIT_USER_EMAIL=$(prompt_value "Git user email for agent commits" "agent@flume.local")
replace_env_value "GIT_USER_NAME" "${GIT_USER_NAME}"
replace_env_value "GIT_USER_EMAIL" "${GIT_USER_EMAIL}"

# EXECUTION_HOST
HOSTNAME_DEFAULT=$(hostname -s 2>/dev/null || echo "localhost")
EXECUTION_HOST=$(prompt_value "Execution host name (identifies this machine)" "$HOSTNAME_DEFAULT")
replace_env_value "EXECUTION_HOST" "${EXECUTION_HOST}"

# Dashboard port
DASHBOARD_PORT=$(prompt_value "Dashboard port" "8765")
replace_env_value "DASHBOARD_PORT" "${DASHBOARD_PORT}"

echo ""
echo -e "${GREEN}.env configured at ${ENV_FILE}${NC}"

# =============================================================================
# Step 5: Create Elasticsearch indices
# =============================================================================
step 5 "Create Elasticsearch Indices"

echo "This will create the 6 required indices in your Elasticsearch instance."
echo ""

if prompt_yn "Create Elasticsearch indices now?" "y"; then
    bash "${SCRIPT_DIR}/setup/create-es-indices.sh" || {
        echo ""
        echo -e "${YELLOW}Index creation encountered errors. You can re-run it manually:${NC}"
        echo "  bash ${SCRIPT_DIR}/setup/create-es-indices.sh"
    }
fi

# =============================================================================
# Step 6: Set up workspace directories
# =============================================================================
step 6 "Set Up Workspace"

echo "Creating required directories and initializing clean state files..."
echo ""

WORKSPACE="${SCRIPT_DIR}"

# =============================================================================
# Scrub any accidentally-included user project repositories
# =============================================================================
# Some build pipelines may accidentally bundle cloned git repos into the
# installation directory. Before running the system, remove any top-level
# git repositories that are not part of Flume's own code layout.
echo "Scrubbing bundled project repositories (best-effort)..."
for d in "${WORKSPACE}"/*; do
    [ -d "${d}" ] || continue
    base="$(basename "${d}")"

    case "${base}" in
        agents|dashboard|frontend|memory|setup|worker-manager|plan-sessions) continue ;;
        .env|.env.template|README.md|projects.json|sequence_counters.json) continue ;;
        install.sh) continue ;;
        *) : ;;
    esac

    if [ -d "${d}/.git" ]; then
        rm -rf "${d}"
    fi
done

mkdir -p "${WORKSPACE}/plan-sessions"
mkdir -p "${WORKSPACE}/worker-manager"

# Clean initial state files (if not already present)
if [ ! -f "${WORKSPACE}/projects.json" ]; then
    echo '{"projects": []}' > "${WORKSPACE}/projects.json"
    echo -e "  ${GREEN}Created projects.json${NC}"
fi

if [ ! -f "${WORKSPACE}/sequence_counters.json" ]; then
    echo '{}' > "${WORKSPACE}/sequence_counters.json"
    echo -e "  ${GREEN}Created sequence_counters.json${NC}"
fi

if [ ! -f "${WORKSPACE}/worker-manager/state.json" ]; then
    echo '{"workers": []}' > "${WORKSPACE}/worker-manager/state.json"
    echo -e "  ${GREEN}Created worker-manager/state.json${NC}"
fi

# Apply git identity from .env
source "${ENV_FILE}" 2>/dev/null || true
if [ -n "${GIT_USER_NAME:-}" ] && [ -n "${GIT_USER_EMAIL:-}" ]; then
    git config --global user.name "${GIT_USER_NAME}" 2>/dev/null && \
        echo -e "  ${GREEN}Set git user.name = ${GIT_USER_NAME}${NC}" || true
    git config --global user.email "${GIT_USER_EMAIL}" 2>/dev/null && \
        echo -e "  ${GREEN}Set git user.email = ${GIT_USER_EMAIL}${NC}" || true
fi

# Make scripts executable
chmod +x "${WORKSPACE}/dashboard/run.sh" 2>/dev/null || true
chmod +x "${WORKSPACE}/worker-manager/run.sh" 2>/dev/null || true

echo ""
echo -e "${GREEN}Workspace ready.${NC}"

# =============================================================================
# Step 7: Final instructions
# =============================================================================
step 7 "Done!"

DASHBOARD_PORT_VAL=$(grep -E '^DASHBOARD_PORT=' "$ENV_FILE" | cut -d= -f2-)
DASHBOARD_HOST_VAL=$(grep -E '^DASHBOARD_HOST=' "$ENV_FILE" | cut -d= -f2-)

DISPLAY_HOST="${DASHBOARD_HOST_VAL:-0.0.0.0}"
if [ "$DISPLAY_HOST" = "0.0.0.0" ]; then
    DISPLAY_HOST=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
fi

echo -e "${GREEN}${BOLD}Flume is ready to run!${NC}"
echo ""
echo -e "${BOLD}Start the dashboard:${NC}"
echo "  cd ${SCRIPT_DIR}"
echo "  bash dashboard/run.sh"
echo ""
echo -e "${BOLD}Then open in your browser:${NC}"
echo "  http://${DISPLAY_HOST}:${DASHBOARD_PORT_VAL:-8765}"
echo ""
echo -e "${BOLD}Start the agent workers (in a separate terminal):${NC}"
echo "  cd ${SCRIPT_DIR}"
echo "  bash worker-manager/run.sh"
echo ""
echo -e "${BOLD}Or start both from the dashboard:${NC}"
echo "  Use the 'Start Workers' button on the Workers page."
echo ""
echo "For full documentation see: README.md"
echo ""
