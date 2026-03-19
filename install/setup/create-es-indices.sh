#!/usr/bin/env bash
# Flume — Create Elasticsearch indices
#
# Creates all 6 indices required by Flume using the index templates
# bundled in the memory/es/index_templates/ directory.
#
# Prerequisites:
#   - Elasticsearch is running and reachable at ES_URL
#   - .env file exists in the Flume workspace root (parent of this script's directory)
#   - ES_API_KEY is set with sufficient permissions (or run as elastic superuser)
#
# Usage:
#   ./create-es-indices.sh              # loads .env automatically
#   ES_URL=... ES_API_KEY=... ./create-es-indices.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Support both package layout (memory at root) and git clone (memory under src/)
if [ -n "${ENV_FILE:-}" ] && [ -f "${ENV_FILE}" ]; then
    WORKSPACE_ROOT="$(cd "$(dirname "${ENV_FILE}")" && pwd)"
else
    WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi
if [ -d "${WORKSPACE_ROOT}/memory/es/index_templates" ]; then
    TEMPLATES_DIR="${WORKSPACE_ROOT}/memory/es/index_templates"
elif [ -d "${WORKSPACE_ROOT}/src/memory/es/index_templates" ]; then
    TEMPLATES_DIR="${WORKSPACE_ROOT}/src/memory/es/index_templates"
else
    TEMPLATES_DIR="${WORKSPACE_ROOT}/memory/es/index_templates"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "  \033[0;36m[INFO]\033[0m  $*"; }
success() { echo -e "  ${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "  ${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "  ${RED}[ERROR]\033[0m $*"; exit 1; }

# Load .env if not already set
if [ -z "${ES_API_KEY:-}" ]; then
    if [ -n "${ENV_FILE:-}" ] && [ -f "${ENV_FILE}" ]; then
        set -a
        source "${ENV_FILE}"
        set +a
    elif [ -f "${WORKSPACE_ROOT}/.env" ]; then
        set -a
        source "${WORKSPACE_ROOT}/.env"
        set +a
    fi
fi

# Validate required vars
: "${ES_URL:?ES_URL is not set. Set it in .env or pass it as an environment variable.}"
: "${ES_API_KEY:?ES_API_KEY is not set. Set it in .env or pass it as an environment variable.}"

ES_VERIFY_TLS="${ES_VERIFY_TLS:-false}"
CURL_TLS_OPT=""
if [ "${ES_VERIFY_TLS}" = "false" ]; then
    CURL_TLS_OPT="-k"
fi

echo ""
echo "========================================"
echo "  Flume — Create Elasticsearch Indices"
echo "========================================"
echo "  ES_URL: ${ES_URL}"
echo ""

# Test connectivity
info "Testing Elasticsearch connectivity..."
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" ${CURL_TLS_OPT} \
    -H "Authorization: ApiKey ${ES_API_KEY}" \
    "${ES_URL}/")

if [ "$HTTP_STATUS" != "200" ]; then
    error "Cannot reach Elasticsearch at ${ES_URL} (HTTP ${HTTP_STATUS}). Check ES_URL and ES_API_KEY."
fi
success "Elasticsearch is reachable."
echo ""

create_index() {
    local INDEX_NAME="$1"
    local TEMPLATE_FILE="$2"

    if [ ! -f "${TEMPLATE_FILE}" ]; then
        warn "Template not found: ${TEMPLATE_FILE} — skipping ${INDEX_NAME}"
        return
    fi

    info "Creating index: ${INDEX_NAME}..."

    # Check if index already exists
    EXIST_STATUS=$(curl -s -o /dev/null -w "%{http_code}" ${CURL_TLS_OPT} \
        -H "Authorization: ApiKey ${ES_API_KEY}" \
        "${ES_URL}/${INDEX_NAME}")

    if [ "$EXIST_STATUS" = "200" ]; then
        warn "${INDEX_NAME} already exists — skipping."
        return
    fi

    RESPONSE=$(curl -s -o /tmp/es_create_response.json -w "%{http_code}" \
        ${CURL_TLS_OPT} \
        -X PUT "${ES_URL}/${INDEX_NAME}" \
        -H "Content-Type: application/json" \
        -H "Authorization: ApiKey ${ES_API_KEY}" \
        -d "@${TEMPLATE_FILE}")

    if [ "$RESPONSE" = "200" ] || [ "$RESPONSE" = "201" ]; then
        success "${INDEX_NAME} created."
    else
        echo -e "  ${RED}[FAIL]${NC}  ${INDEX_NAME} — HTTP ${RESPONSE}"
        cat /tmp/es_create_response.json 2>/dev/null && echo ""
    fi
}

# Create all 6 indices
create_index "agent-task-records"      "${TEMPLATES_DIR}/task_records.json"
create_index "agent-handoff-records"   "${TEMPLATES_DIR}/handoff_records.json"
create_index "agent-failure-records"   "${TEMPLATES_DIR}/failure_records.json"
create_index "agent-provenance-records" "${TEMPLATES_DIR}/provenance_records.json"
create_index "agent-memory-entries"    "${TEMPLATES_DIR}/memory_entries.json"
create_index "agent-review-records"    "${TEMPLATES_DIR}/review_records.json"

echo ""
echo -e "${GREEN}Index creation complete.${NC}"
echo ""
echo "Verify with:"
echo "  curl ${CURL_TLS_OPT} -H 'Authorization: ApiKey \${ES_API_KEY}' '${ES_URL}/_cat/indices?v'"
echo ""
