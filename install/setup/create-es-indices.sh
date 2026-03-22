#!/usr/bin/env bash
# Flume — Create Elasticsearch indices
#
# Creates all 6 indices required by Flume using the index templates
# bundled in the memory/es/index_templates/ directory.
#
# Loads ES_URL / ES_API_KEY from .env and/or OpenBao (see hydrate-openbao-env.py).
# If the cluster returns HTTP 401, offers guided API-key creation (elastic password)
# unless FLUME_NON_INTERACTIVE=1 or stdin is not a TTY.
#
# Usage:
#   ./create-es-indices.sh
#   ES_URL=... ES_API_KEY=... ./create-es-indices.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${ENV_FILE:-}" ] && [ -f "${ENV_FILE}" ]; then
    WORKSPACE_ROOT="$(cd "$(dirname "${ENV_FILE}")" && pwd)"
elif [ -f "${SCRIPT_DIR}/../../pyproject.toml" ] && [ -d "${SCRIPT_DIR}/../../src" ]; then
    WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
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
        # shellcheck disable=SC1090
        source "${ENV_FILE}"
        set +a
    elif [ -f "${WORKSPACE_ROOT}/.env" ]; then
        set -a
        # shellcheck disable=SC1090
        source "${WORKSPACE_ROOT}/.env"
        set +a
    fi
fi

# OpenBao: hydrate ES_* when flume.config.json exists
if [ -z "${ES_API_KEY:-}" ] || [ "${ES_API_KEY}" = "AUTO_GENERATED_BY_INSTALLER" ]; then
    CFG_OK=false
    [ -f "${WORKSPACE_ROOT}/flume.config.json" ] && CFG_OK=true
    [ -f "${WORKSPACE_ROOT}/../flume.config.json" ] && CFG_OK=true
    if [ "$CFG_OK" = "true" ]; then
        export FLUME_WORKSPACE_ROOT="${WORKSPACE_ROOT}"
        if [ -d "${WORKSPACE_ROOT}/src" ]; then
            export PYTHONPATH="${WORKSPACE_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
        else
            export PYTHONPATH="${WORKSPACE_ROOT}${PYTHONPATH:+:$PYTHONPATH}"
        fi
        _hydrate_out=""
        if command -v uv >/dev/null 2>&1 && [ -f "${WORKSPACE_ROOT}/pyproject.toml" ]; then
            # shellcheck disable=SC1090
            _hydrate_out="$(cd "${WORKSPACE_ROOT}" && uv run python "${SCRIPT_DIR}/hydrate-openbao-env.py" 2>/dev/null)" || true
        elif command -v python3 >/dev/null 2>&1; then
            # shellcheck disable=SC1090
            _hydrate_out="$(python3 "${SCRIPT_DIR}/hydrate-openbao-env.py" 2>/dev/null)" || true
        fi
        if [ -n "${_hydrate_out}" ]; then
            # shellcheck disable=SC1090
            eval "${_hydrate_out}"
        fi
    fi
fi

