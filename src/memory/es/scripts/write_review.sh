#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 5 ]]; then
  echo "usage: write_review.sh <review_id> <task_id> <verdict> <summary> <recommended_next_role> [promotion_candidate] [confidence]" >&2
  exit 1
fi

review_id="$1"
task_id="$2"
verdict="$3"
summary="$4"
next_role="$5"
promotion_candidate="${6:-false}"
confidence="${7:-medium}"

payload_file="$(mktemp)"
cat >"$payload_file" <<JSON
{
  "review_id": "$review_id",
  "task_id": "$task_id",
  "verdict": "$verdict",
  "summary": "$summary",
  "issues": [],
  "recommended_next_role": "$next_role",
  "promotion_candidate": $promotion_candidate,
  "confidence": "$confidence"
}
JSON

"${SCRIPT_DIR}/write_review.py" < "$payload_file"
rm -f "$payload_file"
