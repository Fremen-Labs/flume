#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 6 ]]; then
  echo "usage: promote_decision.sh <scope> <project> <repo> <title> <statement> <target_markdown_file>" >&2
  exit 1
fi

scope="$1"
project="$2"
repo="$3"
title="$4"
statement="$5"
target="$6"

tmp_json="$(mktemp)"
cat >"$tmp_json" <<JSON
{
  "scope": "$scope",
  "type": "decision",
  "title": "$title",
  "statement": "$statement",
  "summary": "$statement",
  "project": "$project",
  "repo": "$repo",
  "confidence": "high",
  "source_ref": "manual-promotion"
}
JSON

"$(dirname "$0")/promote_memory.py" "$tmp_json" "$target"
rm -f "$tmp_json"
