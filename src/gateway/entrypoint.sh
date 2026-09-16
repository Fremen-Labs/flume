#!/bin/sh
set -eu
if [ -z "${OPENBAO_TOKEN:-}" ] && [ -f /openbao-init/root_token ]; then
  OPENBAO_TOKEN="$(cat /openbao-init/root_token)"
  export OPENBAO_TOKEN
fi
exec /gateway
