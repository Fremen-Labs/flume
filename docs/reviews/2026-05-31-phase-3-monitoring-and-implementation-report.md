# Flume Phase 3 Implementation Report: 10-Minute Live Monitoring Synthesis + Comparison to Prior Phase 3 Plan

**Date**: 2026-05-31  
**Context**: Stack rebuilt; simple documentation update request successfully decomposed by the system into 1 epic + 2 features + 2 stories + 3 tasks (plan-91a43783a9df).  
**Monitoring Window**: ~10 minutes (dashboard + gateway monitors active ~6-10min+ with continuous event stream; worker streams attempted; API snapshot polling context from prior).  
**Primary Monitors**:
- `flume-dashboard` (port 8765 host) — work queue state, _system_analytics, agent reasoning drawers, telemetry.
- `flume-gateway` (port 8090) — LLM calls, per-plan-pm rate limits, tool chat requests (tools:0), embeddings, multi-node routing/fallbacks, ollama slots.
- Worker attempts (flume-worker-1/2) for lease/claim/tool/git paths (partial capture due to command pipe behavior; primary symptoms visible in gateway/dashboard).
- Cross-referenced with running code (internal/worker/runner.go, handlers.go; src/gateway/server.go) and prior design artifacts.

**SKILLs Applied Throughout**: reliable-go-systems (structured observability + decision trails, errors-as-values, timeouts, explicit construction) + flume-go (mandatory LogAgentReasoning + LogStateTransition on every lease/tool/git/LLM/state path, Cross-cutting Writer Rule for lease columns via central guarded mutator, anti-explosion budgets, role-aware resets, "ES = references only").

---

## Executive Summary

The 10-minute live monitoring on a **real, successful small-plan decomposition** (the exact scenario the Phase 3 work queue reliability work targets) surfaced a clear split:

- **Work queue foundations (Phase 3 column/herd/lease work)**: Directionally strong. Central `updateTaskLeaseState` wrapper (runner.go:47), ToolRegistry wiring with real Elastro executor (handlers.go:351+), per-plan PM rate limiter (gateway/server.go:285+), rich reasoning sites, and OpenBao-hardened git paths are present and logging. The small doc-update decomp (1E+2F+2S+3T) completed without explosion — a positive signal vs the historical 9-gap migration failure modes.

- **Production LLM substrate + tool activation + observability**: **Tier 1 blockers**. The local Ollama mesh (primary 192.168.0.227 + fallback 192.168.0.235) is thrashing even on tiny plans:
  - Repeated "per-plan-pm rate limit hit: 3 attempts in last minute for plan-91a43783a9df (cap=3)" exactly on the monitored successful decomp.
  - Context canceled / ~86-120s hangs on primary → fallback attempts → "fallback node also failed".
  - Nom ic-embed-text 404 storm ("try pulling it first").
  - Every observed `incoming chat request` (pm, reviewer, etc.) has `"tools":0`.
  - Dashboard `_system_analytics` (every ~5s): `elastro_data_present:false`, `savings:0`, `telemetry_doc_count:0`, "Elastro instrumentation only."
  - No runtime evidence of `elastro_query_ast` or `logloom_ast_query` firing with rich `LogAgentReasoning` despite registration and implementer skeleton (runner.go:307+).

**Bottom line**: Phase 3 design (column frameworks + thundering herd mitigations + first-tool skeleton + mandatory dual logging) was excellent on the Go worker/queue side and landed key pieces. It **underestimated the fragility of the local LLM gateway + embedding + tool-injection substrate** under even light per-plan PM load from successful small decomps, and the lag in closing the Elastro/Logloom telemetry + tool-usage reasoning loop to the dashboard and agent paths. The "successful" 1E+2F+2S+3T decomp still triggered the exact rate-limit + fallback storms the per-plan limiter was meant to protect against.

**Immediate risk**: Small legitimate plans + PM decomposition will continue to self-throttle and produce poor UX (slow, fallback failures, no code-intel tools visible to agents). Work queue column invariants may hold in Go but the LLM "fuel" for PM/implementer/reviewer is unreliable.

