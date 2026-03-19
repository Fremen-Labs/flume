#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 5 ]]; then
  echo "usage: write_provenance.sh <id> <task_id> <project> <repo> <agent_role> [review_verdict]" >&2
  exit 1
fi

id="$1"
task_id="$2"
project="$3"
repo="$4"
agent_role="$5"
review_verdict="${6:-pending}"

python3 - <<PY | "${SCRIPT_DIR}/write_provenance.py"
import json
print(json.dumps({
  "id": ${id@Q},
  "task_id": ${task_id@Q},
  "project": ${project@Q},
  "repo": ${repo@Q},
  "agent_role": ${agent_role@Q},
  "context_refs": [],
  "tool_calls": {},
  "artifacts": [],
  "review_verdict": ${review_verdict@Q}
}))
PY
