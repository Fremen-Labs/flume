#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 6 ]]; then
  echo "usage: reviewer_run.sh <task_id> <project> <repo> <verdict> <summary> <next_role> [promotion_candidate]" >&2
  exit 1
fi

task_id="$1"
project="$2"
repo="$3"
verdict="$4"
summary="$5"
next_role="$6"
promotion_candidate="${7:-false}"

"${SCRIPT_DIR}/review_bootstrap.sh" "$task_id" "$project" "$repo"
"${SCRIPT_DIR}/review_task.sh" "$task_id" "$verdict" "$summary" "$next_role" "$promotion_candidate"

echo "Reviewer wrapper completed for $task_id"
