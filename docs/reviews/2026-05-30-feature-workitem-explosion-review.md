# Code Review: feature/workitem-explosion remediation (commits 6dbe8956, 06b4c4fe, 44b185dc + uncommitted runner.go overlay)

**Review scope**: Unified diff at /tmp/grok-review-diff-640e38c5.diff vs merge-base 145fb4a5. All files in /tmp/grok-review-files-640e38c5.txt plus cross-referenced sources (claim.go, manager.go, pkg/types/types.go, worker_test.go). Cross-checked against root-cause plan at docs/designs/worker-reviewer-llm-failure-remediation.md.

**Date of review**: 2026-05-30

## Summary

The changes successfully address the two primary reported P0 loops ("legacy Ollama HTTP 404" + "Worker (unknown-role) ... reset to ready") and several explosion amplifiers. Key fixes landed: role-aware `clearStaleClaim` + correct "review"/"planned" targets (runner.go:969), supervisor export of `FLUME_GATEWAY_URL` (supervisor.go:88), native-mode default + direct Ollama fallback implementation + startup healthcheck + persistent 404/block path in llm/client.go + runner.go, hardened sweeps role filters (sweeps.go:169,223), intake hierarchy anti-explosion (api_intake.go:1115+), PM skip for system nodes (runner.go:425), strengthened spawnReviewTasks guards + sentinel `errSpawnGuardFired` (uncommitted runner.go:989), git lock cleanup, execution_thoughts nested mapping fix (elastic.go + logger.go), and backfill script.

Dominant remaining risk areas: (1) startup timing/race for gatewayAddr visibility + env flag dependency for native LLM defaults (still allows 404 regression); (2) error-path status fight between `handleRoleLLMFailure` "blocked" and subsequent `clearStaleClaim` reset; (3) incomplete Phase 2/3 plan items (no claimer recovery for orphaned ready+role tasks, no unit test for correct reset status, no doctor/logloom gates); (4) narrow string-based persistent error detection and many silent UpdateDoc calls. Overall implementation quality is solid on the core symptoms (no more "unknown-role", reviewer tasks now target "review" on reset) but introduces subtle new races and leaves migration/backfill paths incomplete. Verdict: fixes the reported loops in happy paths but is not yet robust against the exact concurrent crash/reclaim/spawn scenarios that triggered the original storms.

## Issues

### Issue 1 -- Severity: bug
- File: cmd/flume/services/supervisor.go:88
- Description: The `FLUME_GATEWAY_URL` export (and thus reliable native gateway for llm.Client) has a startup race. `startGateway` goroutine writes `s.gatewayAddr` under lock (L182), but the read+Setenv (L88-96) occurs after a blind 500ms sleep with no synchronization or channel wait. On scheduler delay, `gatewayAddr == ""` at export time, so env is not set. Worker manager then starts; `llm.New` (manager.go:81) sees empty env and falls through to `FLUME_NATIVE_MODE` check (llm/client.go:147). If that env var is absent (common in `flume start` wizard paths), it defaults to "http://gateway:8090" → immediate legacy 404 path, recreating the original symptom.
- Suggestion: Make gateway startup synchronous or use a ready channel/condvar before the export block (or move Setenv inside startGateway after bind, or pass explicit GatewayURL into NewManager/llm.New instead of relying on global env). Add a post-start barrier + logged warning if gatewayAddr still empty after sleep.
- Status: open

### Issue 2 -- Severity: bug
- File: internal/worker/runner.go:164 (and 148-160, 298-302)
- Description: LLM failure path for reviewer creates a status machine violation / reset loop under cap. `handleReviewer` on Chat err calls `handleRoleLLMFailure` (which does `UpdateDoc` to "blocked" + clears active_worker when `failureCount >= 3`, L1238-1258). It then returns the err. The outer `RunWorker` err handler (L124) then evaluates `isPersistentConfigError` (string match on "404" etc). If the LLM err does *not* contain those strings (e.g., timeout, parse failure, generic gateway error after 3 attempts), it skips the block and unconditionally calls `clearStaleClaim(ctx, ..., worker.Role)` (L164) which does another `UpdateDoc` to "review" (L977, L987). The "blocked" write is overwritten; task returns to claimable review state and the loop resumes. (Even for 404 strings the double-write is racy.)
- Suggestion: Make `handleRoleLLMFailure` (and the persistent block path) return a sentinel or boolean indicating "terminal state written, do not clearStale". Or hoist the cap/block decision into the single outer error handler before any clear. Ensure blocked tasks are never passed to clearStaleClaim. Add a state transition log that is authoritative.
- Status: open

