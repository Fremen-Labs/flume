#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"
if [[ $# -lt 5 ]]; then
  echo "usage: create_work_item.sh <id> <title> <type> <project> <repo> [parent_id] [status]" >&2
  exit 1
fi
id="$1"; title="$2"; type="$3"; project="$4"; repo="$5"; parent_id="${6:-null}"; status="${7:-inbox}"
parent_json=null
if [[ "$parent_id" != "null" && -n "$parent_id" ]]; then parent_json="\"$parent_id\""; fi
cat <<JSON | "${SCRIPT_DIR}/create_work_item.py"
{
  "id": "$id",
  "title": "$title",
  "work_item_type": "$type",
  "project": "$project",
  "repo": "$repo",
  "parent_id": $parent_json,
  "status": "$status",
  "priority": "normal",
  "acceptance_criteria": [],
  "artifacts": [],
  "needs_human": false,
  "risk": "medium"
}
JSON
