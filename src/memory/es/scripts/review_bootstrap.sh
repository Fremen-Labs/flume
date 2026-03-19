#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 3 ]]; then
  echo "usage: review_bootstrap.sh <task_id_or_query> <project> <repo>" >&2
  exit 1
fi

query="$1"
project="$2"
repo="$3"

echo '=== TASK CONTEXT ==='
"${SCRIPT_DIR}/task_search.sh" "$query"
echo
echo '=== MEMORY CONTEXT ==='
"${SCRIPT_DIR}/retrieve_context.sh" "$query" "$project" "$repo"