### Issue 3 -- Severity: bug
- File: internal/llm/client.go:143 (cross-ref supervisor.go:88 and manager.go:81)
- Description: `llm.New` native-mode fallback logic still depends on `FLUME_NATIVE_MODE=="1"` env var when `FLUME_GATEWAY_URL` is empty at New() time. The remediation added the localhost default inside the `if gatewayURL == ""` block, but the supervisor export (the intended primary signal) is racy (Issue 1) and the wizard/start path does not guarantee the NATIVE_MODE flag. Result: in some native runs the client still initializes with "http://gateway:8090", `gatewayAvailable` fails, all reviewer/PM/tester traffic hits `legacyChat` → `resolveOllamaBaseURL` (which can 404 on non-Ollama ports) exactly as in the pre-fix diagnosis. The 3s-delayed healthcheck error log (L173) fires too late to prevent first-wave loops.
- Suggestion: (a) Always prefer an explicit gateway URL passed at construction (add `NewWithGatewayURL` or config struct); (b) have supervisor set both `FLUME_GATEWAY_URL` *and* `FLUME_NATIVE_MODE=1` (or better, stop using the MODE env for this decision); (c) make the healthcheck synchronous or fail-fast in New() for worker contexts; (d) log the *effective* gatewayURL at client creation time.
- Status: open

### Issue 4 -- Severity: bug
- File: internal/worker/claim.go:60 (and 66; plan Phase 2 item 3)
- Description: `TryAtomicClaim` (via `roleToTargetStatus`) only ever queries the role's canonical bucket (`"review"` for reviewer/tester). The plan explicitly called for a recovery bandage: "also allow 'ready' tasks that have `worker_role=reviewer|tester` (for recovery of the orphaned ones created by the current bug)". This was not implemented. Post-fix, a reviewer task that lands in "ready" (via resume sweep, consensus reject, manual edit, or pre-fix backfill) will never be claimed by a reviewer worker, even though clearStale now correctly targets "review". Orphans stay stuck until external intervention.
- Suggestion: Extend the claim query (or add a secondary query) to also match `status=="ready" AND worker_role IN ("reviewer","tester")` when the worker role is reviewer/tester. Document as temporary migration aid and remove after backfill.
- Status: open

### Issue 5 -- Severity: bug
- File: scripts/backfill_orphaned_tasks.sh:66 (and 43)
- Description: The backfill script (Phase 5 of plan) only *blocks* tasks matching the old error signatures; it never repairs them to a correct status + worker_role so the fixed paths can process them. For a reviewer task that was in "ready" with 404 in error_message, it forces "blocked". This is safe but loses work and contradicts the plan's stated goal ("repair any orphaned review-flavored tasks ... (set status=review + worker_role if missing)").
- Suggestion: Add a repair mode (or separate pass) that sets status=review + worker_role=reviewer (or the appropriate value from title/prior reasoning) for review-flavored orphans instead of (or before) blocking. Make blocking opt-in or the last resort.
- Status: open

### Issue 6 -- Severity: bug
- File: internal/worker/runner.go:1234 (handleRoleLLMFailure) and 807 (ImplementerHandleLLMFailure)
- Description: Both retry-cap handlers (and the outer persistent block) perform bare ` _ = r.es.UpdateDoc(...)` (L831, L834, L1258, L1262, etc.) with no error logging, no retry, and no subsequent state transition emission in the failure case. Under the exact ES contention that occurs during the original 404/reset storms (multiple workers + sweeps + LogAgentReasoning scripts), these writes can be lost, leaving tasks in "running" with stale active_worker or with attempts not incremented. The role-aware clearStaleClaim then sees inconsistent state.
- Suggestion: Surface errors (at least via logger + LogAgentReasoning), use the existing OCC helpers where seq/prim available, or wrap in a small retry helper. At minimum log every ignored UpdateDoc error on critical status paths.
- Status: open

### Issue 7 -- Severity: suggestion
- File: docs/designs/worker-reviewer-llm-failure-remediation.md:63 (and 97, 111, 149, 171)
- Description: The plan document (itself part of the reviewed diff) lists many Phase 0/2/4/5 items as open checkboxes that were not executed or landed: logloom build + doctor gate assertions, unit test asserting "review" reset not "ready", claimer "ready + role" recovery, metrics counter, troubleshooting.md updates, backfill repair (vs pure block), etc. Several root-cause line numbers in the doc are now stale post-edit. This reduces the value of the document as the single source of truth for verification.
- Suggestion: Update the plan with a "Completed in this PR" section + new open follow-ups. Promote at least the reset-status unit test and one doctor assertion before declaring the remediation done.
- Status: open

