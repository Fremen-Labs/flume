#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
# LogLoom × Flume — Real-World Engineering Tests
# ════════════════════════════════════════════════════════════════
# Target: flume-logloom-enrichment @ local-elastro-brain (localhost:9205)
# Date: $(date -u +"%Y-%m-%dT%H:%M:%SZ")

ES="http://localhost:9205"
IDX="flume-logloom-enrichment"
PASS=0
FAIL=0
TOTAL=0

passed() { ((PASS++)); ((TOTAL++)); echo "  ✅ PASS: $1"; }
failed() { ((FAIL++)); ((TOTAL++)); echo "  ❌ FAIL: $1 — $2"; }

echo "═══════════════════════════════════════════════════════════════"
echo "  LogLoom × Flume: Real-World Engineering Tests"
echo "  Cluster: local-elastro-brain | Index: $IDX"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ── Test Suite 1: Index Health ──────────────────────────────────
echo "── Suite 1: Index Health ──"

# T1.1: Index exists and is green
HEALTH=$(curl -s "$ES/_cat/indices/$IDX?h=health" | tr -d '[:space:]')
if [ "$HEALTH" = "green" ]; then passed "Index health is green"
else failed "Index health" "got: $HEALTH"; fi

# T1.2: Document count matches graph build
COUNT=$(curl -s "$ES/$IDX/_count" | jq '.count')
if [ "$COUNT" = "1720" ]; then passed "Document count matches graph (1720)"
else failed "Document count" "expected 1720, got $COUNT"; fi

# T1.3: No test file nodes present
TEST_COUNT=$(curl -s "$ES/$IDX/_count" -H 'Content-Type: application/json' \
  -d '{"query":{"wildcard":{"logloom.file":"*_test.go"}}}' | jq '.count')
if [ "$TEST_COUNT" = "0" ]; then passed "No test file nodes (0)"
else failed "Test file exclusion" "expected 0, got $TEST_COUNT"; fi

echo ""

# ── Test Suite 2: Data Quality ──────────────────────────────────
echo "── Suite 2: Data Quality ──"

# T2.1: Go nodes = 377
GO_COUNT=$(curl -s "$ES/$IDX/_count" -H 'Content-Type: application/json' \
  -d '{"query":{"wildcard":{"logloom.file":"*.go"}}}' | jq '.count')
if [ "$GO_COUNT" = "377" ]; then passed "Go node count (377)"
else failed "Go node count" "expected 377, got $GO_COUNT"; fi

# T2.2: Python nodes = 1343
PY_COUNT=$(curl -s "$ES/$IDX/_count" -H 'Content-Type: application/json' \
  -d '{"query":{"wildcard":{"logloom.file":"*.py"}}}' | jq '.count')
if [ "$PY_COUNT" = "1343" ]; then passed "Python node count (1343)"
else failed "Python node count" "expected 1343, got $PY_COUNT"; fi

# T2.3: Module path consistency — no bare "server" module
BARE_SERVER=$(curl -s "$ES/$IDX/_count" -H 'Content-Type: application/json' \
  -d '{"query":{"term":{"logloom.module":"server"}}}' | jq '.count')
if [ "$BARE_SERVER" = "0" ]; then passed "No bare 'server' module paths (0)"
else failed "Module path consistency" "expected 0 bare 'server', got $BARE_SERVER"; fi

# T2.4: Bare '{}' templates ≤ 1 (one legitimate dynamic log)
BARE_TEMPLATE=$(curl -s "$ES/$IDX/_count" -H 'Content-Type: application/json' \
  -d '{"query":{"bool":{"must":[{"term":{"logloom.message_template":"{}"}},{"wildcard":{"logloom.file":"*.go"}}]}}}' | jq '.count')
if [ "$BARE_TEMPLATE" -le 1 ]; then passed "Bare '{}' Go templates ≤ 1 (got $BARE_TEMPLATE)"
else failed "False positive filter" "expected ≤1, got $BARE_TEMPLATE"; fi

# T2.5: Every node has a non-empty function name
EMPTY_FN=$(curl -s "$ES/$IDX/_count" -H 'Content-Type: application/json' \
  -d '{"query":{"bool":{"must_not":[{"exists":{"field":"logloom.function"}}]}}}' | jq '.count')
