# Implementation Plan: Plan New Work + Work Queue Reliability Fixes

**Observed (2026-05-27 session `plan-f7a621ca894e` for "centralized logging in CLI")**:
- Complexity 4 (medium), 1 epic / 1 feat / 1 story / 2 tasks in LLM plan.
- `countPlanTasks=2 <=3` → fastpath path taken on commit (only 2 `item_type=task` records, no hierarchy bloat).
- Workers mesh healthy: pm/implementer/reviewer/tester roles present and idle.
- 0 tasks in `agent-task-records` (clean baseline).
- Gateway: correct hybrid routing for intake "reasoning" (frontier when needed), ~4s LLM.
- No explosions or errors during planning phase in filtered logs.
- Session remained "active"/"ready" (user had not yet committed at analysis time).

**Key Risks Found (would negatively impact UX on commit + execution)**:

## P0 – Critical Bugs (Broken Lifecycle for Medium+ Plans)
1. **Hierarchy promotion deadlock (siblings after first never reach ready)**  
   - `buildTaskHierarchy` (api_intake.go:1020): creates stories/feats/epics with `status=planned` (owner=pm). First leaf task per story chain pre-set `ready`; siblings `planned` + `depends_on=[prev]`.
   - `promotePlannedTasks` (sweeps.go:371): `if task.ParentID != "" { pstatus = ...; if pstatus=="" || pstatus=="planned" || ... { skip } }` + depends check.
   - Result: story (parent) stays `planned` forever → all post-first siblings **never promoted**, even after depends met and prior task `done`.
   - Only the very first leaf per story (pre-created ready) + fastpath (<=3 leaves) ever run.
   - Affects any plan with >3 leaf tasks or stories containing 2+ tasks (common for "medium").
   - `parentCompletionSweep` (730) only considers top-level (no parent_id) items *already* in {running,ready,review*}; epics created `planned` → never swept → hierarchy items stuck `planned`.

2. **No server-side guardrails on workitem count from LLM plan**  
   - Only client-side prompt guidelines ("NEVER >3 tasks for single-file", "match granularity", coalesce per story).
   - `commitPlan` (1120) + `countPlanTasks` (925) accept any size; fastpath only for <=3.
   - Medium request can still yield 10-30+ leaves → 20-60+ total workitems (hierarchy) → queue flood, high token spend, worker thrash, user perceives "explosion".
   - No hard cap, no warning in UI on commit, no backpressure.

3. **Shadow-mode TaskStateMachine hides violations during real runs** (types.go:333, validation.go)  
   - `DefaultTaskStateMachine` starts `ShadowMode=true`.
   - All writers (sweeps, runner:110, claim, api_tasks transitions, intake) call `Enforce...OrLog` but proceed on violation.
   - During a real Plan→Queue run, bugs in transitions (e.g. the parent-status interaction above) only produce WARN logs; state can drift.
   - No aggregated metrics / dashboard view of shadow violations.

## P1 – Performance & Scalability
4. **Expensive ID sequencing on every commit** (`getNextIDSequence` + `esCounterHWM`, api_intake:855)  
   - 4 regexp searches (size 10k) + HWM counter doc per commit (epic/feat/story/task prefixes).
   - Linear in history size; repeated on refine+commit cycles; no per-repo sharding or in-mem cache.
   - For concurrent plans or large history → latency + load on ES during the exact moment user hits "commit".

5. **Sweep overhead scales poorly with planned backlog**  
   - `RunThrottled` + adaptive promote (137): still does 200-hit searches + mget of deps every 2-5s globally (and per-repo?).
   - `promotePlannedTasks` called with `""` (global) even when only one project active.
   - After explosion commit, repeated full scans + mget for every planned item.

6. **Planning LLM always high-cost path for intake**  
   - Intake sessions route as `task_type=reasoning` (gateway logs) → frontier (grok-4.3) even for complexity=4.
   - Separate from the `<=3 fastModel` routing only applied later in `commitPlan` (1136) for the *resulting tasks*.
   - 120s timeout (seen in planningStatus); slow local models or rate limits → poor "Plan New Work" UX.

## P2 – Work Breakdown & UX Friction
7. **Inconsistent fastpath vs hierarchy UX**  
   - <=3 leaves: flat task list (good, clean progression via sibling depends only).
   - >3: full epic/feat/story/task tree (organizational bloat, titles duplicated, complexity copied to every level).
   - User edits plan in chat → count crosses 3 → surprises with 5-10x more queue items on commit.
   - `coalesceStoryTasks` (902) only intra-story; no cross-feature dedup.

8. **PM items from intake never reliably "complete" the visible hierarchy**  
   - Intake creates pm-owned planned epics etc.
   - `handlePM` (runner:345) does further decomposition (creates more subtasks) and sets self `running` (not done).
   - `parentCompletionSweep` insufficient for sub-levels.
   - Result: after leaves finish, organizational items linger in planned/review states → cluttered queue, misleading "not done".

