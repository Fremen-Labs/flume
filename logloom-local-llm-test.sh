#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
# LogLoom Local LLM A/B Test
# ════════════════════════════════════════════════════════════════
# Tests 2 real debugging questions against the local Ollama model
# with and without LogLoom context enrichment.
#
# For each question:
#   Trial A: Full file context (load the entire source file)
#   Trial B: LogLoom ES context (targeted log-site metadata only)
#
# Measures: prompt tokens, completion tokens, latency, answer quality

OLLAMA="http://localhost:11434"
MODEL="qwen3.5:9b"
ES="http://localhost:9205"
IDX="flume-logloom-enrichment"
GATEWAY_SRC="/Users/jonathandoughty/clients/fremenlabs/flume /flume/src/gateway"

echo "════════════════════════════════════════════════════════════════"
echo "  LogLoom Local LLM A/B Test"
echo "  Model: $MODEL | Ollama: $OLLAMA"
echo "  ES: $ES | Index: $IDX"
echo "════════════════════════════════════════════════════════════════"
echo ""

# Helper: call ollama and capture full response with timing
ollama_chat() {
  local label="$1"
  local system_msg="$2"
  local user_msg="$3"
  local outfile="$4"

  local payload
  payload=$(jq -n \
    --arg model "$MODEL" \
    --arg sys "$system_msg" \
    --arg usr "$user_msg" \
    '{
      model: $model,
      messages: [
        {role: "system", content: $sys},
        {role: "user", content: $usr}
      ],
      stream: false,
      options: {
        temperature: 0.3,
        num_predict: 2048,
        num_ctx: 8192
      }
    }')

  local start_ms
  start_ms=$(python3 -c "import time; print(int(time.time()*1000))")

  local resp
  resp=$(curl -s --max-time 120 "$OLLAMA/api/chat" -H 'Content-Type: application/json' -d "$payload")

  local end_ms
  end_ms=$(python3 -c "import time; print(int(time.time()*1000))")

  local wall_ms=$((end_ms - start_ms))
  local prompt_tokens=$(echo "$resp" | jq '.prompt_eval_count // 0')
  local completion_tokens=$(echo "$resp" | jq '.eval_count // 0')
  local content=$(echo "$resp" | jq -r '.message.content // "ERROR: no response"')

  echo "$resp" > "$outfile"

  echo "  ⏱️  Wall time: ${wall_ms}ms"
  echo "  📊 Prompt tokens: ${prompt_tokens}"
  echo "  📊 Completion tokens: ${completion_tokens}"
  echo "  📊 Total tokens: $((prompt_tokens + completion_tokens))"
  echo ""
  echo "  ── Response (first 800 chars) ──"
  echo "$content" | head -25
  echo ""

  # Export for report
  eval "${label}_WALL_MS=$wall_ms"
  eval "${label}_PROMPT=$prompt_tokens"
  eval "${label}_COMPLETION=$completion_tokens"
  eval "${label}_TOTAL=$((prompt_tokens + completion_tokens))"
}

# ══════════════════════════════════════════════════════════════════
# QUESTION 1: Circuit Breaker State Machine
# "The health checker is marking nodes offline incorrectly.
#  What log messages fire during each circuit breaker transition?"
# ══════════════════════════════════════════════════════════════════

Q1="The health checker is marking Ollama nodes offline incorrectly in the Flume Go gateway. I need to understand the circuit breaker state machine. What log messages fire during each circuit breaker state transition (closed → degraded → open → half-open → closed)? List every log site with its file, line number, and the exact log message template. Then explain the transition conditions."

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  QUESTION 1: Circuit Breaker State Machine"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Trial 1A: Full file context ──────────────────────────────────────
echo "┌─────────────────────────────────────────────────────────────┐"
echo "│ Trial 1A: Full File Context (health_checker.go — 760 lines)│"
echo "└─────────────────────────────────────────────────────────────┘"

FILE_CONTEXT=$(cat "$GATEWAY_SRC/health_checker.go")

SYSTEM_1A="You are a Go debugging assistant. You have been given the complete source code of a file from the Flume gateway. Answer the user's question precisely, citing exact line numbers and log message strings."

USER_1A="Here is the complete source code of health_checker.go:

\`\`\`go
$FILE_CONTEXT
\`\`\`

$Q1"

ollama_chat "Q1A" "$SYSTEM_1A" "$USER_1A" "/tmp/q1a_response.json"

echo ""

# ── Trial 1B: LogLoom ES context ─────────────────────────────────────
echo "┌─────────────────────────────────────────────────────────────┐"
echo "│ Trial 1B: LogLoom ES Context (~4 targeted log sites)       │"
echo "└─────────────────────────────────────────────────────────────┘"

# Query LogLoom for circuit breaker log sites
LOGLOOM_Q1=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' -d '{
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"wildcard": {"logloom.file": "*health_checker*"}},
        {"bool": {"should": [
          {"wildcard": {"logloom.message_template": "*circuit*"}},
          {"wildcard": {"logloom.message_template": "*offline*"}},
          {"wildcard": {"logloom.message_template": "*half-open*"}},
          {"wildcard": {"logloom.message_template": "*recovered*"}},
          {"wildcard": {"logloom.message_template": "*probe failed*"}},
          {"wildcard": {"logloom.message_template": "*healthy*"}}
        ], "minimum_should_match": 1}}
      ]
    }
  },
  "_source": ["logloom.function", "logloom.file", "logloom.line", "logloom.message_template", "logloom.tags", "logloom.call_parents"],
  "sort": [{"logloom.line": "asc"}]
}')

# Format the LogLoom data for the LLM
LOGLOOM_CONTEXT=$(echo "$LOGLOOM_Q1" | jq -r '.hits.hits[]._source.logloom | "- File: \(.file), Line: \(.line), Function: \(.function), Message: \"\(.message_template)\", Tags: \(.tags | join(", "))"')

SYSTEM_1B="You are a Go debugging assistant. You have been given structured observability metadata from the LogLoom call-graph index for the Flume gateway. Each entry represents a log site with its exact file path, line number, containing function, message template, and semantic tags. Use this data to answer the user's question precisely."

USER_1B="Here is the LogLoom observability data for the circuit breaker and health checker log sites in the Flume Go gateway:

$LOGLOOM_CONTEXT

Also, here are the key constants from the health checker:
- circuitOpenThreshold = 3 consecutive failures
- circuitHalfOpenSuccesses = 2 consecutive successes
- offlineProbeInterval = 60 seconds
- defaultHealthInterval = 15 seconds

$Q1"

ollama_chat "Q1B" "$SYSTEM_1B" "$USER_1B" "/tmp/q1b_response.json"

echo ""
echo ""

# ══════════════════════════════════════════════════════════════════
# QUESTION 2: Multi-Node Routing Decision Trace
# "The gateway is routing requests to the wrong node.
#  What is the complete decision path?"
# ══════════════════════════════════════════════════════════════════

Q2="The Flume gateway's multi-node router is selecting the wrong Ollama node for code generation tasks. I need to trace the complete routing decision path. What log messages fire at each step from ExecuteSmartRoute to the final node selection? Include every decision branch (frontier_only, hybrid, local_only modes) and the fallback cascades. List each log site with its file, line number, function, and exact message template."

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  QUESTION 2: Multi-Node Routing Decision Trace"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Trial 2A: Full file context ──────────────────────────────────────
echo "┌─────────────────────────────────────────────────────────────┐"
echo "│ Trial 2A: Full File Context (multi_node_router.go + node_  │"
echo "│           registry.go — 950 lines combined)                │"
echo "└─────────────────────────────────────────────────────────────┘"

FILE_CONTEXT_2A=$(cat "$GATEWAY_SRC/multi_node_router.go" "$GATEWAY_SRC/node_registry.go")

SYSTEM_2A="You are a Go debugging assistant. You have been given the complete source code of two files from the Flume gateway. Answer the user's question precisely, citing exact line numbers and log message strings from both files."

USER_2A="Here is the complete source code of multi_node_router.go and node_registry.go:

\`\`\`go
$FILE_CONTEXT_2A
\`\`\`

$Q2"

ollama_chat "Q2A" "$SYSTEM_2A" "$USER_2A" "/tmp/q2a_response.json"

echo ""

# ── Trial 2B: LogLoom ES context ─────────────────────────────────────
echo "┌─────────────────────────────────────────────────────────────┐"
echo "│ Trial 2B: LogLoom ES Context (~19 targeted log sites)      │"
echo "└─────────────────────────────────────────────────────────────┘"

# Query LogLoom for routing decision log sites
LOGLOOM_Q2=$(curl -s "$ES/$IDX/_search" -H 'Content-Type: application/json' -d '{
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
        ]}},
        {"bool": {"must": [
          {"wildcard": {"logloom.file": "*routing_policy*"}},
          {"bool": {"should": [
            {"wildcard": {"logloom.message_template": "*routing*"}},
            {"wildcard": {"logloom.message_template": "*frontier*"}},
            {"wildcard": {"logloom.message_template": "*hybrid*"}}
          ]}}
        ]}}
      ],
      "minimum_should_match": 1
    }
  },
  "_source": ["logloom.function", "logloom.file", "logloom.line", "logloom.message_template", "logloom.tags", "logloom.call_parents"],
  "sort": [{"logloom.file": "asc"}, {"logloom.line": "asc"}]
}')

LOGLOOM_CONTEXT_2=$(echo "$LOGLOOM_Q2" | jq -r '.hits.hits[]._source.logloom | "- File: \(.file), Line: \(.line), Function: \(.function), Message: \"\(.message_template)\", Tags: \(.tags | join(", "))"')

SYSTEM_2B="You are a Go debugging assistant. You have been given structured observability metadata from the LogLoom call-graph index for the Flume gateway. Each entry represents a log site with its exact file path, line number, containing function, message template, and semantic tags. Use this data to answer the user's question precisely."

USER_2B="Here is the LogLoom observability data for the multi-node routing decision path in the Flume Go gateway:

$LOGLOOM_CONTEXT_2

Routing mode constants:
- RoutingModeFrontierOnly = \"frontier_only\"
- RoutingModeHybrid = \"hybrid\"
- RoutingModeLocalOnly = \"local_only\" (default)

$Q2"

ollama_chat "Q2B" "$SYSTEM_2B" "$USER_2B" "/tmp/q2b_response.json"

echo ""
echo ""

# ══════════════════════════════════════════════════════════════════
# FINAL COMPARISON SUMMARY
# ══════════════════════════════════════════════════════════════════
echo "════════════════════════════════════════════════════════════════"
echo "  LOCAL LLM A/B TEST — FINAL COMPARISON"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "  Model: $MODEL"
echo "  Context Window: 8192 tokens"
echo ""
echo "  ┌────────────────┬───────────┬───────────┬───────────┬──────────┐"
echo "  │ Trial          │ Prompt    │ Comp.     │ Total     │ Latency  │"
echo "  ├────────────────┼───────────┼───────────┼───────────┼──────────┤"
printf "  │ Q1A Full File  │ %9s │ %9s │ %9s │ %7sms │\n" "$Q1A_PROMPT" "$Q1A_COMPLETION" "$Q1A_TOTAL" "$Q1A_WALL_MS"
printf "  │ Q1B LogLoom    │ %9s │ %9s │ %9s │ %7sms │\n" "$Q1B_PROMPT" "$Q1B_COMPLETION" "$Q1B_TOTAL" "$Q1B_WALL_MS"
printf "  │ Q2A Full File  │ %9s │ %9s │ %9s │ %7sms │\n" "$Q2A_PROMPT" "$Q2A_COMPLETION" "$Q2A_TOTAL" "$Q2A_WALL_MS"
printf "  │ Q2B LogLoom    │ %9s │ %9s │ %9s │ %7sms │\n" "$Q2B_PROMPT" "$Q2B_COMPLETION" "$Q2B_TOTAL" "$Q2B_WALL_MS"
echo "  └────────────────┴───────────┴───────────┴───────────┴──────────┘"
echo ""

# Compute savings
if [ "$Q1A_PROMPT" -gt 0 ] 2>/dev/null; then
  Q1_SAVINGS=$(python3 -c "print(f'{(1 - $Q1B_PROMPT / $Q1A_PROMPT) * 100:.1f}')")
  Q1_SPEEDUP=$(python3 -c "print(f'{$Q1A_WALL_MS / max($Q1B_WALL_MS, 1):.1f}')")
  echo "  Q1 Token Reduction: ${Q1_SAVINGS}% fewer prompt tokens"
  echo "  Q1 Speed Improvement: ${Q1_SPEEDUP}x faster"
fi
if [ "$Q2A_PROMPT" -gt 0 ] 2>/dev/null; then
  Q2_SAVINGS=$(python3 -c "print(f'{(1 - $Q2B_PROMPT / $Q2A_PROMPT) * 100:.1f}')")
  Q2_SPEEDUP=$(python3 -c "print(f'{$Q2A_WALL_MS / max($Q2B_WALL_MS, 1):.1f}')")
  echo "  Q2 Token Reduction: ${Q2_SAVINGS}% fewer prompt tokens"
  echo "  Q2 Speed Improvement: ${Q2_SPEEDUP}x faster"
fi
echo ""
echo "════════════════════════════════════════════════════════════════"
