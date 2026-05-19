#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
# LogLoom Utility Benchmark — Real-World Engineering Questions
# ════════════════════════════════════════════════════════════════
# Simulates 8 debugging questions an engineer might ask an LLM
# about the Flume gateway, measuring the precision and speed
# of LogLoom-enriched ES queries vs manual code grep.

ES="http://localhost:9205"
IDX="flume-logloom-enrichment"
TOTAL_QUESTIONS=0
TOTAL_NODES_RETURNED=0
TOTAL_QUERY_MS=0

echo "════════════════════════════════════════════════════════════════"
echo "  LogLoom Utility Benchmark: Real-World Engineering Questions"
echo "  Cluster: local-elastro-brain | Index: $IDX"
echo "════════════════════════════════════════════════════════════════"
echo ""

# ── Q1: "The health checker is marking nodes offline incorrectly. ──────
#         What log messages and functions are involved in the circuit
#         breaker state machine?"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Q1: Circuit breaker state machine — what logs fire when a node"
echo "    transitions through closed → open → half-open → closed?"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
((TOTAL_QUESTIONS++))
START=$(python3 -c "import time; print(int(time.time()*1000))")
Q1=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' -d '{
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"wildcard": {"logloom.file": "*health_checker*"}},
        {"bool": {"should": [
          {"wildcard": {"logloom.message_template": "*circuit*"}},
          {"wildcard": {"logloom.message_template": "*offline*"}},
          {"wildcard": {"logloom.message_template": "*half-open*"}},
          {"wildcard": {"logloom.message_template": "*recovered*"}}
        ], "minimum_should_match": 1}}
      ]
    }
  },
  "_source": ["logloom.function", "logloom.file", "logloom.line", "logloom.message_template", "logloom.tags"],
  "sort": [{"logloom.line": "asc"}]
}')
END=$(python3 -c "import time; print(int(time.time()*1000))")
MS=$((END - START))
TOTAL_QUERY_MS=$((TOTAL_QUERY_MS + MS))
COUNT=$(echo "$Q1" | jq '.hits.total.value')
TOTAL_NODES_RETURNED=$((TOTAL_NODES_RETURNED + COUNT))
echo "  ⏱️  ${MS}ms | ${COUNT} log sites found"
echo "$Q1" | jq -r '.hits.hits[]._source.logloom | "  📍 \(.file):\(.line) \(.function)() → \"\(.message_template)\""'
echo ""

# ── Q2: "Requests are timing out when the ensemble is active. ──────────
#         What is the full blast radius of ExecuteEnsemble?"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Q2: What functions does ExecuteEnsemble call? (blast radius)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
((TOTAL_QUESTIONS++))
START=$(python3 -c "import time; print(int(time.time()*1000))")
# Get ExecuteEnsemble's call_children
Q2_IDS=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' -d '{
  "size": 1,
  "query": {"term": {"logloom.function": "Server.ExecuteEnsemble"}},
  "_source": ["logloom.call_children"]
}' | jq -r '.hits.hits[0]._source.logloom.call_children // []')
# Resolve children to named functions
Q2=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' -d "{
  \"size\": 30,
  \"query\": {\"terms\": {\"logloom.node_id\": $Q2_IDS}},
  \"_source\": [\"logloom.function\", \"logloom.file\", \"logloom.line\", \"logloom.message_template\"]
}")
END=$(python3 -c "import time; print(int(time.time()*1000))")
MS=$((END - START))
TOTAL_QUERY_MS=$((TOTAL_QUERY_MS + MS))
COUNT=$(echo "$Q2" | jq '.hits.total.value')
TOTAL_NODES_RETURNED=$((TOTAL_NODES_RETURNED + COUNT))
echo "  ⏱️  ${MS}ms | ${COUNT} downstream log sites in blast radius"
echo "$Q2" | jq -r '.hits.hits[]._source.logloom | "  📍 \(.file):\(.line) \(.function)() → \"\(.message_template)\""' | head -15
echo "  ... ($(echo "$Q2" | jq '.hits.total.value') total)"
echo ""

