#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
execution_host="${1:-rocky-vm}"
"${SCRIPT_DIR}/worker_loop.sh" tester "$execution_host"
