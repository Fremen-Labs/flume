#!/bin/sh
set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
    apk add --no-cache python3 >/dev/null
fi

echo "Waiting for OpenBao listener to boot natively..."
while true; do
    set +e
    vault status -address=http://openbao:8200 >/dev/null 2>&1
    STATUS=$?
    set -e
    
    # vault status returns 2 if initialized but sealed. It returns 1 if connection refused.
    # We just need it to accept connections (exit code 2 or 0)
    if [ $STATUS -eq 2 ] || [ $STATUS -eq 0 ]; then
        break
    fi
    sleep 1
done

exec python3 /app/bootstrap.py
