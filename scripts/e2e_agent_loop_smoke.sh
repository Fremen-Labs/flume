#!/usr/bin/env bash
# scripts/e2e_agent_loop_smoke.sh
# Automated E2E smoke for the full agent loop:
# Intake small task -> PM decomp (visible reasoning + node) -> Implementer does work + commit -> Reviewer -> Done
#
# Assumes the cluster is running (flume start) and you have a small test repo onboarded.

set -euo pipefail

DASHBOARD="http://localhost:8765"
PROJECT_ID="${PROJECT_ID:-rflow}"   # Change to your test project

echo "=== E2E Agent Loop Smoke Test ==="

# 1. Trigger a small intake / plan (or use existing planned task)
echo "Creating a tiny test task via API (simulated intake)..."
TASK_PAYLOAD='{
  "title": "E2E Smoke: Add a hello world comment",
  "objective": "Add a simple comment at the top of main.go (or equivalent entrypoint) saying \"E2E smoke test was here\".",
  "repo": "'"$PROJECT_ID"'",
  "item_type": "task"
}'

# For a real test you would call the intake API. Here we just ensure the system is responsive.
curl -sf "$DASHBOARD/api/health" > /dev/null

echo "System is responsive. In a full run you would now:"
echo "  - POST to /api/intake/session/.../commit to create planned work"
echo "  - Watch PM workers via logs or /api/tasks for decomp with node telemetry"
echo "  - Watch implementer produce edits + commit via streaming reasoning"
echo "  - Confirm reviewer passes and task reaches 'done'"

# Placeholder assertion - in real CI this would poll until task is done with commit
echo "E2E smoke skeleton complete. Full automation requires the completed implementer loop + reliable LLM calls."

echo "SUCCESS (skeleton)"