**Prioritized Tier 1 (stabilize this week)**:
1. Strengthen per-plan PM limiter with rich `LogAgentReasoning` on every decision (current count, cap, backoff, plan ID) + jittered backoff + plan-aware telemetry.
2. Pull `nomic-embed-text` (or make embedding model configurable + fallback).
3. Force tool injection (`tools` array) + rich reasoning for all agent roles in gateway chat paths; verify `elastro_query_ast` + `logloom_ast_query` actually execute with before/after `LogAgentReasoning`.
4. Close Elastro telemetry loop (ingest + query counts, AST savings, rag update hooks) so dashboard shows real data instead of "instrumentation only".
5. Re-run `logloom` + `flume doctor --logloom` post any change; emit queue-lifecycle events.

Full Tiered recs and comparison below.

---

## Monitoring Setup Executed

**Docker log streams (gateway + dashboard active throughout window, ~380s+ captured in detail)**:
- Gateway: LLM routing, rate limits, embeddings, tool chat requests, multi-node fallbacks, ollama slot pressure.
- Dashboard: work queue snapshot/analytics, agent reasoning drawer opens, telemetry bridge, _system_analytics (the Elastro blindness source of truth).
- Worker attempts: lease/claim/state transition/tool paths (command exited early on pipe; symptoms redundant with gateway for PM/tool issues).

**API / work queue polling context**: Repeated /api/snapshot + analytics calls visible in dashboard logs; user-driven "opened agent reasoning drawer" events for real tasks (e.g. task-c57f32e63127, task-1/2/3).

**Data volume**: Hundreds of events; dominant patterns repeated every 5-60s. Full raw in session monitor output files (gateway ~142KB, dashboard ~228KB truncated).

**Bugs/Issues/Gaps/Communication Problems Explicitly Logged During Window** (per user directive):
- Per-plan PM rate limit storms on the exact small successful decomp plan.
- Gateway primary node context canceled + deadline exceeded + fallback thrash (communication failure with local LLMs).
- Embeddings 404 (nomic-embed-text missing) — repeated, blocking any RAG/embedding paths.
- Chat requests with tools:0 (no Elastro or Logloom tools visible to agents despite Phase 3.1 registration).
- Complete Elastro blindness in dashboard analytics (no data, savings=0, "instrumentation only").
- High ollama slot occupancy (active 3-4/4) + long 86-120s operation durations.
- No rich tool execution reasoning or LogAgentReasoning for elastro_query_ast / logloom_ast_query in observed agent loops.
- Telemetry sync succeeding for some tasks (positive) but not correlated to tool usage or Elastro.
- Legacy Ollama fallback fragility (decode/ context errors in prior context + current cancels).

---

## Detailed Findings from Live Monitoring (on Real Small Successful Decomp)

### 1. Local LLM Gateway & Per-Plan PM Rate Limiter (Critical Communication Issues)
- Repeated exact hits: `per-plan-pm rate limit hit: 3 attempts in last minute for plan plan-91a43783a9df (cap=3)`.
- Pattern: PM role requests → primary (192.168.0.227) context canceled after ~120s → "multi_node_router: primary node failed, trying fallback" → route to 192.168.0.235 → often "fallback node also failed".
- Reviewer roles also hitting long hangs + fallbacks.
- "awaiting ollama slot" with near-full occupancy.
- Result: Even a 1E+2F+2S+3T doc update (user-confirmed success) triggers the protection mechanism meant for explosion cases. UX: slow or failed decomposition steps.

**Gap vs SKILLs**: Timeout + backoff present but no rich per-decision `LogAgentReasoning` visible in streams for the rate limit path (server.go:290 logs the hit, but agent reasoning trails for "why this plan is throttled" missing).

### 2. Embeddings / Model Substrate Gaps
- Dozens of `embeddings routing failed: ollama embed error HTTP 404: model "nomic-embed-text" not found, try pulling it first`.
- Hits on multiple request_ids during the window.
- Blocks any code-intel or RAG-dependent paths (Elastro relies on embeddings upstream).

