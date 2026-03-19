#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ $# -lt 4 ]]; then
  echo "usage: start_intake_agent.sh <project> <repo> <root_title> <tree_json_file>" >&2
  exit 1
fi
project="$1"; repo="$2"; title="$3"; tree_json="$4"
"${SCRIPT_DIR}/generate_work_tree.py" "$project" "$repo" "$title" "$tree_json"
