# Plan New Work Monitoring Report - 2026-06-03 (post Phase 3/4 + conn/auth fixes)

## Trigger
- Time: ~2026-06-03T20:45:40Z
- Prompt: "ensure all CLI commands are detailed in the documentation" (pure-doc, should skip RAG)
- Repo: "rflow"
- Session: plan-0a7b7e0a7995
- Expected from optimizations: real draft in <120s (or quick placeholder), on local mesh only (qwen3.5:35b), no explosion, status progresses requesting_plan -> ready, node telemetry surfaced.

## Monitoring Method
- 250s window using polls to /api/intake/session every ~5s + docker logs snapshots for gateway (filtered on request_id 5d717d489058f6c2 and planning keywords) + dashboard.
- ES direct queries for session doc.
- Background monitors started (some had shell quoting limits in harness but key events captured via snapshots and one persistent log tail).

## Key Timeline / Events Captured
- 20:45:40: POST /api/intake/session succeeded (200), session created in ES with stage="queued".
- 20:45:40: Dashboard log: "RAG skipped for pure-doc prompt (Phase 3...)" + agent reasoning for plan-rag-skip-rflow. Good.
- 20:45:40: Gateway: per-plan-pm rate limit (allowed), config_refresh (271ms), "incoming chat request", "awaiting ollama slot", "routing request" (model qwen3.5:35b-a3b, intake).
- 20:45:40: Status in ES/API: stage="requesting_plan", elapsed=0, lastUpdated initial time. (set before LLM call)
- ~20:47:40 (exactly ~120s later): Gateway log: "operation completed" ollama_chat_stream duration_ms:119728.859
- 20:47:40: Gateway ERROR "chat failed" "ollama request: Post \"http://192.168.0.227:11434/api/chat\": context canceled" duration_ms:120001.046
- No logs seen for: "primary node failed for planning task", "trying remaining healthy mesh", "exhausted local mesh for planning task — escalating to frontier", secondary attempts, or successful response.
- Throughout 250s window (polls up to ~16:50 / 20:50Z ~5+ min after trigger): API and ES consistently report stage="requesting_plan", elapsed=0, lastUpdated=20:45:40 initial. No draftPlan, no stage=ready or failed with placeholder.
- Gateway metrics during: showed conn stats for the synth 'ollama-192.168.0.227:11434' (keepalive=1, max_idle=20) -- Phase1 conn mgr active and used.
- Dashboard logs: only GET polls for sessions (including this one and another), the RAG skip at trigger time. No error logs or "LLM ... failed" from the intake handler for this POST. Handler appears still blocked.
- No task creation in queue (no agent-task-records for this session observed in polls).
- Previous similar plan (different id around 20:44) also showed ~120s ollama_chat_stream then context canceled + chat failed.

## Analysis vs Expectations
- **Positive**: RAG skip worked (Phase3). Conn manager wired and used for the node (Phase1/4 stats visible). Planning used the registered mesh node (192.168.0.227 not localhost). Rate limit and routing reached. Call was non-stream as expected for intake Chat.
- **Failure**: Plan New Work did NOT complete. Stuck indefinitely in "requesting_plan" elapsed=0 (no progress after 5+ min, well past 120s target or 180s resilience budget). No real draft or even placeholderPlan written to ES. No queue work items. No "ready" stage.
- The gateway side for the primary attempt did exactly the "full 120s burn then cancel" that previous phases aimed to prevent (the ollama call took the full budget, "context canceled" on the Post to node).
- No visible recovery in planning resilience (no secondary node try, no frontier escalation logs for this request_id). The error path in multi_router.executePlanningWithMeshResilience or routeToNode did not produce expected "planning_router: primary node failed..." etc. (perhaps ctx cancel short-circuited or logs lack request_id).
- End-to-end from dashboard: the llmClient.Chat never returned (intake handler blocked, never reached the post-LLM status update to "ready" + placeholder + ES index + response). Even after gateway had errored at 120s.
- This matches historical symptoms ("400s+ plan", "stuck requesting_plan elapsed=0 even on mesh success", "primary failed... trying remaining", "exhausted... escalating to frontier").
- Root hypotheses:
  1. llm client (postGateway) + intake handler ctx propagation: the 120s reqCtx in post + outer handler ctx led to permanent block; error from gateway not unblocking the Do or the client didn't read the error response (perhaps write from gateway failed due to client ctx or connection state after long open).
  2. Gateway planning resilience not robust to "context canceled" from child attempt ctx (the 90s per-attempt or 180s overall); didn't proceed to fresh ctx secondary or frontier, or the final err not written back to the dashboard caller.
  3. The node call itself (to 192.168.0.227:11434 qwen35b) is slow for this structured planning prompt (even with RAG skip, conn reuse, stream); took full budget instead of <60-90s target. Perhaps no keepalive effective, or model load, or network from gateway container to that LAN IP (host.docker.internal issues?).
  4. No status "heartbeat" updates during the long LLM (only before/after), so if blocked, UI sees stale requesting_plan 0 forever.
  5. WriteTimeout 300s is set, but perhaps not sufficient or not the issue (handler is blocked on downstream read, not write yet).
- No task explosion (good, because never completed to commitPlan).
- All attempts were local mesh (no frontier in this case).

## Data Sources
- API polls (session status, elapsed, stage).
- Docker logs (gateway filtered on request_id + keywords; dashboard).
- Gateway /api/gateway-metrics (conn stats showed manager in use for the node).
- ES direct (plan-sessions doc).
- 250s window covered multiple 120s cycles.

## Plan to Fix "Once and For All" (Phased, SKILL-aligned, no breaking)
Follow flume-go (rich LogAgentReasoning on every decision/fail/hop, propagate ids, role-aware, no silent legacy) + reliable-go (ctx first, explicit errs, bounded timeouts on ALL I/O, reconciliation for status, observability, idempotent, verification loops, small units).

