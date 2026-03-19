#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 5 ]]; then
  echo "usage: planner_run.sh <task_id> <title> <objective> <project> <repo> [priority] [risk] [handoff_to_role]" >&2
  exit 1
fi

task_id="$1"
title="$2"
objective="$3"
project="$4"
repo="$5"
priority="${6:-normal}"
risk="${7:-medium}"
handoff_to="${8:-implementer}"

"${SCRIPT_DIR}/task_create.sh" "$task_id" "$title" "$objective" "$repo" planner "$priority" "$risk"
"${SCRIPT_DIR}/bootstrap_memory.sh" "$objective" "$project" "$repo"
"${SCRIPT_DIR}/write_handoff.sh" "$task_id" planner "$handoff_to" "plan created" "$objective" ready
"${SCRIPT_DIR}/task_update_by_key.sh" "$task_id" ready "$handoff_to" false "$priority"

echo "Planner wrapper completed for $task_id"
