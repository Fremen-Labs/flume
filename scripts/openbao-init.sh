#!/bin/sh
# Initialize and unseal the file-backed Vault/OpenBao used by Flume Core.
set -eu
export VAULT_ADDR="${VAULT_ADDR:-http://openbao:8200}"
INIT_DIR="${INIT_DIR:-/init}"
mkdir -p "$INIT_DIR"

flat_json() {
  tr -d '\n\r\t ' < "$1"
}

extract() {
  # usage: extract field file
  flat_json "$2" | sed -n "s/.*\"$1\":\"\\([^\"]*\\)\".*/\\1/p" | head -1
}

extract_key() {
  flat_json "$1" | sed -n 's/.*"unseal_keys_b64":\["\([^"]*\)".*/\1/p' | head -1
}

i=0
while [ "$i" -lt 60 ]; do
  if vault status >/dev/null 2>&1; then
    break
  fi
  # sealed (2) or not initialized still means the HTTP listener is up
  vault status >/tmp/vault-status 2>&1 || true
  if grep -q 'Initialized' /tmp/vault-status 2>/dev/null; then
    break
  fi
  i=$((i + 1))
  sleep 2
done

if vault status >/dev/null 2>&1; then
  if [ -f "$INIT_DIR/keys.json" ]; then
    extract root_token "$INIT_DIR/keys.json" > "$INIT_DIR/root_token"
  fi
  VAULT_TOKEN="$(cat "$INIT_DIR/root_token" 2>/dev/null || true)"
  export VAULT_TOKEN
  vault secrets enable -version=2 -path=secret kv >/dev/null 2>&1 || true
  echo "openbao already unsealed"
  exit 0
fi

if [ -f "$INIT_DIR/keys.json" ]; then
  KEY="$(extract_key "$INIT_DIR/keys.json")"
  if [ -z "$KEY" ]; then
    echo "failed to parse unseal key from $INIT_DIR/keys.json" >&2
    cat "$INIT_DIR/keys.json" >&2
    exit 1
  fi
  vault operator unseal "$KEY" >/dev/null
  extract root_token "$INIT_DIR/keys.json" > "$INIT_DIR/root_token"
  VAULT_TOKEN="$(cat "$INIT_DIR/root_token")"
  export VAULT_TOKEN
  vault secrets enable -version=2 -path=secret kv >/dev/null 2>&1 || true
  echo "openbao unsealed from saved keys"
  exit 0
fi

vault operator init -key-shares=1 -key-threshold=1 -format=json > "$INIT_DIR/keys.json"
KEY="$(extract_key "$INIT_DIR/keys.json")"
TOKEN="$(extract root_token "$INIT_DIR/keys.json")"
if [ -z "$KEY" ] || [ -z "$TOKEN" ]; then
  echo "failed to parse init output" >&2
  cat "$INIT_DIR/keys.json" >&2
  exit 1
fi
vault operator unseal "$KEY" >/dev/null
echo "$TOKEN" > "$INIT_DIR/root_token"
chmod 644 "$INIT_DIR/root_token" "$INIT_DIR/keys.json"
export VAULT_TOKEN="$TOKEN"
vault secrets enable -version=2 -path=secret kv >/dev/null 2>&1 || true
echo "openbao initialized"
exit 0