# ── Q3: "API key resolution is failing for Anthropic. ──────────────────
#         What is the full fallback chain and where does each step log?"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Q3: API key resolution fallback chain — show all log sites"
echo "    in resolveAPIKey and its callers"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
((TOTAL_QUESTIONS++))
START=$(python3 -c "import time; print(int(time.time()*1000))")
Q3=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' -d '{
  "size": 20,
  "query": {
    "bool": {
      "should": [
        {"term": {"logloom.function": "ProviderRouter.resolveAPIKey"}},
        {"bool": {"must": [
          {"wildcard": {"logloom.message_template": "*api*key*"}},
          {"wildcard": {"logloom.file": "*providers*"}}
        ]}}
      ],
      "minimum_should_match": 1
    }
  },
  "_source": ["logloom.function", "logloom.file", "logloom.line", "logloom.message_template", "logloom.tags"],
  "sort": [{"logloom.line": "asc"}]
}')
END=$(python3 -c "import time; print(int(time.time()*1000))")
MS=$((END - START))
TOTAL_QUERY_MS=$((TOTAL_QUERY_MS + MS))
COUNT=$(echo "$Q3" | jq '.hits.total.value')
TOTAL_NODES_RETURNED=$((TOTAL_NODES_RETURNED + COUNT))
echo "  ⏱️  ${MS}ms | ${COUNT} log sites found"
echo "$Q3" | jq -r '.hits.hits[]._source.logloom | "  📍 \(.file):\(.line) \(.function)() → \"\(.message_template)\" [\(.tags | join(", "))]"'
echo ""

# ── Q4: "The frontier spend budget isn't stopping requests. ────────────
#         Show me all error/warning logs in the spend enforcement path."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Q4: Spend enforcement — all error/warning logs in routing_policy"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
((TOTAL_QUESTIONS++))
START=$(python3 -c "import time; print(int(time.time()*1000))")
Q4=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' -d '{
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"wildcard": {"logloom.file": "*routing_policy*"}},
        {"terms": {"logloom.tags": ["error", "warning"]}}
      ]
    }
  },
  "_source": ["logloom.function", "logloom.file", "logloom.line", "logloom.message_template", "logloom.tags"],
  "sort": [{"logloom.line": "asc"}]
}')
END=$(python3 -c "import time; print(int(time.time()*1000))")
MS=$((END - START))
TOTAL_QUERY_MS=$((TOTAL_QUERY_MS + MS))
COUNT=$(echo "$Q4" | jq '.hits.total.value')
TOTAL_NODES_RETURNED=$((TOTAL_NODES_RETURNED + COUNT))
echo "  ⏱️  ${MS}ms | ${COUNT} log sites found"
echo "$Q4" | jq -r '.hits.hits[]._source.logloom | "  📍 \(.file):\(.line) \(.function)() → \"\(.message_template)\""'
echo ""

# ── Q5: "Node registration is silently failing. ────────────────────────
#         What functions does handleAddNode call, and which ones can
#         emit errors?"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Q5: handleAddNode blast radius — what can fail during"
echo "    node registration?"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
((TOTAL_QUESTIONS++))
START=$(python3 -c "import time; print(int(time.time()*1000))")
# Get handleAddNode children + its own log sites
Q5_SELF=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' -d '{
  "size": 20,
  "query": {"term": {"logloom.function": "Server.handleAddNode"}},
  "_source": ["logloom.function", "logloom.file", "logloom.line", "logloom.message_template", "logloom.tags", "logloom.call_children"]
}')
# Extract call_children IDs
Q5_IDS=$(echo "$Q5_SELF" | jq '[.hits.hits[]._source.logloom.call_children // [] | .[] ] | unique')
# Resolve children
Q5_CHILDREN=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' -d "{
  \"size\": 30,
  \"query\": {\"terms\": {\"logloom.node_id\": $Q5_IDS}},
  \"_source\": [\"logloom.function\", \"logloom.file\", \"logloom.line\", \"logloom.message_template\", \"logloom.tags\"]
}")
END=$(python3 -c "import time; print(int(time.time()*1000))")
MS=$((END - START))
TOTAL_QUERY_MS=$((TOTAL_QUERY_MS + MS))
SELF_COUNT=$(echo "$Q5_SELF" | jq '.hits.total.value')
CHILD_COUNT=$(echo "$Q5_CHILDREN" | jq '.hits.total.value')
TOTAL_NODES_RETURNED=$((TOTAL_NODES_RETURNED + SELF_COUNT + CHILD_COUNT))
echo "  ⏱️  ${MS}ms | ${SELF_COUNT} direct + ${CHILD_COUNT} downstream log sites"
echo "  ── Direct log sites in handleAddNode ──"
echo "$Q5_SELF" | jq -r '.hits.hits[]._source.logloom | "  📍 \(.file):\(.line) → \"\(.message_template)\""'
echo "  ── Downstream callees ──"
echo "$Q5_CHILDREN" | jq -r '.hits.hits[]._source.logloom | "  📍 \(.file):\(.line) \(.function)() → \"\(.message_template)\""' | head -10
echo ""

