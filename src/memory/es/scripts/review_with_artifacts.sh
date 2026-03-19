#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 3 ]]; then
  echo "usage: review_with_artifacts.sh <task_id> <project> <repo>" >&2
  exit 1
fi

task_id="$1"
project="$2"
repo="$3"

echo '=== REVIEW TASK LOOKUP ==='
"${SCRIPT_DIR}/task_search.sh" "$task_id"
echo
echo '=== REVIEW MEMORY LOOKUP ==='
"${SCRIPT_DIR}/retrieve_context.sh" "$task_id" "$project" "$repo"
echo
echo '=== DASHBOARD SNAPSHOT ==='
"${SCRIPT_DIR}/status_dashboard.sh"