_flume_trim_es_api_key() {
    ES_API_KEY="${ES_API_KEY:-}"
    ES_API_KEY="${ES_API_KEY#"${ES_API_KEY%%[![:space:]]*}"}"
    ES_API_KEY="${ES_API_KEY%"${ES_API_KEY##*[![:space:]]}"}"
    ES_API_KEY="${ES_API_KEY//$'\r'/}"
    ES_API_KEY="${ES_API_KEY//$'\n'/}"
    case "${ES_API_KEY}" in
        \"*\") ES_API_KEY="${ES_API_KEY#\"}"; ES_API_KEY="${ES_API_KEY%\"}" ;;
        \'*\') ES_API_KEY="${ES_API_KEY#\'}"; ES_API_KEY="${ES_API_KEY%\'}" ;;
    esac
}

_flume_reload_local_env() {
    if [ -f "${WORKSPACE_ROOT}/.env" ]; then
        set -a
        # shellcheck disable=SC1090
        source "${WORKSPACE_ROOT}/.env"
        set +a
    fi
}

_flume_rehydrate_es_from_openbao() {
    CFG_OK=false
    [ -f "${WORKSPACE_ROOT}/flume.config.json" ] && CFG_OK=true
    [ -f "${WORKSPACE_ROOT}/../flume.config.json" ] && CFG_OK=true
    [ "$CFG_OK" = "false" ] && return 0
    export FLUME_WORKSPACE_ROOT="${WORKSPACE_ROOT}"
    if [ -d "${WORKSPACE_ROOT}/src" ]; then
        export PYTHONPATH="${WORKSPACE_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
    else
        export PYTHONPATH="${WORKSPACE_ROOT}${PYTHONPATH:+:$PYTHONPATH}"
    fi
    _h=""
    if command -v uv >/dev/null 2>&1 && [ -f "${WORKSPACE_ROOT}/pyproject.toml" ]; then
        # shellcheck disable=SC1090
        _h="$(cd "${WORKSPACE_ROOT}" && uv run python "${SCRIPT_DIR}/hydrate-openbao-env.py" 2>/dev/null)" || true
    elif command -v python3 >/dev/null 2>&1; then
        # shellcheck disable=SC1090
        _h="$(python3 "${SCRIPT_DIR}/hydrate-openbao-env.py" 2>/dev/null)" || true
    fi
    if [ -n "${_h}" ]; then
        # shellcheck disable=SC1090
        eval "${_h}"
    fi
}

# API key + ES_URL written by install-elasticsearch.sh / provisioning (overrides stale OpenBao during first install).
_flume_source_es_bootstrap_file() {
    [ "${FLUME_IGNORE_ES_BOOTSTRAP_FILE:-}" = "1" ] && return 0
    local bf="${WORKSPACE_ROOT}/install/.es-bootstrap.env"
    [ -f "$bf" ] || return 0
    set -a
    # shellcheck disable=SC1090
    source "$bf"
    set +a
    info "Loaded install/.es-bootstrap.env (credentials from Elasticsearch provisioning)."
}

_flume_source_es_bootstrap_file

: "${ES_URL:?ES_URL is not set. Set it in OpenBao KV, .env, install/.es-bootstrap.env, or pass as an environment variable.}"

_flume_trim_es_api_key

ES_VERIFY_TLS="${ES_VERIFY_TLS:-false}"
CURL_TLS_OPT=""
if [ "${ES_VERIFY_TLS}" = "false" ]; then
    CURL_TLS_OPT="-k"
fi

# HTTP status for GET / on ES (ApiKey header only if ES_API_KEY is non-empty)
_es_http_code() {
    local url="$1"
    local out st
    set +e
    if [ -n "${ES_API_KEY:-}" ]; then
        out=$(curl -s -o /dev/null -w "%{http_code}" ${CURL_TLS_OPT} \
            -H "Authorization: ApiKey ${ES_API_KEY}" \
            --connect-timeout 3 --max-time 15 \
            "${url}/" 2>/dev/null)
    else
        out=$(curl -s -o /dev/null -w "%{http_code}" ${CURL_TLS_OPT} \
            --connect-timeout 3 --max-time 15 \
            "${url}/" 2>/dev/null)
    fi
    st=$?
    set -e
    if [ "$st" -ne 0 ] || [ -z "${out}" ]; then
        echo "000"
        return 0
    fi
    echo "${out}"
    return 0
}

_es_cluster_responded() {
    local url="$1"
    local c
    c="$(_es_http_code "${url}")"
    case "${c}" in
        200|201|401|403) return 0 ;;
    esac
    set +e
    c=$(curl -s -o /dev/null -w "%{http_code}" ${CURL_TLS_OPT} \
        --connect-timeout 3 --max-time 15 "${url}/" 2>/dev/null)
    set -e
    case "${c}" in
        200|401|403) return 0 ;;
    esac
    return 1
}

