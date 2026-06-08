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
**CRITICAL CLARIFICATION (user query):** Phase 2 does **NOT** mean users must (or should) authenticate with their local Ollama models. This would be a lot of overhead — most people do not (and should not have to) do this for LAN/local dev setups. Auth for local nodes is **strictly optional and opt-in only**.

- Default behavior for all existing and new meshes is **unauthenticated** (no Authorization header, zero config or overhead). This matches stock Ollama on `host.docker.internal`, `192.168.x`, localhost, etc.
- Users who intentionally secure their Ollama instances (e.g. behind nginx auth_request, a reverse proxy enforcing Bearer, or corporate firewall rules) can **manually set up auth**:
  1. Put the bearer token into OpenBao at a path of their choice (e.g. `bao kv put secret/flume/nodes/secure-ollama-1 token=...` or equivalent).
  2. When adding/registering the node in the **portal** (or via direct `POST /api/nodes`), include `"auth_secret_path": "secret/data/flume/nodes/secure-ollama-1"` (or the relative form).
  3. The gateway will lazily load the token (in-memory only, never logged or to ES) and inject `Authorization: Bearer ...` **only** for that node on health probes + inference calls.
- The portal already supports this today because `handleNodesAdd` proxies the raw body (including `auth_secret_path`) to the gateway. No special UI fields needed for basic manual use; future polish can add a "secured node" toggle + secret value helper.
- YAML mesh config (advanced) also accepts the field for `flume start` seeding.
- All changes are conditional: `if authToken != "" { set header }`. No require, no default paths, no wizard forcing.

**Goal:** Make "secure coding platform" real *for those who opt into protected local nodes*. Complete the declared-but-incomplete auth story for inference (not just probes; it was only on health before). Improve health for auth-aware nodes. Preserve the phenomenal zero-config experience for everyone else.
**Scope (report refs):** "Make auth complete for local"; "Enhance node health to also probe auth"; part of Security in §5; ties to "Auth for Local (security gap)" in §4. Explicitly addresses "auth ... optional ... manually setup within the portal".
**Deliverables:**
- Inject Bearer in inference paths: in `ollamaWithNode`, StreamOllama* funcs (and nonstream if still used for local), if `authToken != ""` set `req.Header.Set("Authorization", "Bearer "+authToken)` (match health_checker pattern). Fix prior skeleton where callers passed "" even when token was available.
- Implement AuthToken population from OpenBao (lazy, opt-in):
  - Add `SecretStore.GetNodeAuthToken` (graceful "", supports common keys like "token"/"bearer_token"/"value").
  - In `node_registry.go`: `NewNodeRegistry` takes `*SecretStore`; `resolveAuthTokenIfNeeded` (lazy on Get/refresh/route/health; brief lock only on set; never on unauth nodes).
  - Called from RefreshFromES (post-unlock), GetNode, health probes, routers before RouteToNode. Token lives in-mem only (stripped on all ES/API).
