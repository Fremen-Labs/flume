# Remediation Plan: Post-Migration Worker Reviewer LLM 404 Loops + Role/Status Reset Storms

**Date**: 2026-06 (current on `feature/workitem-explosion`)
**Latest implementation pass**: commits `6dbe8956`, `06b4c4fe`, `44b185dc` + uncommitted overlay on `feature/workitem-explosion` (reviewed 2026-05-30).
**Context**: After Python→Go worker migration (internal/worker/*, ~2k LOC port of runner/claim/sweeps + LLM client), agent reasoning popout (LogAgentReasoning + execution_thoughts bridge in logger.go:386) now surfaces the exact failure signatures the user observed:
- "Worker reviewer failed: llm: legacy Ollama HTTP 404: 404 page not found..."
- "Worker (unknown-role) crashed or LLM call failed; stale claim cleared and task reset to ready..."
These loop until a task is externally forced into `review` (or equivalent "In Review"), at which point more review-consensus children + top-level workitems spawn via implementer success paths + PM decomp.

**Why the popout was decisive**: It (plus the new LogStateTransition sites) made the reset/spawn causal chains visible without log grepping. The prior "empty reasoning" bug hid exactly these loops.

## Implementation Status After First Pass (reviewed 2026-05-30)

The three commits + local edits land the large majority of Phase 1 and Phase 2 (core P0 fixes):
- Role-aware `clearStaleClaim` (now takes `workerRole`, switches on reviewer/tester/pm, targets "review"/"planned", no more "unknown-role" in reasoning logs).
- Supervisor now exports `FLUME_GATEWAY_URL` (and `FLUME_NATIVE_MODE`) before worker manager starts; `llm.Client` has native localhost default + "direct Ollama" fallback path + 3s delayed healthcheck + persistent 404/blocking after N failures.
- `handleRoleLLMFailure` + caps for reviewer, `isPersistentConfigError` fast-path to blocked.
- Strengthened spawn guards (including new `errSpawnGuardFired` sentinel), intake hierarchy anti-explosion, PM skip for system nodes, git lock cleanup, execution_thoughts nested mapping fix, backfill script (exists, but see review findings).
- Many rich LogAgentReasoning / LogStateTransition sites added on the exact failure/reset paths.

**Independent review performed** (full artifacts below):
- Review diff collected for the exact 3 commits + uncommitted runner.go overlay vs. merge-base with main.
- Full review + summary written by dedicated reviewer subagent (cross-checked every changed file + the original plan).
- 11 issues found (7 of severity **bug**). Core reported signatures are fixed in nominal/native happy paths. However, several subtle races and incomplete items can still recreate loops under concurrent claim/reset/spawn/ES contention — exactly the conditions that produced the original 74- and 258-item explosions.

See the review outputs:
- Full structured review: `/tmp/grok-review-640e38c5.md`
- Executive summary + top issues + recommended next actions: `/tmp/grok-review-summary-640e38c5.md`

**Key remaining high-severity gaps** (prioritized from the review; see full review for all 11 + line numbers):
1. Supervisor gateway export race (supervisor.go:88) + llm.New still depending on racy env + FLUME_NATIVE_MODE flag → can still hit the Docker default and legacy 404 on some native starts.
2. Status fight: `handleRoleLLMFailure` writes "blocked" after 3 failures; outer RunWorker error path then calls `clearStaleClaim` which overwrites it back to "review" for many error strings.
3. Claimer still has no "ready + worker_role=reviewer/tester" recovery query (plan Phase 2.3 not implemented) → pre-fix orphans stay stuck forever.
4. Backfill script only blocks; does not repair to correct status+role (contradicts plan Phase 5).
5. Multiple bare `UpdateDoc` (no error handling, no OCC, no reasoning emission) on critical status paths + parallel reset paths in sweeps that bypass the new central role-aware logic.
6. Persistent error detection is brittle string matching; no typed sentinels or configurable caps for reviewer path.
7. Many Phase 0/4 checklist items (logloom rebuild + doctor gates, reset-status unit test, troubleshooting updates, metrics) still open.

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
- [x] (partially) Core reset/spawn/failure paths now emit rich LogAgentReasoning + LogStateTransition (enables the popout that diagnosed the original problem and the review that found the 11 follow-ups).
- [ ] Run `logloom build --source . --languages go,python --output flume-robust-ast-graph.json --git --call-graph --tags` (and commit the updated graph + any new nodes for `clearStaleClaim`, `legacyChat`, `handleReviewer`, `roleToTargetStatus`, `spawnReviewTasks`, `handleRoleLLMFailure`). **Still open per review.**
- [ ] Re-run `flume doctor --logloom` and `logloom graph find "clearStaleClaim|legacy Ollama|unknown-role|handleRoleLLMFailure"` to confirm new nodes appear. Promote at least one hard assertion in doctor.go (plan + review both call for this).
- [ ] Add a permanent doctor gate (already sketched in doctor.go:466) that asserts >0 nodes for "reviewer" + "clearStaleClaim" + "FLUME_GATEWAY_URL" + the new failure sentinel. **Still open.**

### Phase 1 (2h) — Kill the 404 at the Source (Native Gateway Wiring)
**Status after implementation pass + review**: Most landed (supervisor export + llm.Client native default + direct Ollama fallback + healthcheck + persistent block path). **One critical regression risk remains (see Review Issue 1 + 3).**

1. **Make LLM client native-aware + fix default** ([internal/llm/client.go](/Users/jonathandoughty/clients/fremenlabs/flume /flume/internal/llm/client.go)):
   - [x] Native localhost default + `FLUME_NATIVE_MODE` awareness added.
   - [x] Supervisor now does `Setenv` for `FLUME_GATEWAY_URL` + `FLUME_NATIVE_MODE` (supervisor.go).
   - [ ] **Open (high priority per review)**: The export still has a startup race (blind 500ms sleep after `startGateway` goroutine; `gatewayAddr` may be empty at Setenv time). Plus `llm.New` still falls back to the MODE env check. Can still produce the original "legacy Ollama 404" on some native wizard starts. Recommendation from review: make startup synchronous or pass explicit URL into NewManager/llm constructors instead of (only) env.

2. **Add a loud startup check** in worker manager cycle (or NewManager): attempt a 2s gateway /health; if failing and no usable Ollama base URL, log a FATAL-style "LLM path misconfigured for native mode — reviewers will 404-loop..." + emit agent reasoning event.
   - [x] 3s delayed healthcheck + error log added in llm/client (good observability).

3. **Parallel quick win**: in gatewayAvailable, on failure also log the exact URL that was tried (currently silent after first cache miss).
   - [x] Improved logging present.

### Phase 2 (2h) — Make Stale-Claim Role-Aware + Eliminate "unknown-role"
**Status after implementation pass + review**: Core landed and eliminates the reported "unknown-role" + wrong-status reviewer resets in nominal paths. Several follow-ups remain open (see Review Issues 2, 4, 6, 8).

1. **Signature change + logic fix** (runner.go + callers):
   - [x] `clearStaleClaim` now accepts role, switches correctly, targets "review" for reviewer/tester (and "planned" for pm). "unknown-role" string eliminated from reasoning logs. Callsite updated. Excellent.
   - [ ] Minor: some git helpers still use global slog (inconsistency with rich reasoning goal).

2. **Harden the stuck sweeps** (sweeps.go):
   - [x] Role-aware filters added to requeueStuck* (good).
   - [ ] **Open (review Issue 8)**: Still multiple parallel reset paths that hardcode "ready" or bypass the central `clearStaleClaim` + EnforceTransitionOrLog. Recommendation: extract single `ResetTaskForRole` helper used everywhere (plan originally asked for this).
   - [ ] Unit test asserting correct "review" (not "ready") reset target for a reviewer task under LLM crash **still missing** (plan + review both require it before declaring done).

3. **Claimer safety**: in TryAtomicClaim and roleToTargetStatus, also allow "ready" tasks that have `worker_role=reviewer|tester` (for recovery of the orphaned ones created by the current bug). This is a migration bandage.
   - [ ] **Open (high priority, review Issue 4)**: Not implemented. Orphaned review tasks in "ready" will never be claimed by reviewer workers even after the reset logic is correct. Pre-fix backfill + any resume/consensus path can still create them. Add the secondary query (document as temporary).

### Phase 3 (1.5h) — Resilience & Anti-Loop Hardening
**Status**: Significant progress (persistent 404 block after N failures, strengthened spawn guards including new sentinel, intake/PM guards). The block-vs-clear fight (Review Issue 2) and brittle string detection (Review Issue 10) are the main remaining correctness gaps.

1. **Lightweight retry...** — partial (retry logic exists in places; not uniformly wired for reviewer path yet).
2. **Fast-fail + poison...** — [x] `handleRoleLLMFailure` + `isPersistentConfigError` (string-based 404/gateway unreachable) + cap + block path landed. **Caveat (review)**: string matching is brittle; the block write can be overwritten by the outer clearStaleClaim path for non-exact-match errors.
3. **Strengthen spawnReviewTasks guards** — [x] landed (plus intake hierarchy guards and errSpawnGuardFired sentinel in the uncommitted overlay).
4. **Implementer stub awareness** — partial (some guards improved; full "do not spawn reviews from stub" not yet enforced per plan).

### Phase 4 (1h) — Tests + Gates + Observability
**Status per review**: None of the concrete test/gate items landed. This is the largest gap for claiming the remediation complete.
- [ ] Integration test for correct reset status + no extra children under forced 404 (plan + review both require before wider use).
- [ ] Promote logloom doctor gate + at least one assertion for the new symbols (clearStaleClaim, handleRoleLLMFailure, etc.).
- [ ] Metrics for llm_failure_resets_by_role.
- [ ] troubleshooting.md + popout docs updates.

### Phase 5 (30min) — Backfill & Migration
**Status per review**: Script exists but is incomplete (only blocks; does not repair).
- [ ] Repair mode (or separate pass) that sets status=review + worker_role=reviewer for review-flavored orphans (instead of / in addition to blocking). See Review Issue 5.
- [ ] "worker role mismatch detector" sweep (or incorporate into existing sweeps).
- [ ] The claimer "ready + role" recovery query (Phase 2.3) is the runtime equivalent of this backfill.

## Post-Review Follow-ups (from 2026-05-30 independent review of the implementation commits)

The review (full notes at /tmp/grok-review-640e38c5.md) found **11 issues** (7 bugs) even after the good core fixes. Prioritize these before declaring the remediation complete or rolling out widely:

**Bugs (highest priority first)**:
- Supervisor gateway export race + llm.New env dependency (supervisor.go:88, llm/client.go:143, manager.go:81) — can still recreate the original 404 loop on native starts.
- Block vs. clearStaleClaim status fight in reviewer error path (runner.go:164 and handleRoleLLMFailure) — "blocked" writes can be overwritten, re-enabling loops.
- Missing claimer recovery for orphaned ready+worker_role tasks (claim.go:60) — plan Phase 2.3 not implemented.
- Backfill script only blocks instead of repairing (scripts/backfill_orphaned_tasks.sh) — contradicts plan Phase 5.
- Bare UpdateDoc + parallel reset paths bypassing central logic (multiple sites in runner.go + sweeps.go) — lost writes under contention.
- Brittle string-based persistent error detection + magic caps (runner.go + llm/client.go) — easy to regress the fast-block path.
- (Plus the sweeps unification and test gaps already noted in the phases above.)

**Suggestions / nits** (important for long-term health):
- Update this plan document with "Landed" vs. "Still open" (this edit does a first pass) and promote the missing unit test + doctor assertion.
- Use errors.Is / typed sentinel instead of == for errSpawnGuardFired.
- Thread structured logger through git helpers instead of global slog.
- Add typed IsLLMConfigError helper and make reviewer cap configurable.

All 11 issues are tracked in the review artifact. Addressing the top 3-4 bugs + the missing test will make the system robust against the original concurrent failure scenarios.

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