if [ "$EMPTY_FN" = "0" ]; then passed "All nodes have function names"
else failed "Function name coverage" "$EMPTY_FN nodes missing function"; fi

# T2.6: Every node has a file path
EMPTY_FILE=$(curl -s "$ES/$IDX/_count" -H 'Content-Type: application/json' \
  -d '{"query":{"bool":{"must_not":[{"exists":{"field":"logloom.file"}}]}}}' | jq '.count')
if [ "$EMPTY_FILE" = "0" ]; then passed "All nodes have file paths"
else failed "File path coverage" "$EMPTY_FILE nodes missing file"; fi

# T2.7: Every node has a line number > 0
ZERO_LINE=$(curl -s "$ES/$IDX/_count" -H 'Content-Type: application/json' \
  -d '{"query":{"range":{"logloom.line":{"lte":0}}}}' | jq '.count')
if [ "$ZERO_LINE" = "0" ]; then passed "All nodes have line > 0"
else failed "Line number validity" "$ZERO_LINE nodes with line ≤ 0"; fi

echo ""

# ── Test Suite 3: Call-Graph Integrity ──────────────────────────
echo "── Suite 3: Call-Graph Integrity ──"

# T3.1: Go nodes with edges ≥ 250 (was 252 in blast-radius verification)
GO_WITH_EDGES=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' \
  -d '{"size":0,"query":{"bool":{"must":[{"wildcard":{"logloom.file":"*.go"}},{"script":{"script":"doc[\"logloom.call_parents\"].size() > 0 || doc[\"logloom.call_children\"].size() > 0"}}]}},"aggs":{"c":{"value_count":{"field":"logloom.node_id"}}}}' | jq -r '.aggregations.c.value')
if [ "$GO_WITH_EDGES" -ge 250 ]; then passed "Go nodes with call edges ≥ 250 (got $GO_WITH_EDGES)"
else failed "Go call-graph coverage" "expected ≥250, got $GO_WITH_EDGES"; fi

# T3.2: Python nodes with edges ≥ 1100
PY_WITH_EDGES=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' \
  -d '{"size":0,"query":{"bool":{"must":[{"wildcard":{"logloom.file":"*.py"}},{"script":{"script":"doc[\"logloom.call_parents\"].size() > 0 || doc[\"logloom.call_children\"].size() > 0"}}]}},"aggs":{"c":{"value_count":{"field":"logloom.node_id"}}}}' | jq -r '.aggregations.c.value')
if [ "$PY_WITH_EDGES" -ge 1100 ]; then passed "Python nodes with call edges ≥ 1100 (got $PY_WITH_EDGES)"
else failed "Python call-graph coverage" "expected ≥1100, got $PY_WITH_EDGES"; fi

# T3.3: StartGateway has ≥ 25 call_children (main blast-radius hub)
SG_CHILDREN=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' \
  -d '{"size":1,"query":{"term":{"logloom.function":"StartGateway"}},"sort":[{"logloom.line":"asc"}],"_source":["logloom.call_children"]}' \
  | jq '.hits.hits[0]._source.logloom.call_children | length')
if [ "$SG_CHILDREN" -ge 25 ]; then passed "StartGateway blast radius ≥ 25 children (got $SG_CHILDREN)"
else failed "StartGateway blast radius" "expected ≥25 children, got $SG_CHILDREN"; fi

# T3.4: main() has ≥ 50 call_children (top-level entry point)
MAIN_CHILDREN=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' \
  -d '{"size":1,"query":{"bool":{"must":[{"term":{"logloom.function":"main"}},{"wildcard":{"logloom.file":"*.go"}}]}},"sort":[{"logloom.line":"asc"}],"_source":["logloom.call_children"]}' \
  | jq '.hits.hits[0]._source.logloom.call_children | length')
if [ "$MAIN_CHILDREN" -ge 50 ]; then passed "main() blast radius ≥ 50 children (got $MAIN_CHILDREN)"
else failed "main() blast radius" "expected ≥50 children, got $MAIN_CHILDREN"; fi

