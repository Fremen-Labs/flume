#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 4 ]]; then
  echo "usage: write_decision.sh <scope> <project> <repo> <title> [statement]" >&2
  exit 1
fi

scope="$1"
project="$2"
repo="$3"
title="$4"
statement="${5:-$4}"
id="memory-$(date +%Y%m%d-%H%M%S)-decision"

python3 - <<PY | "${SCRIPT_DIR}/write_memory.py"
import json
print(json.dumps({
  "id": ${id@Q},
  "scope": ${scope@Q},
  "type": "decision",
  "title": ${title@Q},
  "statement": ${statement@Q},
  "summary": ${statement@Q},
  "project": ${project@Q},
  "repo": ${repo@Q},
  "tags": ["decision"],
  "confidence": "high",
  "active": True,
  "source_ref": "manual"
}))
PY
