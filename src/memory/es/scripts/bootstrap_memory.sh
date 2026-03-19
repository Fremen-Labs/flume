#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

if [[ $# -lt 1 ]]; then
  echo "usage: bootstrap_memory.sh <query> [project] [repo]" >&2
  exit 1
fi

query="$1"
project="${2:-openclaw}"
repo="${3:-workspace}"

printf '=== CANONICAL MEMORY HINTS ===\n'
for f in \
  ${LOOM_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}/MEMORY.md \
  ${LOOM_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}/USER.md \
  ${LOOM_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}/HYBRID_MEMORY_SYSTEM.md; do
  if [[ -f "$f" ]]; then
    echo "--- $(basename "$f") ---"
    sed -n '1,80p' "$f"
    echo
  fi
done

printf '=== OPERATIONAL MEMORY ===\n'
"${SCRIPT_DIR}/retrieve_context.sh" "$query" "$project" "$repo"