# ── Q6: "The gateway is returning 503 during startup. ─────────────────
#         What log messages fire during the graceful shutdown path?"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Q6: Graceful shutdown — what logs fire during SIGTERM/SIGINT?"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
((TOTAL_QUESTIONS++))
START=$(python3 -c "import time; print(int(time.time()*1000))")
Q6=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' -d '{
  "size": 20,
  "query": {
    "bool": {
      "must": [{"wildcard": {"logloom.file": "*server*"}}],
      "should": [
        {"wildcard": {"logloom.message_template": "*shutdown*"}},
        {"wildcard": {"logloom.message_template": "*drain*"}},
        {"wildcard": {"logloom.message_template": "*signal*"}},
        {"wildcard": {"logloom.message_template": "*readyz*"}},
        {"wildcard": {"logloom.message_template": "*shutting_down*"}}
      ],
      "minimum_should_match": 1
    }
  },
  "_source": ["logloom.function", "logloom.file", "logloom.line", "logloom.message_template", "logloom.tags"],
  "sort": [{"logloom.line": "asc"}]
}')
END=$(python3 -c "import time; print(int(time.time()*1000))")
MS=$((END - START))
TOTAL_QUERY_MS=$((TOTAL_QUERY_MS + MS))
COUNT=$(echo "$Q6" | jq '.hits.total.value')
TOTAL_NODES_RETURNED=$((TOTAL_NODES_RETURNED + COUNT))
echo "  ⏱️  ${MS}ms | ${COUNT} log sites found"
echo "$Q6" | jq -r '.hits.hits[]._source.logloom | "  📍 \(.file):\(.line) \(.function)() → \"\(.message_template)\""'
echo ""

# ── Q7: "Vault initialization is hanging during first boot. ───────────
#         What is the complete lifecycle boot sequence for OpenBao?"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Q7: OpenBao vault lifecycle — complete boot sequence"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
((TOTAL_QUESTIONS++))
START=$(python3 -c "import time; print(int(time.time()*1000))")
Q7=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' -d '{
  "size": 30,
  "query": {
    "bool": {
      "must": [{"wildcard": {"logloom.file": "*vault*"}}],
      "filter": [{"terms": {"logloom.tags": ["lifecycle"]}}]
    }
  },
  "_source": ["logloom.function", "logloom.file", "logloom.line", "logloom.message_template", "logloom.tags"],
  "sort": [{"logloom.line": "asc"}]
}')
END=$(python3 -c "import time; print(int(time.time()*1000))")
MS=$((END - START))
TOTAL_QUERY_MS=$((TOTAL_QUERY_MS + MS))
COUNT=$(echo "$Q7" | jq '.hits.total.value')
TOTAL_NODES_RETURNED=$((TOTAL_NODES_RETURNED + COUNT))
echo "  ⏱️  ${MS}ms | ${COUNT} lifecycle log sites found"
echo "$Q7" | jq -r '.hits.hits[]._source.logloom | "  📍 \(.file):\(.line) \(.function)() → \"\(.message_template)\""'
echo ""

# ── Q8: "Multi-node routing is picking the wrong node. ─────────────────
#         What log sites trace the full routing decision path?"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Q8: Multi-node routing decision trace — full path from"
echo "    ExecuteSmartRoute to node selection"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
((TOTAL_QUESTIONS++))
START=$(python3 -c "import time; print(int(time.time()*1000))")
Q8=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' -d '{
  "size": 30,
  "query": {
    "bool": {
      "should": [
        {"wildcard": {"logloom.file": "*multi_node_router*"}},
        {"bool": {"must": [
          {"wildcard": {"logloom.file": "*node_registry*"}},
          {"bool": {"should": [
            {"wildcard": {"logloom.function": "*Select*"}},
            {"wildcard": {"logloom.function": "*Route*"}}
          ]}}
        ]}}
      ],
      "minimum_should_match": 1
    }
  },
  "_source": ["logloom.function", "logloom.file", "logloom.line", "logloom.message_template", "logloom.tags"],
  "sort": [{"logloom.file": "asc"}, {"logloom.line": "asc"}]
}')
END=$(python3 -c "import time; print(int(time.time()*1000))")
MS=$((END - START))
TOTAL_QUERY_MS=$((TOTAL_QUERY_MS + MS))
COUNT=$(echo "$Q8" | jq '.hits.total.value')
TOTAL_NODES_RETURNED=$((TOTAL_NODES_RETURNED + COUNT))
echo "  ⏱️  ${MS}ms | ${COUNT} log sites in routing decision path"
echo "$Q8" | jq -r '.hits.hits[]._source.logloom | "  📍 \(.file):\(.line) \(.function)() → \"\(.message_template)\""'
echo ""

# ════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════
AVG_MS=$((TOTAL_QUERY_MS / TOTAL_QUESTIONS))
echo "════════════════════════════════════════════════════════════════"
echo "  BENCHMARK SUMMARY"
echo "════════════════════════════════════════════════════════════════"
echo "  Questions asked:        $TOTAL_QUESTIONS"
echo "  Total log sites found:  $TOTAL_NODES_RETURNED"
echo "  Total query time:       ${TOTAL_QUERY_MS}ms"
echo "  Average query time:     ${AVG_MS}ms"
echo "════════════════════════════════════════════════════════════════"
