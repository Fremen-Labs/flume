#!/bin/sh
set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
    apk add --no-cache python3 >/dev/null
fi

exec python3 /app/bootstrap.py