9. **Weak feedback after commit**  
   - IntakeModal `commitWork` (414) only shows `count`, invalidates queries, sets `committed`.
   - No immediate deep-link to the new items in queue, no progress tracker for the batch (e.g. "2/2 tasks promoted, 1 running"), no ETA or token estimate.
   - ProjectDetailPage + snapshot refresh is eventual; user sees "nothing happened" for 5-10s while first promote/claim cycle runs.

10. **Partial error handling in commit + execution**  
    - `commitPlan` indexes sequentially; partial failure leaves orphans (some docs in, session marked committed).
    - No cleanup on LLM planning failure mid-hierarchy build.
    - Runner `AutoCommitAndPush` / PR creation failures are logged but task may still go to review with no diff.

## P3 – Observability & Diagnostics
11. **Hard to debug "why is this stuck in planned?" for end users / operators**  
    - No per-task "promotion eligibility" view or last-sweep reason.
    - `promote*` Debug logs exist but not surfaced in UI or `/api/logs`.
    - No correlation ID from plan session → created task IDs → execution traces.

12. **Log noise vs signal during queue activity**  
    - Current docker filters catch some, but production runs mix Python logloom enrichment (legacy graph nodes) with Go structured logs.
    - No dedicated "plan-commit" or "queue-lifecycle" event stream.

## Proposed Implementation Plan (Phased, Test-Driven)

### Phase 0 – Immediate Hardening (1-2 days, no behavior change for fastpath)
- Add **server-side cap + warning** in `commitPlan` (api_intake.go):
  - `MAX_LEAF_TASKS=15` (tunable env). If `countPlanTasks > MAX` after coalesce: reject commit with clear error ("Plan too large (N leaves). Refine in chat or split request.").
  - UI (IntakeModal): show live "estimated work items" + warning banner before commit button when >8 or crosses into hierarchy.
- Flip **TaskStateMachine to strict mode in non-prod** (or add `FLUME_STRICT_STATE_MACHINE=1`); keep shadow in prod until audit clean. Instrument shadow violations to a counter in ES or prometheus (via logger).
- Add **unit test** for promote with hierarchy parents (sweeps_test or new); assert siblings promote once depends met even if parent remains "planned" (or decide intended semantics).

### Phase 1 – Fix Promotion & Hierarchy Semantics (core reliability, 3-4 days)
- **Decide & document hierarchy contract** (update pkg/types + docs):
  Option A (recommended for UX): Organizational items (epic/feat/story) are **purely structural**, never executed by workers. `promotePlannedTasks` for `item_type=="task"` should **ignore parent status** (only enforce `depends_on` + not blocked/archived). Epics etc. get auto-marked `done` by strengthened `parentCompletionSweep` (or new `hierarchyCompletionSweep`) when all descendant leaves terminal.
  Option B: Make intake create stories/feats in `ready` (or a new `active` status) so existing parent check passes; pm workers "own" them for visibility only.
- Implement chosen option in `promotePlannedTasks` + `parentCompletionSweep` (add recursive descendant check or ES parent/child if mapping supports; keep simple for now).
- Ensure first task in hierarchy chains still pre-created `ready` (or let promote handle uniformly).
- Update `buildTaskHierarchy` to set `Complexity*` only on leaves or consistently; copy acceptanceCriteria down only where useful.
- Add `parent_completion_sweep` unit + integration test that exercises full chain (epic → story → 3 tasks) and verifies all levels reach `done`.
- **Guard**: after any promote or parent sweep, emit structured event to `agent-task-records` or a new `agent-queue-events` index for audit.

### Phase 2 – Guardrails, Performance, Predictable Breakdown (4-5 days)
- **Hard + soft limits**:
  - Env `FLUME_MAX_PLAN_LEAVES=12`, `FLUME_WARN_PLAN_LEAVES=5`.
  - In `runInitialPlanning` / refine: after LLM returns plan, if > warn threshold, append agent message "This plan will create N work items (est. cost X). Consider splitting?"
  - Coalesce more aggressively (cross-feature file-based, or LLM post-pass "merge trivial tasks").
- **ID sequencing perf**:
  - Cache HWM per prefix in memory (Sweeper or Server) with periodic refresh + write-through on allocate.
  - Or switch to ES `_seq_no` or monotonic counter doc with optimistic increment (single doc per prefix, CAS via seq).
  - Make `getNextIDSequence` repo-scoped if possible (prefix `task-{repo}-` ? but keep human IDs simple).
- **Sweep efficiency**:
  - Make global promote sweep cheaper: only run full when `plannedCount` high; otherwise per-repo on project activity (webhook from commit, or UI-triggered "nudge queue" button).
  - Use `pit` or scroll for very large backlogs; add `last_promoted_at` watermark.