# T3.5: HealthChecker.Start goroutine closure edge resolved
HC_CHILDREN=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' \
  -d '{"size":1,"query":{"bool":{"must":[{"term":{"logloom.function":"HealthChecker.Start"}},{"script":{"script":"doc[\"logloom.call_children\"].size() > 0"}}]}},"_source":["logloom.call_children"]}' \
  | jq '.hits.hits[0]._source.logloom.call_children | length')
if [ "$HC_CHILDREN" -ge 1 ]; then passed "HealthChecker.Start goroutine edge (got $HC_CHILDREN children)"
else failed "Goroutine closure edge" "HealthChecker.Start has no call_children"; fi

# T3.6: FrontierProber.Start goroutine closure edge resolved
FP_CHILDREN=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' \
  -d '{"size":1,"query":{"bool":{"must":[{"term":{"logloom.function":"FrontierProber.Start"}},{"script":{"script":"doc[\"logloom.call_children\"].size() > 0"}}]}},"_source":["logloom.call_children"]}' \
  | jq '.hits.hits[0]._source.logloom.call_children | length')
if [ "$FP_CHILDREN" -ge 1 ]; then passed "FrontierProber.Start goroutine edge (got $FP_CHILDREN children)"
else failed "Goroutine closure edge" "FrontierProber.Start has no call_children"; fi

echo ""

# ── Test Suite 4: Blast-Radius Queries ──────────────────────────
echo "── Suite 4: Blast-Radius Queries (Real-World Use Cases) ──"

# T4.1: "What functions does StartGateway call?" — should return resolved function names
# Build a JSON array of the first 5 child node IDs using jq (macOS-compatible)
SG_IDS_JSON=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' \
  -d '{"size":1,"query":{"term":{"logloom.function":"StartGateway"}},"sort":[{"logloom.line":"asc"}],"_source":["logloom.call_children"]}' \
  | jq '.hits.hits[0]._source.logloom.call_children[:5]')
RESOLVED=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' \
  -d "{\"size\":5,\"query\":{\"terms\":{\"logloom.node_id\":$SG_IDS_JSON}},\"_source\":[\"logloom.function\",\"logloom.file\",\"logloom.line\"]}" \
  | jq -r '.hits.hits[]._source.logloom | "\(.function) @ \(.file):\(.line)"')
if [ -n "$RESOLVED" ]; then
  passed "StartGateway children resolve to named functions"
  echo "      ↳ $(echo "$RESOLVED" | head -3 | tr '\n' ' | ')"
else
  failed "Call-graph resolution" "StartGateway children did not resolve to named functions"
fi

# T4.2: "What log messages could be affected if ProviderRouter.Route fails?"
PR_COUNT=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' \
  -d '{"size":0,"query":{"bool":{"must":[{"term":{"logloom.function":"ProviderRouter.Route"}}]}},"aggs":{"templates":{"terms":{"field":"logloom.message_template","size":20}}}}' \
  | jq '.aggregations.templates.buckets | length')
if [ "$PR_COUNT" -ge 2 ]; then passed "ProviderRouter.Route has ≥ 2 distinct templates (got $PR_COUNT)"
else failed "Blast radius query" "expected ≥2 templates for ProviderRouter.Route, got $PR_COUNT"; fi

# T4.3: Semantic tag filtering — find all "auth" related log sites
AUTH_COUNT=$(curl -s "$ES/$IDX/_count" -H 'Content-Type: application/json' \
  -d '{"query":{"term":{"logloom.tags":"auth"}}}' | jq '.count')
if [ "$AUTH_COUNT" -ge 5 ]; then passed "Semantic tag 'auth' returns ≥ 5 sites (got $AUTH_COUNT)"
else failed "Semantic tag query" "expected ≥5 auth tags, got $AUTH_COUNT"; fi

# T4.4: "Show all error/warning log sites in the vault module"
VAULT_ERRORS=$(curl -s "$ES/$IDX/_count" -H 'Content-Type: application/json' \
  -d '{"query":{"bool":{"must":[{"wildcard":{"logloom.module":"*vault*"}},{"terms":{"logloom.tags":["error","warning"]}}]}}}' | jq '.count')
