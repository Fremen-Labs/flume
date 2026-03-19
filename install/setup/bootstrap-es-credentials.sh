#!/usr/bin/env bash
# Flume — Generate ES API key and update .env
#
# Use this when Elasticsearch is already running but .env has no valid ES_API_KEY.
# You need the 'elastic' superuser password (from ES install or reset).
#
# Usage:
#   ELASTIC_PASSWORD=yourpassword bash install/setup/bootstrap-es-credentials.sh
#   # or run interactively (will prompt for password)
#   bash install/setup/bootstrap-es-credentials.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# Repo root: install/.. for git clone
if [ -d "${INSTALL_DIR}/../src" ]; then
    REPO_ROOT="$(cd "${INSTALL_DIR}/.." && pwd)"
else
    REPO_ROOT="${INSTALL_DIR}"
fi
ENV_FILE="${REPO_ROOT}/.env"
BOOTSTRAP_FILE="${INSTALL_DIR}/.es-bootstrap.env"
ES_URL="${ES_URL:-https://localhost:9200}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo ""
echo "Flume — Elasticsearch credentials bootstrap"
echo "==========================================="
echo ""

if [ -z "${ELASTIC_PASSWORD:-}" ]; then
    echo -e "${YELLOW}Enter the 'elastic' superuser password for Elasticsearch.${NC}"
    echo "(If you don't have it, reset it with: sudo /usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -i)"
    echo ""
    read -r -s ELASTIC_PASSWORD
    echo ""
    if [ -z "${ELASTIC_PASSWORD}" ]; then
        echo -e "${RED}Password cannot be empty.${NC}"
        exit 1
    fi
fi

echo "Generating Flume API key..."
API_KEY_RESPONSE=$(curl -sk -u "elastic:${ELASTIC_PASSWORD}" \
    -X POST "${ES_URL}/_security/api_key" \
    -H "Content-Type: application/json" \
    -d '{"name":"flume","role_descriptors":{}}' 2>/dev/null || true)

API_KEY_ENCODED=$(echo "$API_KEY_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('encoded',''))" 2>/dev/null || echo "")

if [ -z "$API_KEY_ENCODED" ]; then
    echo -e "${RED}Failed to generate API key. Check:${NC}"
    echo "  1. Elasticsearch is running: curl -sk ${ES_URL}/"
    echo "  2. The elastic password is correct"
    echo ""
    echo "To reset the elastic password:"
    echo "  sudo /usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -i"
    exit 1
fi

echo -e "${GREEN}API key generated.${NC}"

# Ensure .env exists
if [ ! -f "${ENV_FILE}" ]; then
    if [ -f "${INSTALL_DIR}/.env.template" ]; then
        cp "${INSTALL_DIR}/.env.template" "${ENV_FILE}"
        echo "Created .env from template at ${ENV_FILE}"
    else
        echo -e "${RED}.env not found at ${ENV_FILE}. Create it first.${NC}"
        exit 1
    fi
fi

# Update .env (safely handle special chars in value)
replace_val() {
    local key="$1"
    local val="$2"
    local tmp
    tmp=$(mktemp)
    local found=0
    while IFS= read -r line; do
        if [[ "$line" == "$key"=* ]]; then
            echo "${key}=${val}"
            found=1
        else
            echo "$line"
        fi
    done < "${ENV_FILE}" > "${tmp}"
    if [ "$found" -eq 0 ]; then
        echo "${key}=${val}" >> "${tmp}"
    fi
    mv "${tmp}" "${ENV_FILE}"
}

replace_val "ES_URL" "${ES_URL}"
replace_val "ES_API_KEY" "${API_KEY_ENCODED}"
replace_val "ES_VERIFY_TLS" "false"

# Also write bootstrap for install.sh
{
    echo "ES_URL=${ES_URL}"
    echo "ES_API_KEY=${API_KEY_ENCODED}"
    echo "ES_VERIFY_TLS=false"
    echo "ES_BOOTSTRAP_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${BOOTSTRAP_FILE}"
chmod 600 "${BOOTSTRAP_FILE}" 2>/dev/null || true

echo ""
echo -e "${GREEN}Credentials written to ${ENV_FILE}${NC}"
echo ""
echo "You can now run:"
echo "  bash src/dashboard/run.sh"
echo "  bash src/worker-manager/run.sh"
echo ""