**Phase A: Immediate Resilience in Intake Handler (make long LLM non-blocking for status)**
- In handleIntakeStartSession (and refine), set "requesting_plan" + initial update BEFORE LLM.
- Launch the llmClient.Chat in a goroutine with result chan + err.
- Use select { case res := <-ch: use it; case <-time.After( the 120s + margin ): set placeholder, update ES to "ready", log rich reasoning "LLM timeout in intake handler - using placeholder", respond with current session. }
- Always update ES with final status (ready/placeholder or error) even on panic/recover, using defer.
- Add "heartbeat" : before LLM, launch a bg goroutine that every 15s updates ES with current "requesting_plan" + "elapsed_so_far" + "last_heartbeat" (so UI sees progress even if main handler blocked).
- Use dedicated ctx with deadline for the LLM call (context.WithTimeout(..., 130*time.Second)) separate from handler ctx.
- Emit LogAgentReasoning at start of LLM, on timeout, on success/fail with node/telemetry if available.
- This ensures UI never sees stuck "requesting_plan 0", and user gets usable (placeholder) plan quickly.

**Phase B: Harden llm.Client.Chat / postGateway for Planning/Intake (error propagation, ctx, fast fail)**
- In postGateway, on any err from Do (incl cancel/deadline), always cancel reqCtx, and ensure error is returned without leaking goroutines.
- For planning (TaskType=="planning" or AgentRole=="intake"), use shorter hard timeout or no retry loop (fast fail to legacy or placeholder), per comment in code about burning time on retries.
- Make the outer ctx in Chat respected for the whole (including legacy fallback).
- Add explicit LogAgentReasoning in llm client on gateway fail, fallback attempt, legacy use, with plan_session_id, duration, err.
- In intake error path, always set placeholder + update ES + return success response with the session (don't let err bubble to 500).
- Verify with test that injects slow gateway and checks status ends in ready/placeholder within budget + reasoning emitted.

**Phase C: Make Gateway Planning Resilience Bulletproof (always try, always log, fresh ctx, bounded)**
- In executePlanningWithMeshResilience (and executeLocalOnly), wrap each routeToNode in its own defer/recover to ensure next attempts happen even on panic/cancel.
- Always log (using the request_id + plan_session) on primary fail, before/after each secondary attempt, on exhaust, on escalation. Include the err type (canceled vs real).
- On "context canceled" from child, treat as "attempt timeout", proceed to next with fresh child ctx (don't let parent cancel poison the handler).
- Add overall hard deadline for the planning response write (use a separate timer goroutine to force write error if not done).
- Surface the "why" (node, duration, err) in the returned error or telemetry so llm client and intake can log it.
- For intake specifically (high value), prefer "top node only + fast fail to placeholder" over full resilience if we detect it's intake (to hit <120s target reliably).
- Wire conn stats / node health into the routing decision more (skip known slow/degraded nodes for planning).
- Add unit tests for the resilience paths with mocked slow/cancel nodes.

**Phase D: Observability & "Heartbeat" for Long Operations (Phase4 extension)**
- During long LLM in gateway (for planning), periodically update a "inflight" doc or use the existing per-plan rate limit to heartbeat "still working on node X, elapsed Y".
- In dashboard intake, the slow check and reasoning already there; extend to always attach the final telemetry/node to the planningStatus (Provider/Model/Host/BaseURL) even on error path (from the err or last known).
- Expose in /api/gateway-metrics or new /api/intake/status the in-flight planners with node/dur.
- Update UI (planningStatus) to show "in progress on node X for Z s" based on lastUpdated + elapsed.
- Rich LogAgentReasoning on every hop/fail/timeout with full ids (plan_session, request_id, node, model, dur, err).

**Phase E: Node/Conn Health & Perf (make the 35b call actually fast)**
- Health checker / node registry: mark node degraded on repeated 120s cancels/timeouts for planning, lower its score for future planning selects.
- Ensure conn manager lifecycle: on node health change to degraded, CloseIdleForNode.
- Investigate the specific node 192.168.0.227 (is it reachable reliably from gateway container? Use host.docker.internal if co-located? Test with /api/tags probe from gateway).
- Add per-node timeout tuning or model-specific for planning (smaller num_predict for planner?).
- Verify with 250s monitor on same prompt after fixes: should see <90s success or quick placeholder, multiple attempts logged if needed, status ready with draft or placeholder, tasks created, reasoning with node info.

**Verification (SKILLs)**
- todo sub, rich reasoning on all new paths.
- go test -race on gateway + dashboard intake tests (add cases for timeout LLM, canceled ctx, placeholder path).
- Rebuild, 250s monitor on the exact prompt + "simple doc task", capture before/after metrics (time to ready, elapsed, node used, task count <10, no frontier unless intended, conn reuse in metrics).
- Update docs/reference/local-llm-mesh-optimization-report.md and phases.md with this run's data + "fixed in Phase X".
- E2e in logloom-local-llm-test or new script.
- Bounded: all changes explicit ctx, timeouts on every external (http to gateway/node, ES writes), no fire-forget (use errgroup or wait for goros).

**Risks/Mitigation**
- Over-fallback to placeholder too often: tune the intake timeout higher than node attempt (e.g. 130s handler budget).
- Breaking existing: gate new timeout/heartbeat behind the existing TimeoutSeconds, preserve current success path.
- Perf: the heartbeats add ES writes, but only for long planning (>15s), bounded.

This plan addresses the root (handler blocking + incomplete error recovery in cross-component long LLM + missing heartbeats) "once and for all" while building on the conn/stream/fastpath work already done. Small PRs per phase, verification loops until 0 issues in 250s repro.

