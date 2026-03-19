#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"
if [[ $# -lt 1 ]]; then
  echo "usage: dispatcher_claim_next.sh <agent_role> [execution_host]" >&2
  exit 1
fi
role="$1"
execution_host="${2:-}"
json="$(${SCRIPT_DIR}/ready_work_items.py)"
item_id="$(python3 - <<'PY' "$json"
import json,sys
obj=json.loads(sys.argv[1])
hits=obj.get('hits',{}).get('hits',[])
print(hits[0]['_id'] if hits else '')
PY
)"
if [[ -z "$item_id" ]]; then
  echo "NO_READY_ITEMS"
  exit 0
fi
if [[ -n "$execution_host" ]]; then
  "${SCRIPT_DIR}/claim_work_item.sh" "$item_id" "$role" "$execution_host"
else
  "${SCRIPT_DIR}/claim_work_item.sh" "$item_id" "$role"
fi
echo "CLAIMED=$item_id"