- **Routing for planning**:
  - Tag intake/planner LLM calls distinctly (`agent_role=intake`, `task_type=planning`); allow config `FLUME_PLANNER_MODEL` or lower complexity score for the planner itself (separate from resulting work items).
  - Expose planner timeout + model in doctor + settings UI.

### Phase 3 – UX Polish & Observability (2-3 days)
- **Post-commit experience**:
  - Return from `/commit` the list of top-level IDs + "first_ready_task".
  - IntakeModal: on committed, show "Work committed: 2 tasks. View in Queue →" button that navigates to ProjectDetailPage filtered to the new IDs (or highlights fresh items).
  - Add small "lifecycle progress" component (or reuse existing) that polls the batch until all `done`.
- **Queue visibility**:
  - In project tasks view + snapshot: show "blocked by" / "depends on" chips + "promotion eligibility" (last sweep reason if available).
  - Surface recent shadow violations (if any) in admin/debug panel.
  - `/api/logs` or new `/api/queue/events?plan_session=...` endpoint for the lineage of a Plan New Work batch.
- **Better errors**:
  - Partial commit: transactional wrapper or compensating delete of partial docs + clear message.
  - On runner git/PR failures: transition to `blocked` with actionable `error_message` + "retry" action that re-queues.

### Phase 4 – Validation & Hardening (ongoing)
- **E2E test** (tests/e2e/test_autonomous_queue.py or new): 
  1. POST plan session with medium prompt (expect 4-8 leaves).
  2. Commit.
  3. Assert exact #workitems created, fastpath/hierarchy decision, initial statuses.
  4. Wait for sweeps + workers (with timeout); assert full progression for all leaves + parents to terminal states.
  5. Assert no shadow violations in logs during run.
  6. Run with >3 and <=3 cases.
- Add perf test (tests/perf/) for commit of 10-task plan (ID gen + indexing latency <2s).
- Update doctor CLI + onboarding docs with "Plan New Work" sizing guidelines and "what to do if queue stalls".
- Feature flag rollout: `FLUME_HIERARCHY_MODE=v2` (new promotion rules) behind flag for one release.
- Monitoring: add dashboard panels for "plan commits / hour", "avg workitems per plan", "p95 promote latency", "shadow violation rate".

**Files Touched (estimated)**:
- `internal/dashboard/api_intake.go` (guards, fastpath/hierarchy decision, commit tx)
- `internal/worker/sweeps.go` (promote + parent sweeps – the core fix)
- `pkg/types/types.go` + `validation.go` (docs + strict mode toggle)
- `internal/worker/runner.go` (pm handling, error paths, status after auto-commit)
- `src/frontend/src/src/components/IntakeModal.tsx` + ProjectDetailPage.tsx (UX feedback, warnings, navigation)
- `internal/dashboard/api_system.go` + `api_tasks.go` (events, diagnostics endpoints)
- New: `internal/worker/sweeps_test.go` (or expand worker_test), e2e tests, docs/reference/queue-lifecycle.md
- Config: new envs in `internal/config/`, start.go, docker-compose, doctor.go

**Success Criteria (measurable)**:
- Any "simple-medium" Plan New Work (user-described 3-8 leaves) produces ≤ N items (N=leaves+small constant), all reach `done` or `blocked` within 2× single-task time, zero stuck `planned` siblings.
- 0 shadow violations in a full end-to-end run of the logging example + a 6-task cross-cutting example.
- Commit of 12-leaf plan rejected with clear UX message (no explosion).
- ID sequencing for 10 concurrent small plans <500ms each.
- Logs during run contain clear "promoted X (reason: depends met, parent active)" lines correlated to plan session.
- User can click from committed modal → see their 2-5 items in queue already in `ready` or `running`.

**Rollback / Safety**:
- All changes behind `FLUME_QUEUE_RELIABILITY_V2=1` or per-component flags initially.
- Existing fastpath (<=3) behavior unchanged in Phase 1.
- Extensive logging of before/after promote decisions during shadow period.

**Next Steps for Author**:
1. Review this doc with team (especially original Python port authors for hierarchy intent).
2. Land Phase 0 (cap + shadow audit) this week – low risk.
3. Reproduce the sibling-stuck bug locally: create a 5-task plan via UI, commit, watch `promotePlannedTasks` debug logs + task statuses over 60s.
4. Run the full e2e once fixes land; update this doc with measured numbers from real run (workitems created, wall time to all-done, token spend).

This plan directly addresses the monitored failure modes (explosion risk, incomplete Planned→...→Done flow, work breakdown surprises) while preserving the excellent fastpath experience for the simple case the user exercised.

---
*Generated from live log monitoring + static analysis of api_intake.go, sweeps.go, runner.go, types/*, IntakeModal.tsx (2026-05-27).*