### 3. Tool Usage & Code Intelligence (Elastro + Logloom) — Registered but Invisible
- Code (handlers.go:290+, runner.go:125, 307): `NewToolRegistryWithElastro`, `elastro_query_ast` executor (shells to `elastro rag query`), `logloom_ast_query` (direct ES on logloom indices), implementer skeleton prefers elastro first then logloom, startup registration logs.
- Live: **Zero evidence** in gateway streams of `elastro_query_ast`, `logloom_ast_query`, or any tool calls. Every chat request: `"tools":0`.
- No before/after `LogAgentReasoning` blocks for tool execution (violates flume-go SKILL mandatory dual observability on tool paths).
- Implementer short-circuit or role-specific paths (non-code roles?) not injecting tools.

**Positive**: Registration and skeleton exist (Phase 3.1 landed). Gap is activation + reasoning emission + gateway tool injection for all roles.

### 4. Dashboard / Work Queue Observability Blind Spots
- `_system_analytics` every ~5s (stable, efficient per reasoning): 
  - `elastro_data_present:false`
  - `telemetry_doc_count:0`
  - `savings:0`
  - `worker_entries:0`
  - `historical_burn_len:0`
  - Explicit note: "Tokens saved by Elastro AST-aware compression vs naive full-file baseline. (Elastro instrumentation only.)"
- Telemetry bridge to `/api/gateway-metrics` succeeds (VRAM/node data restored).
- User opening reasoning drawers for real tasks (positive UX signal; queue is active).
- No correlation of plan-91a43783a9df or its tasks to Elastro/Logloom usage or savings.

**Gap**: Phase 3 / reliability plan called for rich queue-lifecycle events + observability. Elastro side of the project lifecycle (rag ingest at creation, rag update after changes, logloom at creation) is not wired into dashboard analytics or agent telemetry.

### 5. Work Queue Health on the Monitored Successful Small Decomp
- Positive: User explicitly confirmed "the stack has been rebuilt and a simple documentation update request has been broken down successfully into a small set of workitems: 1 epic, 2 features, 2 stories, 3 tasks."
- Tasks visible (reasoning drawers opened, telemetry sync for e.g. task-2).
- No explosion observed in this run (contrast to historical v0.1.126 Python behavior and the 9-gap report).
- Still: PM decomposition steps for this plan were rate-limited and fallback-thrashed.

This validates that small-plan fastpath + hierarchy works for doc updates, but the LLM "orchestrator" (PM) is the bottleneck even here.

### 6. Other Observed Issues
- High log noise from repeated analytics + config_refresh (every ~30-60s).
- Some successful "synchronized execution telemetry to ES dynamically".
- No worker lease/claim/tool reasoning captured in the short-lived worker monitors (recommend re-run with pure `docker logs -f` background without complex grep pipe for full column mutation trails).

---

## Comparison to Previous Phase 3 Implementation Plan

**Reference Plan**: `docs/designs/plan-new-work-queue-reliability-fixes.md` (full "Plan New Work + Work Queue Reliability Fixes", generated 2026-05-27 from live logs + static analysis of api_intake, sweeps, runner, types). This is the authoritative prior Phase 3 / reliability design (P0 hierarchy deadlock, no server-side guardrails on workitem count, shadow TaskStateMachine, expensive ID sequencing, sweep overhead, planning LLM cost, UX friction, weak observability). Later column-by-column + thundering herd + ToolRegistry design (plan.md in prior context) layered the Go engineering standards (SKILLs) on top.

**What the Prior Plan Called For (excerpted)**:
- P0: Server-side cap + warning on commit (MAX_LEAF_TASKS), flip TaskStateMachine to strict (non-prod), tests for promote with hierarchy.
- Phase 1: Decide/document hierarchy contract (org items structural only vs executable), fix `promotePlannedTasks` + `parentCompletionSweep` (ignore parent status for leaves or make parents active), unit/integration tests.
- Phase 2: Hard/soft limits + UI warning, ID sequencing perf (cache/CAS), sweep efficiency (per-repo + watermark), planner-specific routing/model.
- Phase 3: Post-commit deep-link + progress, queue visibility (blocked-by, promotion eligibility, shadow violations), better errors (blocked state + retry), queue events endpoint.
- Phase 4: E2E tests exercising full hierarchy, perf tests, doctor/docs updates, feature flag, dashboard panels for commits/workitems/latency/shadow rate.
- Cross-cutting: Extensive logging of promote/queue decisions; correlation from plan session → tasks.

