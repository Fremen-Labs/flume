#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 3 ]]; then
  echo "usage: task_update.sh <task_doc_id> <status> <owner> [needs_human] [priority]" >&2
  exit 1
fi

doc_id="$1"
status="$2"
owner="$3"
needs_human="${4:-false}"
priority="${5:-normal}"
frag="$(mktemp)"
cat >"$frag" <<JSON
{
  "status": "$status",
  "owner": "$owner",
  "needs_human": $needs_human,
  "priority": "$priority"
}
JSON
"${SCRIPT_DIR}/task_update.py" "$doc_id" "$frag"
rm -f "$frag"
