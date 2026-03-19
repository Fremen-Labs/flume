#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 5 ]]; then
  echo "usage: write_failure.sh <task_id> <project> <repo> <error_class> <summary> [root_cause] [fix_applied]" >&2
  exit 1
fi

task_id="$1"
project="$2"
repo="$3"
error_class="$4"
summary="$5"
root_cause="${6:-}"
fix_applied="${7:-}"

python3 - <<PY
import json, os, ssl, urllib.request
from datetime import datetime, timezone
payload = {
  "id": f"failure-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
  "task_id": ${task_id@Q},
  "project": ${project@Q},
  "repo": ${repo@Q},
  "error_class": ${error_class@Q},
  "summary": ${summary@Q},
  "root_cause": ${root_cause@Q},
  "fix_applied": ${fix_applied@Q},
  "confidence": "medium",
  "recurrence_count": 1,
  "created_at": datetime.now(timezone.utc).isoformat(),
  "updated_at": datetime.now(timezone.utc).isoformat(),
}
ctx=None
if os.environ.get("ES_VERIFY_TLS","false").lower() != "true":
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
req=urllib.request.Request(
    f"{os.environ.get('ES_URL','https://localhost:9200').rstrip('/')}/{os.environ.get('ES_INDEX_FAILURES','agent-failure-records')}/_doc",
    data=json.dumps(payload).encode(),
    headers={"Content-Type":"application/json","Authorization":f"ApiKey {os.environ['ES_API_KEY']}"},
    method="POST",
)
with urllib.request.urlopen(req, context=ctx) as r:
    print(r.read().decode())
PY
