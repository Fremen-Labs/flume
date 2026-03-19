#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"
if [[ $# -lt 1 ]]; then
  echo "usage: worker_loop.sh <agent_role> [execution_host]" >&2
  exit 1
fi
role="$1"
execution_host="${2:-}"
"${SCRIPT_DIR}/compute_ready.sh" >/dev/null
if [[ -n "$execution_host" ]]; then
  "${SCRIPT_DIR}/dispatcher_claim_next.sh" "$role" "$execution_host"
else
  "${SCRIPT_DIR}/dispatcher_claim_next.sh" "$role"
fi
