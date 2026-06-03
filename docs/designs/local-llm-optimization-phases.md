# Local LLM Mesh Optimization - Phased Implementation Plan

**Based on:** `docs/reference/local-llm-mesh-optimization-report.md` (2026-06-03)  
**Date:** 2026-06-03  
**Principles (from flume-go + reliable-go-systems SKILLs):** 
- Follow reliable-go: ctx first, explicit wrapped errors, reconciliation, bounded (sem, timeouts on all I/O), structured obs + rich reasoning (LogAgentReasoning on every LLM hop/decision), idempotent, no globals/fire-forget, verification loops.
- Flume-specific: propagate task/plan/agent ids/roles everywhere for routing/budget/rate/audit; enhance (never bypass) reasoning/logs; preserve Manager/Claimer/Sweeper/Runner/StateMachine separation; only enhance ProviderOllama + mesh paths (zero breaking changes to frontier OpenAI-compat, Anthropic, etc.).
- Use todo_write for >3 steps, check-work style verification, golangci-lint + -race, rich comments with phase refs.
- Prioritization: Highest impact/lowest risk first (connection reuse + streaming = biggest perf win for distributed mesh). Small PRs, feature flags where risky, zero-downtime.

## Phase Breakdown

### Phase 1: Persistent Connection Management & Local Streaming Unification (Core Efficiency - Highest Leverage)
**Goal:** Eliminate repeated TCP handshakes for local Ollama nodes (biggest gap in "efficient/performant"). Make local LLM comms use HTTP/2 where possible and always-streaming. Directly addresses "Connection Mgmt (big perf hit)" and "Other" + "No HTTP/2" in report §4.
**Scope (report refs):** Core Optimization "Node-Aware Persistent Connection Manager"; "Always-stream for *all* local Ollama calls"; "Unify timeout"; "Inject into StreamOllama* / doPost / ollama*".
**Deliverables:**
- New `src/gateway/node_conn_manager.go` (or integrated enhancement to ProviderRouter).
- Per-node shared `*http.Transport` (or client pool): `MaxIdleConnsPerHost=20`, `IdleConnTimeout=90s`, `TLSHandshakeTimeout=5s`, `DisableKeepAlives=false`, `ForceAttemptHTTP2=true` (if backend supports - gives multiplexing without protocol change).
- Lifecycle tied to node health (close idles on offline; warm on healthy).
- Refactor:
  - `src/gateway/tool_stream.go`: StreamOllamaChat, StreamOllamaToolCall use managed client/transport.
  - `src/gateway/providers.go`: ollamaWithNode, ollamaNonStream, doPost, ollama*, RouteToNode use manager (pass transport or factory).
- Force always-stream for local Ollama (in ollamaWithNode / decision logic): planning + exec. Update intake paths if needed (can collect full for JSON parse while streaming NDJSON). Non-stream only for frontier.
- Unify timeouts: prefer `ctx.WithTimeout` (caller-provided 120s etc.); deprecate/minimize hard client.Timeouts (or make generous + ctx wins). Pass ctx through.
- Basic observability: expose conn stats (idle/inuse per node) via existing metrics/health (e.g. in `BuildLiveGatewayMetrics` or /health).
- Update `flume-go/SKILL.md` checklist minimally.
- Minimal tests: extend `src/gateway/gateway_test.go`, `node_api_test.go` for transport reuse (e.g. assert conn reuse after calls), no regression on stream/nonstream.
- Config: `FLUME_OLLAMA_KEEPALIVE`, `FLUME_OLLAMA_MAX_IDLE_PER_NODE` (safe defaults).
- Non-breaking: Only ProviderOllama + mesh. Frontier paths (openaiCompat, etc.) untouched. Legacy direct fallback unchanged.
**Verification (per SKILLs):** todo_write sub-tasks; read SKILLs first; rich phase comments; `go test -race` + golangci; local mesh repro (as in prior 250s monitors); before/after handshake latency (e.g. via logs or tcpdump).
**Dependencies:** None (builds on existing baseURL/authToken threading).
**Risks/Mitigation:** Conn mgmt bugs (feature flag `FLUME_OLLAMA_CONN_POOL=1`); perf regression on frontier (none).
**Expected:** Local planner <60-90s on simple requests (reuse saves handshakes); higher mesh throughput; measurable via new stats.
**Timeline:** 1-2 days (small, focused PR).