### Issue 8 -- Severity: bug
- File: internal/worker/sweeps.go:195 (requeueStuckImplementerTasks) and 537 (ExecuteResumeSweep)
- Description: Even with the new role filters on the implementer stuck sweep (good), the reset is still a hardcoded `"ready"` string + no worker_role consideration in the update map. While correct for the filtered set, it bypasses the central `clearStaleClaim` logic and the TaskStateMachine.EnforceTransitionOrLog path used elsewhere (contrast with clearStale L994). Resume sweep also always forces "ready" regardless of the task's worker_role or prior status. This creates a second, less-audited reset path that could violate role invariants under future changes.
- Suggestion: Extract a single `ResetTaskForRole(ctx, taskID, currentStatus, workerRole string)` helper (or reuse clearStaleClaim with a "force" flag) and call it from all reset sites (sweeps, resume, consensus reject paths). This was explicitly requested in the plan (Phase 2.2).
- Status: open

### Issue 9 -- Severity: nit
- File: internal/worker/runner.go:769 (and 1044, 1079)
- Description: `cleanStaleLocks`, `gitCheckoutBranch`, and several git helpers use the global `slog` package directly (`slog.Warn`) instead of the Runner's structured `r.logger`. This loses the component/trace context that the rest of the worker path emits and makes Logloom correlation harder. Minor but inconsistent with the "rich reasoning" goal of the remediation.
- Suggestion: Thread the logger through the git helpers or use `r.logger` (or a package-level worker logger) for all git-side effects.
- Status: open

### Issue 10 -- Severity: suggestion
- File: internal/llm/client.go:634 (legacyChat) + 144 (persistent detection in runner.go)
- Description: Persistent config error detection is a brittle `strings.Contains(errStr, "404") || ... "no such host" || "gateway is unreachable..."`. Any wording change in legacyChat, resolveOllamaBaseURL, or gateway errors will cause the fast-block path to be missed, reintroducing infinite loops for the exact class of misconfig the remediation targeted. The 3-attempt cap in handleRoleLLMFailure is also a magic number with no env override (unlike implementer).
- Suggestion: Define typed sentinel errors or an `IsLLMConfigError(err error) bool` in the llm package (or export the check). Make reviewer/tester cap configurable via `FLUME_REVIEWER_MAX_LLM_FAILURES`. Add a unit test that injects a 404-shaped error and asserts block after N attempts with no further resets.
- Status: open

### Issue 11 -- Severity: nit
- File: internal/worker/runner.go:30 (uncommitted overlay)
- Description: Sentinel `var errSpawnGuardFired = fmt.Errorf(...)` is compared with `==` at the call site (L990). While this works for unexported same-package vars today, it is fragile if the error ever gets wrapped (current callers do not wrap). The name and comment are good, but using `errors.Is` + a custom type or `errors.New` + sentinel would be stricter.
- Suggestion: Keep the pattern but document the equality contract or switch to a typed error for future-proofing.
- Status: open

## Additional Observations (no new issue filed)
- `handleTester` currently has no LLM path at all (pure local exec), so the new `handleRoleLLMFailure` and reviewer-specific caps do not apply to it. If/when a real tester LLM is added, the same error paths must be wired.
- Many JSON unmarshals and field accesses use the `, ok` pattern inconsistently or ignore errors (e.g., reviewer approved parse, multiple places in sweeps). This is pre-existing style but amplifies risk on the new reasoning/execution_thoughts paths.
- The remediation adds excellent LogAgentReasoning + LogStateTransition coverage on the failure/reset/spawn paths; this directly enables the observability that diagnosed the original problem.
- No new tests (worker_test.go only had pre-existing roleToTargetStatus table) were added despite the plan calling for one. Manual verification will be required.

The core user-visible loops should be gone after these changes on a clean native start, but the issues above (especially 1-4) mean the system is not yet hardened against the precise concurrent failure + restart scenarios that produced the 74- and 258-item explosions. Recommend addressing the supervisor race + reviewer block-vs-clear fight before wider rollout, plus landing the missing claimer recovery and a reset-status test.