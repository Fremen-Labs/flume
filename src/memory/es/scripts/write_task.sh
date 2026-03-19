#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 6 ]]; then
  echo "usage: write_task.sh <id> <title> <objective> <repo> <owner> <status> [priority]" >&2
  exit 1
fi

id="$1"
title="$2"
objective="$3"
repo="$4"
owner="$5"
status="$6"
priority="${7:-normal}"

python3 - <<PY | "${SCRIPT_DIR}/write_task.py"
import json
print(json.dumps({
  "id": ${id@Q},
  "title": ${title@Q},
  "objective": ${objective@Q},
  "repo": ${repo@Q},
  "owner": ${owner@Q},
  "status": ${status@Q},
  "priority": ${priority@Q},
  "acceptance_criteria": [],
  "artifacts": [],
  "needs_human": False,
  "risk": "medium"
}))
PY
