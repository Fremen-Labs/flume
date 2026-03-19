#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load_env.sh"

echo "Creating Elasticsearch indices..."

# Create memory entries index
echo "Creating agent-memory-entries index..."
curl -s -X PUT "${ES_URL}/agent-memory-entries" \
  -H "Content-Type: application/json" \
  -H "Authorization: ApiKey ${ES_API_KEY}" \
  -d @${SCRIPT_DIR}/../index_templates/memory_entries.json

# Create task records index
echo "Creating agent-task-records index..."
curl -s -X PUT "${ES_URL}/agent-task-records" \
  -H "Content-Type: application/json" \
  -H "Authorization: ApiKey ${ES_API_KEY}" \
  -d @${SCRIPT_DIR}/../index_templates/task_records.json

# Create handoff records index
echo "Creating agent-handoff-records index..."
curl -s -X PUT "${ES_URL}/agent-handoff-records" \
  -H "Content-Type: application/json" \
  -H "Authorization: ApiKey ${ES_API_KEY}" \
  -d @${SCRIPT_DIR}/../index_templates/handoff_records.json

# Create failure records index
echo "Creating agent-failure-records index..."
curl -s -X PUT "${ES_URL}/agent-failure-records" \
  -H "Content-Type: application/json" \
  -H "Authorization: ApiKey ${ES_API_KEY}" \
  -d @${SCRIPT_DIR}/../index_templates/failure_records.json

# Create provenance records index
echo "Creating agent-provenance-records index..."
curl -s -X PUT "${ES_URL}/agent-provenance-records" \
  -H "Content-Type: application/json" \
  -H "Authorization: ApiKey ${ES_API_KEY}" \
  -d @${SCRIPT_DIR}/../index_templates/provenance_records.json

echo "All indices created successfully!"