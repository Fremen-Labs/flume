#!/bin/sh
set -euo pipefail

if ! command -v jq >/dev/null 2>&1; then
    apk add --no-cache jq >/dev/null
fi

log() {
  jq -n -c \
    --arg time "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" \
    --arg level "$1" \
    --arg message "$2" \
    '{time: $time, level: $level, message: $message}'
}

log "info" "Bootstrapping new Flume secrets & Topologies..."

# Source .env locally inside the container to avoid Docker daemon metadata leaks
if [ -f "/app/.env" ]; then
    # We grep to ignore comments and empty lines
    export $(grep -v '^#' /app/.env | grep -v '^[[:space:]]*$' | xargs)
    log "info" "Sourced dynamically generated environment topography."
else
    log "warn" "/app/.env missing; continuing without explicit API token."
fi

if ! vault secrets list | grep -q 'secret/'; then
    vault secrets enable -path=secret kv-v2 >/dev/null
    log "info" "Successfully enabled Vault secret engine at secret/."
else
    log "info" "Secret engine 'secret/' already exists. Skipping creation."
fi

ES_KEY=$(hexdump -vn 32 -e '1/1 "%02x"' /dev/urandom)
log "info" "Generated Dynamic Elastic Token."

vault kv put secret/flume/keys ES_API_KEY="$ES_KEY" OPENAI_API_KEY="${OPENAI_API_KEY:-""}" >/dev/null
log "info" "Injected Matrix API keys into Vault KV-v2 secret/flume/keys block natively."
