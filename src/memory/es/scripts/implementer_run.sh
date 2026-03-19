#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 4 ]]; then
  echo "usage: implementer_run.sh <task_id> <project> <repo> <bootstrap_query> [decision_title] [decision_statement]" >&2
  exit 1
fi

task_id="$1"
project="$2"
repo="$3"
query="$4"
decision_title="${5:-}"
decision_statement="${6:-}"

"${SCRIPT_DIR}/task_bootstrap.sh" "$task_id" "$project" "$repo"
"${SCRIPT_DIR}/bootstrap_memory.sh" "$query" "$project" "$repo"
"${SCRIPT_DIR}/task_update_by_key.sh" "$task_id" running implementer false normal

if [[ -n "$decision_title" ]]; then
  statement="${decision_statement:-$decision_title}"
  "${SCRIPT_DIR}/write_decision.sh" project "$project" "$repo" "$decision_title" "$statement"
fi

echo "Implementer wrapper completed for $task_id"
