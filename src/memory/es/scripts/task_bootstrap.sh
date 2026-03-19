#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 3 ]]; then
  echo "usage: task_bootstrap.sh <task_query> <project> <repo>" >&2
  exit 1
fi

task_query="$1"
project="$2"
repo="$3"

echo '=== TASK SEARCH ==='
"${SCRIPT_DIR}/task_search.sh" "$task_query"
echo
echo '=== MEMORY BOOTSTRAP ==='
"${SCRIPT_DIR}/bootstrap_memory.sh" "$task_query" "$project" "$repo"