### Phase 2: Local Auth Completion & Health Probe Enhancements (Security & Reliability)
**Goal:** Make "secure coding platform" real for protected local nodes. Complete the declared-but-incomplete auth story for inference (not just probes). Improve health for auth-aware nodes.
**Scope (report refs):** "Make auth complete for local"; "Enhance node health to also probe auth"; part of Security in §5; ties to "Auth for Local (security gap)" in §4.
**Deliverables:**
- Inject Bearer in inference paths: in `ollamaWithNode`, StreamOllama* funcs (and nonstream if still used for local), if `authToken != ""` set `req.Header.Set("Authorization", "Bearer "+authToken)` (match health_checker pattern).
- Implement AuthToken population from OpenBao:
  - In `node_registry.go:RefreshFromES` or new recon, if `AuthSecretPath` set, use `SecretStore` (existing pattern in secrets.go) to load token into `node.AuthToken` (in-memory only, never to ES/logs).
  - Lazy load on first use in health or route if not present.
- Enhance `health_checker.go`: 
  - Dedicated auth probe (e.g. /api/version or tags with auth test on registration/probe).
  - Record auth success/failure in health/capabilities.
  - Emit `LogAgentReasoning` (and LogStateTransition) on auth events/decisions (per flume-go SKILL).
- Update node add/upsert (server.go handleAddNode, registry) to validate/persist AuthSecretPath.
- Update node test API (/api/nodes/{id}/test) to exercise auth.
- Expose in UI/docs (e.g. node registration supports secret_path).
- Update `flume-go/SKILL.md` + report checklist: "LLM local calls use ... + full auth injection + reasoning emitted".
- Tests: auth injection tests, mock OpenBao load, health with/without token, no regression on unauth nodes (header omitted if empty).
- Non-breaking: Conditional (if token present); unauth nodes work as before. Frontier unchanged (they use different cred resolution).
**Verification:** todo sub-tasks; reasoning on every auth decision; ctx/err discipline; tests with -race; update SKILL; verification against original report + prior monitors.
**Dependencies:** Phase 1 (for transport reuse in auth'd paths? optional); existing SecretStore/OpenBao.
**Risks:** Token fetch failures (graceful, log + fall to no-auth); secret path mismatches (document + test API).
**Expected:** Protected nodes now fully secure for chat (not just health); better "dependable" UX.
**Timeline:** 1-2 days (after Phase 1).

### Phase 3: Planner-Specific Fast Path & Propagation (Target <120s Local Success)
**Scope:** "Routing/Planning Specific (for <120s simple local success)"; "Propagate more"; "Model selection".
- Fast local path for intake planner: top-node only (or parallel quick race within 60s budget) in resilience logic; avoid full mesh exhaust for breakdown.
- Optional RAG skip for pure-doc prompts (detect low complexity + keywords).
- Ensure propagation: every gateway LLM call (planning) emits structured + LogAgentReasoning (node_id, duration, tokens, etc.). Dashboard intake logs RAG/LLM steps.
- Per-role preferred local model bias in SelectNode/config.
- Non-breaking.

### Phase 4: Observability, Tracing & UX
**Scope:** "Observability & Usefulness"; "Per-node conn stats"; "Why slow" UX; "Distributed tracing"; "Metrics"; "ensemble local".
- Wrap node calls in spans or hop logs (request_id + llm_hop with node details).
- Expose conn stats in health/metrics.
- Surface node/gen latency in planningStatus + reasoning if >60s.
- Node-specific p99 latency metrics.
- Behind-policy local ensemble (query 2-3 nodes, pick best).
- Update UI/telemetry.

### Phase 5: Security Hardening (mTLS etc.)
**Scope:** "Security Hardening (no frontier impact)".
- mTLS for mesh: per-node certs from OpenBao, custom Transport TLS config.
- Enforce network (docs/compose).
- Enhance rate (token bucket in manager).
- Audit index "agent-llm-calls" or reuse telemetry.
- Non-breaking for frontier.

### Phase 6: Polish, SKILLs, Tests, Measurement, Docs
- Full SKILL updates (flume-go + reliable-go checklists).
- Comprehensive tests + check-work verification loops.
- Measurement harness (repro simple request, handshake timing, p99, conn counts).
- Update original report + this phases doc with results.
- Feature flags cleanup, migration notes.
- Doctor/CLI support for new settings.

**Overall Verification Approach (SKILLs):** Every phase uses todo_write (this doc + sub), read SKILLs, rich "Phase X" comments, bounded changes, explicit errs, ctx, Log* reasoning, `go test -race` + lint, local repro (docker logs + API polls as in 250s monitor), before/after metrics. Use check-work subagent for reviews. Small PRs per phase or grouped 1+2.

**No Breaking Changes:** All changes gated to `ProviderOllama`, `multi_node_router`, `node_*` paths. `Route`, frontier providers (openaiCompat, anthropic, etc.), legacy fallback, worker LLM client (prefers gateway) untouched. Config/env additive.

**Success Criteria (from report):** Local simple plans reliably <60-90s end-to-end on mesh; measurable conn reuse; full auth for local; better obs; secure mTLS option; follows SKILLs.

Next steps: Implement Phase 1 (start with conn manager skeleton + transport refactor in tool_stream/providers).

(End of phased plan. Original report remains source of truth for details.)