unset FLUME_PERSIST_ES_URL FLUME_PERSIST_ES_URL_WAS 2>/dev/null || true
if [ -n "${ES_URL:-}" ] && echo "${ES_URL}" | grep -q 'elasticsearch'; then
    ES_URL_BEFORE_REMAP="${ES_URL}"
    primary_code="$(_es_http_code "${ES_URL}")"
    if [ "${primary_code}" != "200" ]; then
        host_port="${FLUME_ES_HOST_PORT:-9201}"
        remapped=""
        for cand in \
            "https://localhost:9200" \
            "https://127.0.0.1:9200" \
            "http://localhost:9200" \
            "http://127.0.0.1:9200" \
            "http://127.0.0.1:${host_port}" \
            "http://localhost:${host_port}"; do
            if _es_cluster_responded "${cand}"; then
                warn "ES_URL was ${ES_URL} — not reachable from the host (Docker-only hostname)."
                info "Using ${cand} for this run; the installer will save this URL to OpenBao and/or .env after indices succeed."
                ES_URL="${cand}"
                export ES_URL
                export FLUME_PERSIST_ES_URL_WAS="${ES_URL_BEFORE_REMAP}"
                export FLUME_PERSIST_ES_URL="${ES_URL}"
                remapped=1
                break
            fi
        done
        if [ -z "${remapped}" ]; then
            warn "Could not auto-detect a host ES URL. Set ES_URL in OpenBao (or .env) to your real cluster URL."
        fi
    fi
fi

echo ""
echo "========================================"
echo "  Flume — Create Elasticsearch Indices"
echo "========================================"
echo "  ES_URL: ${ES_URL}"
echo ""

ES_USE_API_KEY=1
HTTP_STATUS="000"
BOOTSTRAP_OFFERED=0

for iteration in 1 2; do
    _flume_reload_local_env
    _flume_rehydrate_es_from_openbao
    _flume_source_es_bootstrap_file
    _flume_trim_es_api_key

    info "Testing Elasticsearch connectivity..."
    if [ -n "${ES_API_KEY:-}" ]; then
        HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" ${CURL_TLS_OPT} \
            -H "Authorization: ApiKey ${ES_API_KEY}" \
            "${ES_URL}/")
    else
        HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" ${CURL_TLS_OPT} \
            "${ES_URL}/")
    fi

    if [ "$HTTP_STATUS" = "200" ]; then
        if [ -z "${ES_API_KEY:-}" ]; then
            ES_USE_API_KEY=0
            info "Continuing without an API key (this Elasticsearch node allows unauthenticated access)."
        else
            ES_USE_API_KEY=1
        fi
        success "Elasticsearch is reachable."
        echo ""
        break
    fi

    if [ "$HTTP_STATUS" = "000" ] || [ -z "${HTTP_STATUS}" ]; then
        error "Cannot connect to Elasticsearch at ${ES_URL}. Check the URL and TLS (ES_VERIFY_TLS). Then run: ./flume es-indices"
    fi

    if [ "$iteration" = "1" ] && [ "$BOOTSTRAP_OFFERED" = "0" ]; then
        BOOTSTRAP_OFFERED=1
        if { [ "$HTTP_STATUS" = "401" ] || [ "$HTTP_STATUS" = "403" ]; } && [ "${FLUME_SKIP_ES_KEY_BOOTSTRAP:-}" != "1" ]; then
            export FLUME_WORKSPACE_ROOT="${WORKSPACE_ROOT}"
            export ES_URL
            export ES_VERIFY_TLS="${ES_VERIFY_TLS:-false}"
            if command -v uv >/dev/null 2>&1 && [ -f "${WORKSPACE_ROOT}/pyproject.toml" ]; then
                if (cd "${WORKSPACE_ROOT}" && uv run python "${SCRIPT_DIR}/es_bootstrap_api_key.py"); then
                    continue
                fi
            elif command -v python3 >/dev/null 2>&1; then
                export PYTHONPATH="${WORKSPACE_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
                if (cd "${WORKSPACE_ROOT}" && python3 "${SCRIPT_DIR}/es_bootstrap_api_key.py"); then
                    continue
                fi
            fi
        fi
    fi

    if [ "$HTTP_STATUS" = "401" ] || [ "$HTTP_STATUS" = "403" ]; then
        echo -e "  ${RED}[ERROR]${NC} Elasticsearch at ${ES_URL} returned HTTP ${HTTP_STATUS} (not authorized)."
        echo ""
        echo "  The URL is reachable, but Flume could not authenticate."
        echo "  For unattended installs, provisioning should supply one of:"
        echo "    • install/.es-bootstrap.env (API key from install-elasticsearch.sh), or"
        echo "    • ELASTIC_PASSWORD or FLUME_ELASTIC_PASSWORD_FILE, or install/.elastic-admin.env"
        echo "  Then run: ./flume es-indices"
        exit 1
    fi
    error "Cannot reach Elasticsearch at ${ES_URL} (HTTP ${HTTP_STATUS}). Run: ./flume es-indices"
