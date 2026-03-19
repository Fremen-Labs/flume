#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 6 ]]; then
  echo "usage: write_handoff.sh <task_id> <from_role> <to_role> <reason> <objective> <status_hint>" >&2
  exit 1
fi

task_id="$1"
from_role="$2"
to_role="$3"
reason="$4"
objective="$5"
status_hint="$6"

python3 - <<PY | "${SCRIPT_DIR}/write_handoff.py"
import json
print(json.dumps({
  "task_id": ${task_id@Q},
  "from_role": ${from_role@Q},
  "to_role": ${to_role@Q},
  "reason": ${reason@Q},
  "objective": ${objective@Q},
  "inputs": [],
  "constraints": [],
  "status_hint": ${status_hint@Q}
}))
PY
