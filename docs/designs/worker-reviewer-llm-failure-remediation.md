# Remediation Plan: Post-Migration Worker Reviewer LLM 404 Loops + Role/Status Reset Storms

**Date**: 2026-06 (current on `feature/workitem-explosion`)
**Context**: After Python→Go worker migration (internal/worker/*, ~2k LOC port of runner/claim/sweeps + LLM client), agent reasoning popout (LogAgentReasoning + execution_thoughts bridge in logger.go:386) now surfaces the exact failure signatures the user observed:
- "Worker reviewer failed: llm: legacy Ollama HTTP 404: 404 page not found..."
- "Worker (unknown-role) crashed or LLM call failed; stale claim cleared and task reset to ready..."
These loop until a task is externally forced into `review` (or equivalent "In Review"), at which point more review-consensus children + top-level workitems spawn via implementer success paths + PM decomp.

**Why the popout was decisive**: It (plus the new LogStateTransition sites) made the reset/spawn causal chains visible without log grepping. The prior "empty reasoning" bug hid exactly these loops.

## Root Cause Analysis (with code pointers)

### P0 — The LLM 404 Cascade (every reviewer + PM + tester worker)
1. **Gateway default is Docker-only** ([internal/llm/client.go:143-146](/Users/jonathandoughty/clients/fremenlabs/flume /flume/internal/llm/client.go#L143)):
   ```go
   gatewayURL := os.Getenv("FLUME_GATEWAY_URL")
   if gatewayURL == "" {
       gatewayURL = "http://gateway:8090"  // !!!
   }
   ```
   - `gatewayAvailable` (317) does GET /health. Fails in native → `legacyChat` (512).
   - `legacyChat` → `resolveOllamaBaseURL` (686) checks `LOCAL_OLLAMA_BASE_URL` / `LLM_BASE_URL` / `LLM_HOST` (adds :11434).
   - In native mac runs (or mis-set envs from wizard/docker), this often resolves to a non-Ollama HTTP server (dashboard :8080?, frontend, Caddy 404 page, etc.) → exact "legacy Ollama HTTP 404: 404 page not found".
   - Same path hit by handleReviewer:256, handlePM, handleTester, etc.
   - Supervisor (cmd/flume/services/supervisor.go:69-107) starts gateway + dashboard + workerMgr in-process but **never sets FLUME_GATEWAY_URL** (or GATEWAY_URL) to "http://localhost:8090" for the llm.Client singletons created inside NewManager (231) and Claimer/Sweeper.

2. **Native vs. container env contract was never reconciled** after the Phase 5 "one binary" unification. Python workers had separate env injection; Go in-process goroutines inherit the parent flume process env (which the start wizard only partially populates for Docker paths).

### P0 — The "unknown-role" + Wrong-Status Reset Loop (amplifies everything)
3. **clearStaleClaim heuristic is dead** ([internal/worker/runner.go:925-957](/Users/jonathandoughty/clients/fremenlabs/flume /flume/internal/worker/runner.go#L925)):
   ```go
   targetStatus := "ready"
   if currentStatus == ftypes.TaskStatusReview { targetStatus = "review" }
   ...
   flumelogger.LogAgentReasoning(..., "unknown-role", fmt.Sprintf("... reset to %s", targetStatus) ...)
   ```
   - Called from RunWorker error path (123) **after** the claimer has already mutated the task to `status=running` (claim.go:281).
   - Therefore the `if currentStatus == "review"` **never fires**. All LLM crashes (reviewer or otherwise) reset to `ready`.
   - Hardcoded "unknown-role" because `clearStaleClaim` takes only `(taskID, status)` — no worker/role passed (RunWorker has `worker ftypes.Worker` at L57 and the error site L115).
   - Result: reviewer tasks (created with WorkerRole=reviewer, status=review in spawnReviewTasks:1268) bounce:
     - claimer (for reviewer role) only ever queries `status=="review"` (claim.go:432, roleToTargetStatus)
     - stuck-implementer sweep (sweeps.go:192) unconditionally `running`→`ready`
     - clearStale always →`ready`
   - When a "review" status task finally gets claimed (by sweep clearing phantom or manual move), LLM fails instantly → reset to ready → loop. Log shows both messages on every iteration.

4. **Claimer + sweeper role awareness is incomplete**:
   - processWorker has a second copy of roleTarget map (manager.go:303-311) that is not authoritative.
   - No "reset to the status appropriate for this task's worker_role" path.
   - WorkerRole is set on claim (claim.go:285) but never read in reset paths.

### P1 — Explosion Amplification Under Failure
5. **Implementer stub + spawn still fires** (runner.go:81-93):
   - handleImplementer does **no LLM coding** (comments admit "Phase 3"), just EnsureBranch + AutoCommit (whatever is on disk) + push → always returns NextStatus=Review for code tasks.
   - Then the post-switch block spawns reviewer+tester children (unless title contains "review"/"test").
   - Under the reset storm, implementer workers occasionally claim orphaned "review" tasks (now sitting in "ready") → they "succeed" → spawn **more** review children for parents that already have them (the 3x guards in spawnReviewTasks:1192-1251 are good but title/role mismatches + concurrent reset windows still leak).
   - PM decomp keeps feeding new ready tasks → more implementers → more spurious review spawns when the parent hits the success path.

6. **No backpressure / fast-fail for bad LLM config at worker start**; legacy fallback is silent and too broad.

## Prioritized Remediation Plan (8-12 hours of focused work)

### Phase 0 (30 min) — Fresh Observability Baseline
- [ ] Run `logloom build --source . --languages go,python --output flume-robust-ast-graph.json --git --call-graph --tags` (and commit the updated graph + any new nodes for `clearStaleClaim`, `legacyChat`, `handleReviewer`, `roleToTargetStatus`, `spawnReviewTasks`).
- [ ] Re-run `flume doctor --logloom` and `logloom graph find "clearStaleClaim|legacy Ollama|unknown-role"` to confirm new nodes appear. This gives the popout + future agents the exact call-graph edges for the fixes below.
- [ ] Add a permanent doctor gate (already sketched in doctor.go:466) that asserts >0 nodes for "reviewer" + "clearStaleClaim" + "FLUME_GATEWAY_URL".

### Phase 1 (2h) — Kill the 404 at the Source (Native Gateway Wiring)
1. **Make LLM client native-aware + fix default** ([internal/llm/client.go](/Users/jonathandoughty/clients/fremenlabs/flume /flume/internal/llm/client.go)):
   - Change New() default: if `FLUME_NATIVE_MODE=1` or `FLUME_GATEWAY_URL==""`, default to `http://localhost:8090` (or read from a new `GatewayURL` field in internal/config).
   - Better: pass gateway URL explicitly from Supervisor (it already knows the addr at L268) into NewManager → NewRunner/Claimer/Sweeper → llm.NewWithGatewayURL(...).
   - Also set `os.Setenv("FLUME_GATEWAY_URL", s.GatewayURL())` early in supervisor.StartAll so any legacy paths or other clients (api_intake.go:253 etc.) see it.
   - Update resolveOllamaBaseURL and related docs for native mac (direct 127.0.0.1:11434) vs. container (host.docker.internal).

2. **Add a loud startup check** in worker manager cycle (or NewManager): attempt a 2s gateway /health; if failing and no usable Ollama base URL, log a FATAL-style "LLM path misconfigured for native mode — reviewers will 404-loop. Set FLUME_GATEWAY_URL=http://localhost:8090 or run with docker." + emit agent reasoning event.

3. **Parallel quick win**: in gatewayAvailable, on failure also log the exact URL that was tried (currently silent after first cache miss).

### Phase 2 (2h) — Make Stale-Claim Role-Aware + Eliminate "unknown-role"
1. **Signature change + logic fix** (runner.go + callers):
   - `clearStaleClaim(ctx, taskID string, workerRole string, currentStatus TaskStatus)`
   - Inside: 
     ```go
     targetStatus := "ready"
     switch workerRole {
     case "reviewer", "tester": targetStatus = "review"
     case "pm": targetStatus = "planned"
     }
     // also look at task.WorkerRole as fallback if role==""
     ```
   - Update the single callsite in RunWorker:183 (pass `worker.Role`).
   - Update the LogAgentReasoning call (950) to use the real role.
   - In the error log path (115), the existing one already has the role — keep it.

2. **Harden the stuck sweeps** (sweeps.go):
   - `requeueStuckImplementerTasks`: instead of hardcoding "ready", inspect `worker_role` (or title prefix) on the hit and choose the correct target status (or delegate to a new `ResetTaskForRole` helper that lives in one place).
   - Same for `requeueStuckReviewTasks` (currently only touches status=review anyway).
   - Add a unit test (worker_test.go already exists) that creates a review-status task, simulates LLM crash via runner, asserts it lands back in "review" not "ready".

3. **Claimer safety**: in TryAtomicClaim and roleToTargetStatus, also allow "ready" tasks that have `worker_role=reviewer|tester` (for recovery of the orphaned ones created by the current bug). This is a migration bandage.

### Phase 3 (1.5h) — Resilience & Anti-Loop Hardening
1. **Lightweight retry inside handlers** (or in llm.Chat for non-permanent errors): for reviewer/tester (simple single-turn), retry once with 5s backoff on 4xx/5xx from legacy or gateway. Log the retry via LogAgentReasoning so popout shows "LLM transient 404, retrying".
2. **Fast-fail + poison the task** on persistent legacy 404: after 2 failures, set task to `blocked` (or a new `llm-config-error` status) with clear `last_error`, instead of infinite ready/reset. This stops the log spam and spawn amplification.
3. **Strengthen spawnReviewTasks guards** (already excellent):
   - Also key the "already have children" check on `worker_role` + `parent_id` (not just title strings).
   - On any spawn attempt for a parent that already has 2+ review/test children, force the implementer result.NextStatus to ReviewConsensus **without** re-spawning, and emit reasoning.

4. **Implementer stub awareness**: add an env guard or complexity check so stub implementers do **not** auto-spawn review children until real codegen lands (or at least require an explicit "changes committed" marker). Prevents the "empty diff → review loop" fuel.

### Phase 4 (1h) — Tests + Gates + Observability
- Add integration test in worker_test.go or e2e that:
  - Starts supervisor in native mode (with a fake gateway or stubbed llm client).
  - Creates a review task.
  - Forces a legacy 404 (by setting bad LLM_HOST).
  - Asserts: exactly one "Worker reviewer failed" + one state transition to the *correct* target status, no "unknown-role", task not left in ready, no extra children spawned.
- Promote the existing logloom doctor gate to a hard CI assertion (find nodes for the 6 new anti-explosion symbols).
- Add a metrics counter in runner for "llm_failure_resets_by_role" (expose via /metrics or gateway).
- Update troubleshooting.md and the agent reasoning popout docs with the exact signatures and "first thing to check: FLUME_GATEWAY_URL in native".

### Phase 5 (30min) — Backfill & Migration
- One-time ES script / sweeper to repair any orphaned review-flavored tasks that are currently in "ready" (set status=review + worker_role if missing).
- Add a "worker role mismatch detector" sweep that logs + reasons when a task's worker_role doesn't match the status bucket it sits in.

## Verification Checklist (after each phase)
- `flume doctor --deep --logloom` passes with 0 "unknown-role" or legacy-404 suggestions.
- Fresh logloom graph contains nodes for all reset paths + the new "LLM path misconfigured" warning.
- Local native `flume start` + create a small plan → implementer completes → exactly 1 reviewer + 1 tester child created → both complete (or blocked cleanly on real LLM error) → parent reaches review-consensus/done. No loops in logs or popout.
- Under injected gateway-down, reviewer tasks go to blocked (not infinite reset).
- No title-string hacks remain as the sole dedup mechanism.

## Non-Goals / Follow-ups
- Full implementer LLM loop (Phase 3 of original port) is out of scope but the spawn guards make it safe to land later.
- Multi-node gateway failover for legacy path.
- Replacing the brittle JSON `approved` parser in reviewer with a proper schema tool call (nice-to-have once tools are reliable in this path).

This plan directly attacks the two user-reported signatures, closes the role/status feedback loop that was invisible before the popout fix, and prevents the "move to In Review → more tasks spawn" amplification. Executing Phase 1+2 first will make the swarm usable again on native macOS.

**Owner**: (to be assigned)
**Branch**: feature/worker-llm-native-fix (stack on top of workitem-explosion)
**Risk**: Low — mostly localized to llm/client + runner clearStale + one new helper. Existing anti-explosion guards stay in place.