- Enhance `health_checker.go`: call resolve before probes; record auth success/failure distinctly; emit `LogAgentReasoning` (node-*, "node-auth", ...) on load attempts + probe auth outcomes (per flume-go + reliable-go SKILLs).
- Update node add/upsert (server.go handleAddNode) + post-upsert Refresh so newly registered authed nodes get token resolved immediately for test probe + UI.
- Update node test API (/api/nodes/{id}/test) — already exercises via registry node (now with token).
- Expose/document in UI/CLI/docs (node registration accepts secret_path; add godoc + comments everywhere).
- Update `flume-go/SKILL.md` + report + this doc: "local LLM auth is opt-in only; conditional injection + rich reasoning on every auth decision/load".
- Tests: existing unauth tests continue to pass with no header; add/keep coverage for conditional (mock store).
- Non-breaking + zero overhead: Conditional (if token present); unauth nodes (AuthSecretPath=="") work exactly as before (and as last-night working baseline). Frontier paths untouched.
**Verification:** todo sub-tasks; reasoning emitted on every auth decision (success/fail/attempt); ctx/err discipline; tests with -race; rebuild + (optional) 250s monitor on simple plan using unauth local mesh to prove no regression; verification against original report + user explicit constraint.
**Dependencies:** Phase 1 conn manager (auth'd streams reuse too); existing SecretStore/OpenBao (no new infra).
**Risks/Mitigation:** Token fetch fails → graceful (log + reason + fall to no-auth for that node, never hard fail a call); secret path typos (user sees via /test + reasoning); doc the manual bao step.
**Expected:** Users who *want* secured nodes now get full end-to-end auth for both health + actual chat/inference (was incomplete). Everyone else: identical UX and perf to pre-Phase2, zero extra steps.
**Timeline:** Complete as part of initial 2 phases (small focused changes).

### Phase 3: Planner-Specific Fast Path & Propagation (Target <120s Local Success)
**Implemented:** 
- Fast local path in executePlanningWithMeshResilience: top selected node + at most 1 secondary (limited vs full healthy loop exhaust). Added explicit LogAgentReasoning for the decision. (Top-only or limited keeps wall time under the 120s caller budget for simple local plans.)
- Optional RAG skip in handleIntake* for pure-doc prompts (heuristic on keywords like "document"/"cli"/"readme" + no code verbs; emits logReasoning + skips fetch to save 8s+tokens).
- Propagation: added flumelogger.LogAgentReasoning in routeToNode (and planning) for every LLM hop (node_id, host, model, tokens, dur, plan_session, role). Dashboard intake now surfaces slow>60s with telemetry + reasoning (Phase4 tie-in).
- Per-role bias: in SelectNode, extra *1.15 boost for planning/intake on qwen/32b/72b models.
- Non-breaking, gated to planning paths, comments with "Phase 3".
**Scope (rest per original):** Model selection refinements, full parallel race option as follow-up.

### Phase 4: Observability, Tracing & UX
**Implemented:**
- Hop logs: LogAgentReasoning(ctx, ..., "gateway-llm-hop", msg with node+dur+tokens) on every routeToNode success/fail (plus in planning-router for fast path choice).
- Conn stats exposed: wired Server.connMgr -> ProviderRouter; BuildLiveGatewayMetrics now accepts+uses it (or package fallback); added FlumeNodeConnStats to LiveGatewayMetrics + populated in /api/gateway-metrics JSON (used by dashboard telemetry bridge). Stats include per-node idle/keepalive from manager (Phase4 TODO in manager for real in-use later).
- "Why slow" UX: in intake after LLM, if elapsed>60s emit rich logReasoning with node telemetry from resp (node_id/host/model/elapsed); visible in popout/Logloom. Also "slow surface" note.
- Ensemble: existing behind policy for complex (local jury); noted as satisfying.
- No p99 yet (future from metrics), no spans (use Log* + request_id as simple tracing).
- UI/telemetry: the /gateway-metrics now carries conn stats for Analytics/Node cards.
**Scope (rest):** Add node p99, deeper tracing, health reuse of conns for probes.

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

**Status update (after initial phases + next 2):** Phase 1 core (conn mgr + always-stream for local + partial wiring) + Phase 2 (opt-in auth completion, health, manual portal, Log* on auth, full conditional injection) completed (with user clarification that auth is strictly optional). Phase 1 wiring completed as prereq (Server owns + passes connMgr; stats exposed; better keying). Phase 3 (fast planner path: top+1 secondary only to avoid exhaust for <120s; optional RAG skip heuristic for pure-doc prompts; per-role model bias in Select; rich LogAgentReasoning propagation on every gateway LLM hop + intake slow surface) + Phase 4 (conn stats in LiveGatewayMetrics + /gateway-metrics for UX; why-slow reasoning if planner>60s with node telemetry; hop logs via LogAgentReasoning) implemented. Bounded, no breaks to frontier, SKILLs followed (todos, reasoning on decisions, ctx, explicit).

Next steps: Phase 5 (mTLS etc) and Phase 6 (polish, full measurement, SKILL updates, update report with before/after from local repro).

(End of phased plan. Original report remains source of truth for details.)
