#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 4 ]]; then
  echo "usage: review_task.sh <task_id> <verdict> <summary> <recommended_next_role> [promotion_candidate]" >&2
  exit 1
fi

task_id="$1"
verdict="$2"
summary="$3"
next_role="$4"
promotion_candidate="${5:-false}"
review_id="review-${task_id}-$(date +%Y%m%d-%H%M%S)"

"${SCRIPT_DIR}/write_review.sh" "$review_id" "$task_id" "$verdict" "$summary" "$next_role" "$promotion_candidate" high

case "$verdict" in
  approved)
    "${SCRIPT_DIR}/task_update_by_key.sh" "$task_id" done reviewer false normal
    ;;
  changes_requested)
    "${SCRIPT_DIR}/write_handoff.sh" "$task_id" reviewer implementer "$summary" "$summary" running
    "${SCRIPT_DIR}/task_update_by_key.sh" "$task_id" running implementer false normal
    ;;
  blocked)
    "${SCRIPT_DIR}/task_update_by_key.sh" "$task_id" blocked reviewer true high
    ;;
  *)
    echo "Unknown verdict: $verdict" >&2
    exit 1
    ;;
esac

echo "Review flow applied for $task_id with verdict=$verdict"