done

if [ "$HTTP_STATUS" != "200" ]; then
    error "Could not confirm Elasticsearch connectivity. Run: ./flume es-indices"
fi

create_index() {
    local INDEX_NAME="$1"
    local TEMPLATE_FILE="$2"

    if [ ! -f "${TEMPLATE_FILE}" ]; then
        warn "Template not found: ${TEMPLATE_FILE} — skipping ${INDEX_NAME}"
        return
    fi

    info "Creating index: ${INDEX_NAME}..."

    local AUTH_HDR=()
    if [ "${ES_USE_API_KEY}" = "1" ] && [ -n "${ES_API_KEY:-}" ]; then
        AUTH_HDR=(-H "Authorization: ApiKey ${ES_API_KEY}")
    fi

    local EXIST_STATUS
    EXIST_STATUS=$(curl -s -o /dev/null -w "%{http_code}" ${CURL_TLS_OPT} \
        "${AUTH_HDR[@]}" \
        "${ES_URL}/${INDEX_NAME}")

    if [ "${EXIST_STATUS}" = "200" ]; then
        warn "${INDEX_NAME} already exists — skipping."
        return
    fi

    local RESPONSE
    RESPONSE=$(curl -s -o /tmp/es_create_response.json -w "%{http_code}" \
        ${CURL_TLS_OPT} \
        -X PUT "${ES_URL}/_template/${INDEX_NAME}" \
        -H "Content-Type: application/json" \
        "${AUTH_HDR[@]}" \
        -d "@${TEMPLATE_FILE}")

    if [ "$RESPONSE" = "200" ] || [ "$RESPONSE" = "201" ]; then
        success "${INDEX_NAME} template created."
        curl -s -o /dev/null ${CURL_TLS_OPT} "${AUTH_HDR[@]}" -X PUT "${ES_URL}/${INDEX_NAME}"
    else
        echo -e "  ${RED}[FAIL]${NC}  ${INDEX_NAME} — HTTP ${RESPONSE}"
        cat /tmp/es_create_response.json 2>/dev/null && echo ""
    fi
}

create_index "agent-task-records"      "${TEMPLATES_DIR}/task_records.json"
create_index "agent-handoff-records"   "${TEMPLATES_DIR}/handoff_records.json"
create_index "agent-failure-records"   "${TEMPLATES_DIR}/failure_records.json"
create_index "agent-provenance-records" "${TEMPLATES_DIR}/provenance_records.json"
create_index "agent-memory-entries"    "${TEMPLATES_DIR}/memory_entries.json"
create_index "agent-review-records"    "${TEMPLATES_DIR}/agent-review-records.json"

echo ""
echo -e "${GREEN}Index creation complete.${NC}"
echo ""

if [ -n "${FLUME_PERSIST_ES_URL:-}" ] && [ -n "${FLUME_PERSIST_ES_URL_WAS:-}" ]; then
    export FLUME_WORKSPACE_ROOT="${WORKSPACE_ROOT}"
    if [ -d "${WORKSPACE_ROOT}/src" ]; then
        export PYTHONPATH="${WORKSPACE_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
    fi
    if command -v uv >/dev/null 2>&1 && [ -f "${WORKSPACE_ROOT}/pyproject.toml" ]; then
        (cd "${WORKSPACE_ROOT}" && uv run python "${SCRIPT_DIR}/persist-host-es-url.py") || true
    elif command -v python3 >/dev/null 2>&1; then
        python3 "${SCRIPT_DIR}/persist-host-es-url.py" || true
    fi
fi

echo "Verify with (from Flume root):"
if [ "${ES_USE_API_KEY}" = "1" ] && [ -n "${ES_API_KEY:-}" ]; then
    echo "  source .env && curl ${CURL_TLS_OPT} -H \"Authorization: ApiKey \$ES_API_KEY\" \"${ES_URL}/_cat/indices?v\""
else
    echo "  curl ${CURL_TLS_OPT} \"${ES_URL}/_cat/indices?v\""
fi
echo ""
