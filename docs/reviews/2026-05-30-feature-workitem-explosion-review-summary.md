# Review Summary

- **Mode**: branch (with uncommitted overlay)
- **Target**: feature/workitem-explosion vs main (merge-base 145fb4a574efdbb68f7e781747351d539f1b3e32) + current dirty runner.go
- **Files reviewed**: 9 (cmd/flume/orchestrator/elastic.go, cmd/flume/services/supervisor.go, docs/designs/worker-reviewer-llm-failure-remediation.md, internal/dashboard/api_intake.go, internal/llm/client.go, internal/logger/logger.go, internal/worker/runner.go, internal/worker/sweeps.go, scripts/backfill_orphaned_tasks.sh) plus cross-referenced claim/manager/types
- **Diff stats**: 9 files changed, 656 insertions(+), 44 deletions(-) (branch commits); +1 file, 24 insertions, 6 deletions (uncommitted runner.go overlay)
- **Issue counts**: 7 bugs, 3 suggestions, 2 nits (from ## Issues headings in the full review)

## Top issues

- [bug] cmd/flume/services/supervisor.go:88 -- Startup race: gatewayAddr export + FLUME_GATEWAY_URL Setenv happens after blind 500ms sleep with no wait/barrier; llm.New can still see the Docker default and fall to legacy 404.
- [bug] internal/worker/runner.go:164 -- handleRoleLLMFailure writes "blocked" (after 3 failures) but RunWorker err handler then calls clearStaleClaim which overwrites it back to "review", recreating the loop for some error strings.
- [bug] internal/llm/client.go:143 -- Native fallback still relies on FLUME_NATIVE_MODE env (racy export + wizard doesn't always set it); can still initialize with "http://gateway:8090".
- [bug] internal/worker/claim.go:60 -- No recovery query for orphaned "ready" + worker_role=reviewer/tester tasks (plan item not implemented); they stay stuck.
- [bug] scripts/backfill_orphaned_tasks.sh:66 -- Only forces "blocked"; does not repair to correct status+role as the plan required.
- [bug] internal/worker/runner.go:1234 + sweeps.go:195 -- Multiple bare UpdateDoc + parallel reset paths (implementer sweep, resume sweep) bypass the new central role-aware clearStaleClaim logic.
- [suggestion] docs/designs/worker-reviewer-llm-failure-remediation.md -- Many Phase items marked open in the reviewed diff were not executed (tests, logloom gates, doctor assertions, claimer bandage, etc.).

See the full review at: /tmp/grok-review-640e38c5.md

## Next actions recommended
1. Fix the supervisor gateway export race + make llm.Client construction take an explicit URL (highest priority; re-introduces the original P0).
2. Unify the block vs. clearStaleClaim decision so a task cannot be un-blocked by the normal error path.
3. Land the missing claimer "ready + role" recovery clause and make the backfill script repair (not just block).
4. Extract a single ResetTaskForRole helper used by all paths.
5. Add the reset-status unit test and at least one doctor/logloom assertion before broader testing.
6. Update the plan document with "Landed in these commits" section + remaining open items (including the 11 issues above).

The core reported signatures ("unknown-role", reviewer always resetting to ready, immediate 404 loops on native) are fixed for the common case after these changes, but the 7 bugs mean the exact concurrent failure + reclaim + spawn storms can still occur. Address the race + status-fight items first.