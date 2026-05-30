#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# backfill_orphaned_tasks.sh
#
# One-time migration script to fix tasks orphaned by the pre-fix retry loop.
# Finds tasks stuck in "ready" or "review" with error messages containing
# "404" or "unknown-role" and moves them to "blocked" with an explanation.
#
# Usage:
#   ./scripts/backfill_orphaned_tasks.sh [ES_URL]
#
# Arguments:
#   ES_URL  Elasticsearch URL (default: https://localhost:9200)
#
# Safety:
#   - Runs as _update_by_query so it's idempotent (safe to run multiple times)
#   - Only affects tasks matching the specific error patterns
#   - Uses "blocked" status so they appear in the dashboard for manual review
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

ES_URL="${1:-https://localhost:9200}"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "=== Flume Post-Migration Task Backfill ==="
echo "ES URL: ${ES_URL}"
echo "Timestamp: ${NOW}"
echo ""

# 1. Find and block tasks stuck with 404 errors
echo ">>> Phase 1: Blocking tasks stuck with 404 errors..."
RESULT=$(curl -sk -X POST "${ES_URL}/agent-task-records/_update_by_query?conflicts=proceed" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": {
      "bool": {
        "must": [
          { "terms": { "status": ["ready", "review", "running"] } }
        ],
        "should": [
          { "match_phrase": { "error_message": "404" } },
          { "match_phrase": { "error_message": "page not found" } }
        ],
        "minimum_should_match": 1
      }
    },
    "script": {
      "source": "ctx._source.status = \"blocked\"; ctx._source.error_message = \"Backfilled: task was stuck in retry loop due to gateway 404 (pre-fix). Manual review recommended.\"; ctx._source.active_worker = null; ctx._source.queue_state = \"available\"; ctx._source.updated_at = params.now",
      "params": { "now": "'"${NOW}"'" }
    }
  }' 2>/dev/null)

UPDATED=$(echo "${RESULT}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('updated', 0))" 2>/dev/null || echo "?")
echo "    Updated: ${UPDATED} tasks"

# 2. Find and block tasks with "unknown-role" in reasoning
echo ">>> Phase 2: Blocking tasks with unknown-role errors..."
RESULT2=$(curl -sk -X POST "${ES_URL}/agent-task-records/_update_by_query?conflicts=proceed" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": {
      "bool": {
        "must": [
          { "terms": { "status": ["ready", "review", "running"] } },
          { "match_phrase": { "error_message": "unknown-role" } }
        ]
      }
    },
    "script": {
      "source": "ctx._source.status = \"blocked\"; ctx._source.error_message = \"Backfilled: task was stuck in retry loop due to unknown-role crash (pre-fix). Manual review recommended.\"; ctx._source.active_worker = null; ctx._source.queue_state = \"available\"; ctx._source.updated_at = params.now",
      "params": { "now": "'"${NOW}"'" }
    }
  }' 2>/dev/null)

UPDATED2=$(echo "${RESULT2}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('updated', 0))" 2>/dev/null || echo "?")
echo "    Updated: ${UPDATED2} tasks"

echo ""
echo "=== Backfill complete ==="
echo "Total tasks blocked: $((${UPDATED:-0} + ${UPDATED2:-0}))"
echo ""
echo "Next steps:"
echo "  1. Review blocked tasks in the Flume dashboard"
echo "  2. Archive tasks that are no longer needed"
echo "  3. Unblock tasks that should be retried with the fixed gateway"