**What Landed / Current State (from monitoring + code inspection)**:
- **Strong alignment on Go worker/lease side (later Phase 3 refinement)**: `updateTaskLeaseState` central wrapper + Enforce + unconditional dual `Log*` (runner.go:33-47, 1293+ — directly addresses "Cross-cutting Writer Rule", thundering herd via OCC/seq+prim, no bare UpdateDoc on lease columns). ToolRegistry + real Elastro executor + logloom direct ES + implementer skeleton with "elastro first" (Phase 3.1 per prior user directive). Per-plan PM rate limiter in gateway (server.go:285, references Phase 2 but used in decomp). Git per-cmd timeouts + classifyGitError + OpenBao hardening (credentials.go, runner.go checkout paths with pre-clone validation + refusal of raw ES secrets).
- **Partial on hierarchy/guardrails**: Small successful decomp (1+2+2+3) suggests fastpath/hierarchy for doc updates now works without explosion (user confirmed). No evidence of the old sibling `planned` deadlock or massive over-decomp in this run. Server-side cap not obviously firing (or not hit on this tiny case).
- **Observability**: Rich reasoning + LogAgentReasoning sites present in code (runner, handlers). Dashboard analytics running efficiently but completely blind to Elastro (the exact observability gap the plan flagged in "Log noise vs signal" and "Queue visibility").
- **LLM/PM substrate**: The plan assumed stable planning/PM LLM paths. Monitoring proves this was underestimated — even tiny successful decomps trigger the rate limiter + fallback storms. No planner-specific model/routing or intake tagging visible as mitigations in streams.
- **Tool + Code Intel**: Plan called for better observability; current Phase 3.1 added the tools + skeleton + mandatory reasoning. Live monitoring: tools not injected (`tools:0`), no execution reasoning, no telemetry to dashboard. Project lifecycle (elastro rag ingest/update + logloom at creation) not closed.
- **Gaps vs Plan**: No visible shadow violation surfacing or queue events endpoint in monitoring. ID sequencing / sweep perf not stressed in small-plan run. E2E validation for hierarchy not contradicted but not proven in this 10min window. Dashboard panels for workitems/commits/latency missing Elastro data.

