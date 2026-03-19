#!/usr/bin/env bash
set -euo pipefail
${LOOM_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}/memory/es/scripts/bootstrap_memory.sh "multi agent memory" openclaw workspace | sed -n '1,240p'
