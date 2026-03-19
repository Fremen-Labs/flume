#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 2 ]]; then
  echo "usage: memory_updater_run.sh <search_query> <target_markdown_file> [project] [repo]" >&2
  exit 1
fi

query="$1"
target="$2"
project="${3:-openclaw}"
repo="${4:-workspace}"

"${SCRIPT_DIR}/retrieve_context.sh" "$query" "$project" "$repo"
"${SCRIPT_DIR}/promote_from_search.py" "$query" "$target"
"${SCRIPT_DIR}/write_decision.sh" system "$project" "$repo" "Memory promotion executed" "Operational memory entry was promoted into canonical memory."

echo "Memory updater wrapper completed for query: $query"