if [ "$VAULT_ERRORS" -ge 3 ]; then passed "Vault error/warning sites ≥ 3 (got $VAULT_ERRORS)"
else failed "Module-scoped error query" "expected ≥3, got $VAULT_ERRORS"; fi

# T4.5: Cross-language edge query — Python nodes calling into Go-adjacent functions
CROSS_LANG=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' \
  -d '{"size":0,"query":{"bool":{"must":[{"wildcard":{"logloom.file":"*.py"}},{"script":{"script":"doc[\"logloom.call_children\"].size() > 3"}}]}},"aggs":{"c":{"value_count":{"field":"logloom.node_id"}}}}' \
  | jq -r '.aggregations.c.value')
if [ "$CROSS_LANG" -ge 50 ]; then passed "Python nodes with >3 children ≥ 50 (got $CROSS_LANG)"
else failed "Python call-graph depth" "expected ≥50, got $CROSS_LANG"; fi

echo ""

# ── Test Suite 5: Mapping & Field Integrity ─────────────────────
echo "── Suite 5: Mapping & Field Integrity ──"

# T5.1: Key fields are keyword type (queryable)
MAPPING=$(curl -s "$ES/$IDX/_mapping")
FILE_TYPE=$(echo "$MAPPING" | jq -r '.[].mappings.properties.logloom.properties.file.type // "missing"')
if [ "$FILE_TYPE" = "keyword" ]; then passed "logloom.file is keyword type"
else failed "Field mapping" "logloom.file type is $FILE_TYPE, expected keyword"; fi

FUNC_TYPE=$(echo "$MAPPING" | jq -r '.[].mappings.properties.logloom.properties.function.type // "missing"')
if [ "$FUNC_TYPE" = "keyword" ]; then passed "logloom.function is keyword type"
else failed "Field mapping" "logloom.function type is $FUNC_TYPE, expected keyword"; fi

MODULE_TYPE=$(echo "$MAPPING" | jq -r '.[].mappings.properties.logloom.properties.module.type // "missing"')
if [ "$MODULE_TYPE" = "keyword" ]; then passed "logloom.module is keyword type"
else failed "Field mapping" "logloom.module type is $MODULE_TYPE, expected keyword"; fi

TAGS_TYPE=$(echo "$MAPPING" | jq -r '.[].mappings.properties.logloom.properties.tags.type // "missing"')
if [ "$TAGS_TYPE" = "keyword" ]; then passed "logloom.tags is keyword type"
else failed "Field mapping" "logloom.tags type is $TAGS_TYPE, expected keyword"; fi

TEMPLATE_TYPE=$(echo "$MAPPING" | jq -r '.[].mappings.properties.logloom.properties.message_template.type // "missing"')
if [ "$TEMPLATE_TYPE" = "keyword" ]; then passed "logloom.message_template is keyword type"
else failed "Field mapping" "logloom.message_template type is $TEMPLATE_TYPE, expected keyword"; fi

echo ""

# ── Test Suite 6: Git Metadata ──────────────────────────────────
echo "── Suite 6: Git Metadata ──"

# T6.1: Graph has commit SHA (field: logloom.commit_sha)
COMMIT=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' \
  -d '{"size":1,"_source":["logloom.commit_sha"]}' | jq -r '.hits.hits[0]._source.logloom.commit_sha // "missing"')
if [ "$COMMIT" != "missing" ] && [ "$COMMIT" != "null" ] && [ ${#COMMIT} -ge 7 ]; then
  passed "Git commit SHA present (${COMMIT:0:8}...)"
else
  failed "Git metadata" "commit_sha missing or invalid: $COMMIT"
fi

echo ""

# ═══════════════════════════════════════════════════════════════
# FINAL RESULTS
# ═══════════════════════════════════════════════════════════════
echo "═══════════════════════════════════════════════════════════════"
if [ $FAIL -eq 0 ]; then
  echo "  🟢 ALL $TOTAL TESTS PASSED ($PASS/$TOTAL)"
else
  echo "  🔴 $FAIL/$TOTAL TESTS FAILED ($PASS passed, $FAIL failed)"
fi
echo "═══════════════════════════════════════════════════════════════"
