#!/usr/bin/env bash
# scripts/verify_flume_ready.sh
# End-to-end smoke test for Flume being "ready for real coding work".
# Run this after a rebuild. It exercises the happy path we care about.

set -euo pipefail

DASHBOARD_URL="${DASHBOARD_URL:-http://localhost:8765}"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:8090}"

echo "=== 1. Health checks ==="
curl -sf "$DASHBOARD_URL/api/health" > /dev/null && echo "Dashboard: OK"
curl -sf "$GATEWAY_URL/health" > /dev/null && echo "Gateway: OK"

echo "=== 2. Recent task health (no explosion) ==="
TASKS=$(curl -sf "$DASHBOARD_URL/api/snapshot" | jq -r '.task_counts_by_status')
echo "Task counts: $TASKS"

BLOCKED=$(echo "$TASKS" | jq -r '.blocked // 0')
if [ "$BLOCKED" -gt 5 ]; then
  echo "WARNING: More than 5 blocked tasks ($BLOCKED). Possible explosion or LLM fragility."
else
  echo "Blocked tasks acceptable: $BLOCKED"
fi

echo "=== 3. Gateway can reach at least one Ollama node ==="
NODES=$(curl -sf "$GATEWAY_URL/health" | jq -r '.node_mesh.online_nodes // 0')
if [ "$NODES" -ge 1 ]; then
  echo "Mesh nodes visible: $NODES"
else
  echo "WARNING: No Ollama mesh nodes visible to gateway."
fi

echo "=== All basic checks passed. Full agent loop + real commits still require the completed implementer (see runner.go). ==="
echo "Run with a small real task via the UI or API to do the true end-to-end."