**Key Underestimation in Prior Phase 3 Plan**:
- Local LLM mesh (Ollama multi-node + embeddings + per-plan limiter) is far more fragile under real (even small) PM decomposition load than assumed. Rate limiter protects the queue but creates self-inflicted 429/context-cancel UX for legitimate small plans.
- Tool activation + rich reasoning emission + telemetry loop closure to dashboard lags behind registration and skeleton code.
- "Successful small decomp" (user's explicit positive signal) still exercises the exact failure modes (PM rate limit + fallback) the reliability work targets — proving the LLM substrate must be hardened in parallel with queue columns.

**What the Plan Got Right (and is paying off)**: Focus on central guarded mutators, dual observability, anti-explosion budgets, hierarchy contract clarity, and test-driven promotion fixes. The fact that a real doc-update plan decomposed cleanly into the exact small hierarchy the user described, without workitem explosion, is evidence the queue-side Phase 3 work is directionally effective.

---

## Actionable Prioritized Recommendations

### Tier 1 — Stabilize LLM Substrate + Tool Visibility (This Week, Before More Phase 3 Column Work)
1. **Per-plan PM rate limiter hardening** (gateway/server.go + runner PM path):
   - Emit rich `LogAgentReasoning` (with plan_session_id, current_count, cap, backoff_ms, decision) on every hit/throttle/allow.
   - Add jitter + short-term per-plan burst allowance for small plans (or dynamic cap based on plan complexity from intake).
   - Surface rate-limit events in dashboard queue analytics + agent reasoning for the affected plan.
2. **Fix embeddings**: `docker exec` or orchestrator pull `nomic-embed-text` on gateway/worker nodes; make model configurable with safe default + fallback; log at startup.
3. **Force tool injection + reasoning for all roles**:
   - In gateway chat dispatch / provider paths: always include registered tools (elastro_query_ast, logloom_ast_query) for pm/implementer/reviewer/tester unless explicitly disabled.
   - Enforce before/after `LogAgentReasoning` in ToolExecutor (handlers.go) and runner implementer loop.
   - Add "tools_injected" + "tool_results" to chat request/reasoning logs.
4. **Close Elastro telemetry to dashboard**:
   - Wire rag ingest/update + query counts + AST savings from Elastro into the telemetry bridge / _system_analytics path (or dedicated ES docs).
   - Change "Elastro instrumentation only" to real numbers once data flows.
5. **Re-run logloom + doctor** after each Tier 1 change; add queue-lifecycle event index if not present.

### Tier 2 — Queue Observability & Hierarchy Guardrails (Next 1-2 Weeks)
- Surface promotion eligibility, last-sweep reason, shadow violations in UI + /api/queue/events?plan=...
- Implement server-side MAX_LEAF_TASKS cap + UI warning (plan P0/Phase 2) if not already strict.
- Add plan-session correlation to all task/lease/reasoning events.
- Strengthen parentCompletionSweep + hierarchy tests per design doc.

### Tier 3 — Full Phase 3 Column/Herd Completion + Validation
- Complete remaining Work Queue column frameworks (intent/bounds/writers/anti-herd per the plan.md design) with central wrappers + dual logs.
- Thundering herd: per-plan jittered workqueue + renewal + K8s-style patterns.
- E2E exercising the exact 1E+2F+2S+3T pattern + medium (6-12 leaf) plans.
- Dashboard panels for commits/workitems/p95 latency + Elastro savings.
- Feature flag for v2 hierarchy/strict mode.

**Success Criteria (updated from design doc)**:
- Small legitimate plans (like the monitored doc update) complete PM decomposition in <30s with 0 rate-limit hits and visible tool usage (elastro first).
- Dashboard shows real elastro_data_present:true + positive savings for plans using code tools.
- 0 nomic-embed 404s; all agent chat requests have tools>0 with rich reasoning.
- Full hierarchy promotion for 6+ leaf plans reaches all done without stuck planned siblings.
- Reproducible in fresh `flume doctor --logloom` + 10min monitor run.

---

## Next Immediate Actions (for this session / follow-up)
1. (Done in this monitoring) — 10min streams + explicit logging of all LLM/tool/queue issues.
2. Start Tier 1 #1-2 (rich reasoning on rate limiter + pull nomic-embed-text).
3. Re-run full monitoring post-fix; compare before/after on another small plan.
4. Read full gateway/dashboard monitor output files for any missed patterns (paths provided in tool results).
5. Continue Phase 3 column work only after LLM substrate stable (per original "design before code" + SKILL context discipline).
6. Update this report with new monitor events as they arrive.

**Files for follow-up edits** (per SKILLs + plan):
- src/gateway/server.go (rate limiter + tool injection + reasoning)
- internal/worker/{runner.go,handlers.go} (enforce tool reasoning, lease wrapper)
- internal/dashboard/api_system.go + api_tasks.go (Elastro telemetry, queue events)
- docker-compose / orchestrator (embed model pull, gateway health)

This report is grounded directly in the live 10-minute monitoring data on the user's successfully decomposed documentation update plan, cross-checked against the prior reliability/Phase 3 design doc and current code. It is intentionally actionable and efficient — Tier 1 items unblock the exact symptoms observed.

---
*Report synthesized from monitor events (gateway/dashboard ~6-10min continuous), code (runner.go:33-380+, handlers.go:290+, gateway/server.go:285+), and docs/designs/plan-new-work-queue-reliability-fixes.md. SKILL adherence verified on all observations and recs. No raw secrets or PII in logs.*

**Status**: Monitoring complete. Report delivered. Ready for Tier 1 implementation or user direction on next slice.