# Subagent Fleet Orchestrator for the Grok Build Harness

**Author:** (Grok Systems Architect / design contributor placeholder)  
**Date:** 2026-06-01  
**Status:** Draft (v0.2 addressing 2026-06 review feedback)  
**Version:** 0.2 (revised post-review; implementable after blocking OQs + PR0 runtime spike)

---

## Overview

The Grok Build harness currently provides a simple, effective primitive for delegating work to subagents via the `spawn_subagent` tool (also referred to as the `task` tool in user-guide docs). Subagents run in isolated sessions (with optional `resume_from`, `persona` overlays, `subagent_type` like `general-purpose`/`explore`/`plan`, `capability_mode`, and `isolation: "worktree"`), share the `get_command_or_subagent_output` / `kill_command_or_subagent` / `wait_commands_or_subagents` surface with background shell tasks, and surface in the TUI via `Ctrl+T` (tasks pane, showing active/completed subagents + lineage) and `Ctrl+;` (queue pane).

This works well for small numbers of ad-hoc or manually-orchestrated delegations. However, at the scale observed in real usage—e.g., the 5 concurrent subagents (IDs: 019e8677-614d-7012-99fa-3cf95a8cd0e0 "fix-install" with 119 tool calls/5min; 019e8677-729e-78e3-8900-d0cc199e92dc "fix-clone-ingest" 95 calls; 019e8677-894e-7a70-8361-1e57334dd0cd "fix-plan-new-work-rag" 88 calls; 019e8677-9c83-7270-a320-a750ced81e1e "harden-implementer-tools" 96 calls; 019e8677-b025-70e0-bf50-7239a10f93d8 "integration-tests" 101 calls/393s) used to close the 4-point Elastro + LogLoom contract—the parent agent is forced into manual ID babysitting, repeated polling, and lifecycle reasoning. Compaction can leave stale "still running" views even when `meta.json` shows completion. There is no central registry, heartbeat, claiming/backpressure, or automated recovery.

The **Subagent Fleet Orchestrator** (also "Delegated Work Queue" or "Fleet Manager") introduces a first-class, reliable, observable, backpressured parallel delegation path modeled directly on the production patterns hardened in the Flume worker-manager (the very system improved during the parallel work that exposed these harness limitations). The simple `spawn_subagent` path remains the "fast path" for interactive ad-hoc use; the orchestrator is the "reliable fleet path" for complex parallel contract-style workloads. New tools (`fleet_enqueue`, `fleet_status`, `fleet_wait`, `fleet_sweep`, etc.) and an in-process manager provide automatic claiming, specialized sweeps, state machine enforcement, telemetry bridging (surfacing child reasoning/thinking streams), and TUI integration while preserving full coexistence and `resume_from` / persona / worktree semantics.

Expected impact: for a 5-delegation batch like the recent contract close, the parent context stays small (structured delegation records + handoff file paths instead of full child transcripts); automatic stuck-task recovery and backpressure prevent thundering herds on local LLMs; visibility improves in `Ctrl+T`/`Ctrl+;` without manual `get_*` polling loops.

---

## Background & Motivation

### Current State (Harness Subagent Model)
- **Spawning**: Parent calls `spawn_subagent` (params: `subagent_type`, `description` (often with `[persona]` prefix for pager label renderer), `prompt` or full context, optional `resume_from`, `background: true`, `isolation: "worktree"`, `capability_mode`). See `~/.grok/docs/user-guide/16-subagents.md` (sections "Spawning Subagents", "Capability Modes", "Context Inheritance", "Isolation: Worktree Mode", "The Tasks Pane (TUI)"); skills like `bundled/skills/implement/SKILL.md` (lines 315-338 for implementer launch, 408-419 for parallel reviewers with `background`, 646-650 for `resume_from` fixes) and `bundled/skills/pr-babysit/SKILL.md` (lines 414-462, explicit max 8 concurrent, `task_id` collection then `get_command_or_subagent_output(..., block=true, timeout_ms=...)`).
- **Lifecycle & Polling**: Returns `subagent_id` (usable as `task_id`). Parent manually tracks IDs (as in the 5-contract case) and polls via `get_command_or_subagent_output(task_id)` (non-blocking or `block=true` with timeout) or `wait_commands_or_subagents`. Kill via shared `kill_command_or_subagent`. See `~/.grok/docs/user-guide/20-background-tasks.md` (full "Background Commands", "Getting Output", "Killing Background Tasks", "The Queue Pane").
- **Durable State**: Per-subagent `meta.json` under session dirs (e.g. `sessions/.../subagent-<id>/<id>/meta.json` containing `subagent_id`, `parent_session_id`, `child_session_id`, `subagent_type`, `description`, `prompt`). Subagent sessions also live under `~/.grok/worktrees/.../subagent-<id>/...`. See example from prior review sessions in `~/.grok/sessions/.../subagents/.../meta.json`.
- **TUI**: `Ctrl+T` toggles TODO/task panel (active/completed subagents, lineage, status). `Ctrl+;` toggles queue pane (background + scheduled + monitors). `Ctrl+Shift+A` subagent catalog. Depth limits prevent runaway nesting (docs: "Depth Limits").
- **Personas/Agents**: Layered behavioral instructions (bundled in `~/.grok/bundled/personas/*.toml` and `bundled/roles/`) vs full agent types. Skills explicitly avoid the `persona` param on spawn (use description prefix + prompt injection) because "that parameter is not supported" in current pager label path; see `16-subagents.md` ("Agents vs Personas", "Built-in Personas", "Persona IO Contracts" e.g. implementer reads `review_file` writes `summary_file`).
- **Background Integration**: `/loop` (via `scheduler_create`), `monitor`, `run_terminal_command(background: true)`. Shared surfaces mean subagents and shell tasks are siblings in the task manager.
- **ACP/Headless**: Subagents work in agent mode too (15-agent-mode.md).

### Pain Points Observed in Practice
During this conversation, 5 subagents were spawned in a burst for parallel Elastro/LogLoom contract closure work (exact IDs and call counts above; all exited 0 with high-quality minimal verified changes). The parent (this harness session) had to:
- Manually record 5 distinct `subagent_id`s.
- Poll via `get_command_or_subagent_output` (one case post-compaction showed "still running" despite `meta.json` completed).
- Reason about completion, ordering, and recovery itself.
- No automatic backpressure (risk on local LLM thundering herd), no heartbeats, no claiming (first-come first-served races), no specialized recovery sweeps.

This is **precisely** the "PM Decomposition Death Spiral" class of problem (409 OCC races on claim vs handler, stuck tasks, loss of visibility, manual babysitting, no auto-recovery) that the Flume worker-manager was built to solve. We used the raw harness pattern to parallelize the hardening of the production system designed for reliable parallel agentic work—an ironic and painful observation explicitly called out in the task.

Key Flume references (explored via tools; all paths absolute under `/Users/jonathandoughty/clients/fremenlabs/flume /flume`):
- `internal/worker/manager.go`: `Manager` struct (fields: `pool *Pool`, `claimer *Claimer`, `sweeper *Sweeper`, `wakeChan`, `capsMu`), `NewManager`, `Run` (ticker + wake), `cycle` (L170: pause check, `sweeper.RunThrottled`, pre-flight `countAvailableByStatus`, `fetchBusyWorkers`, `BuildWorkers` + node caps, `processWorker`, `pool.SyncWorkerProcesses`, `saveState` publishing `ClusterState`), `processWorker` (L246: busy check, throttle by `nodeCaps`/`nodeLoads`, pre-flight avail, `claimer.TryAtomicClaim`).
- `internal/worker/claim.go`: `Claimer.TryAtomicClaim` (L56: roleToTargetStatus, ES query with rich `must_not` for `active_worker` + `decomposed_at` (Fix 6 guard), struct+raw `hasDecompMarkers` belt-and-suspenders, `isDuplicateTask` (norm + semantic embed), `isWIPSaturated`, `checkGitOverlap`, OCC `atomicClaim` with `if_seq_no`/`primary_term` via `UpdateDocOCC`, `LogTaskStateTransition`).
- `internal/worker/pool.go`: `Pool` with `golang.org/x/sync/semaphore.Weighted` (default 16), `Submit`, `SyncWorkerProcesses`, `wg` graceful `Shutdown`.
- `internal/worker/sweeps.go`: `Sweeper` (mutex + `lastRun`/`intervals` map), `RunThrottled` (L72: 8+ sweeps on cadences—`stuck_impl` 30s, `stuck_review` 30s, `promote` adaptive 2-5s via `getPromoteInterval` + `SetPlannedCount`, `consensus` 5s, `parent_comp` 5s, `child_count_recon` 45s), `requeueStuckImplementerTasks` (excludes `decomposed_at`), `promotePlannedTasks` (unified source of truth, batch mget cache, depth `MAX_HIERARCHY_DEPTH`, OCC retry, `EnforceTransitionOrLog`), `ExecuteResumeSweep` (skips PM-block patterns), `ExecuteBlockSweep`, `parentCompletionSweep` (2-pass for hierarchy), `childCountReconciliationSweep` (lag logs + script correction).
- `internal/worker/runner.go`: `handlePM` (L794: anti-re-decomp ES child search + denorm + backoff, budget enforcement pre/post-LLM, depth guard, atomic combined `child_count`+`decomposed_at`+status=done + script, streaming `ChatStream` + `LogAgentReasoning` per-delta + telemetry node routing), `handleImplementer` (real multi-turn ReAct loop with `maxTurns`, AST verification gate for writes, `LogAgentReasoning` on every phase/turn/thought/tool), `updateTaskLeaseState` (central choke for lease cols + Enforce + dual Log*), `RunWorker`.
- `pkg/types/validation.go` + `types.go`: `TaskStateMachine` (shadow mode default via env `FLUME_TASK_STATE_MACHINE_SHADOW_MODE`), `DefaultTaskStateMachine.EnforceTransition`/`EnforceTransitionOrLog` (called from 100% writers: claimer, runner, sweeps), `ValidTransitions` map, `TaskStatus` (planned/ready/running/review/review-consensus/done/blocked/archived), `MAX_HIERARCHY_DEPTH=6`, `Task` (denorms `DecomposedAt`/`ChildCount`/`PlanSessionID`/`HierarchyDepth` etc.), `Evidence` for terminal gates.
- Telemetry: `internal/logger/logger.go`: `LogAgentReasoning` (L364: slog + `appendExecutionThoughtNonBlocking` via Painless script to `agent-task-records.execution_thoughts[]` for UI/Logloom; 2s ctx, retry on 409, never silent), `LogStateTransition`, `SetESBridge` (wired in `manager.go:89`), worker snapshots in `ClusterState`, node registry (`flume-node-registry`).

Flume also has WIP limits per-repo, git overlap/lock checks, project `WorkPaused`, plan budgets/sessions, depth enforcement at promote/claim/PM creation, reconciliation sweeps for denorm lag.

The harness lacks all of this for its delegation "queue."

### Why This Change Now
The 5-subagent burst succeeded (high quality output) but at high cognitive cost to the parent agent and with observability gaps. Future complex parallel work (multi-contract closures, large refactors with parallel research+implement+test+review fleets) will hit the same wall harder. Adding a modeled fleet orchestrator makes reliable parallelism a *safe primitive* rather than an expert manual pattern, directly transferable from the Flume learnings.

---

## Goals & Non-Goals

### Goals
- Make reliable parallel subagent delegation (N=5–20 concurrent "contract" style workloads) a first-class, low-parent-context, observable primitive.
- Direct 1:1 conceptual mapping from Flume (Manager/Claimer/Pool/Sweeper/StateMachine/Telemetry) to harness equivalents; cite exact Flume symbols + harness paths.
- Preserve `spawn_subagent` (and `task` tool) unchanged as the interactive fast path. Fleet path is opt-in via new tools for complex cases.
- Full integration with existing surfaces: `resume_from`, personas (via prompt+desc prefix), worktree isolation, `capability_mode`, TUI `Ctrl+T`/`Ctrl+;` (enhanced grouping by fleet), `/loop`/`monitor`/`scheduler`, ACP/agent-mode, depth limits.
- Durability + recovery: delegations survive compaction/restart; automatic stuck sweeps + resume.
- Backpressure + claiming: semaphore-weighted pool + atomic claim (in-memory or file-based OCC) to avoid overload on local models; pre-flight counts.
- Observability: child `thinking`/reasoning streams surface to parent via enhanced task pane + structured delegation records (handoff files like `review_file`/`summary_file`); rich logs analogous to `LogAgentReasoning`.
- Quantified: support ~8-16 concurrent delegations (cf. Flume default 16, pr-babysit cap 8); context savings via structured state vs raw transcripts (e.g. 5x 100-turn children would otherwise bloat parent); latency targets for claim <100ms local, sweep cycles 5-30s.
- Security: same isolation as today + fleet-scoped auth/permissions if ACP; no new privilege escalation.
- Rollout: feature flag + full coexistence; incremental PRs delivering value early (observability first).
- Concrete, implementable after spike: pseudocode for core loops, exact new tool signatures, data model on disk, Mermaid diagrams, PR plan with file impact + deps.

### Non-Goals
- Replace or deprecate `spawn_subagent` / manual use cases. (Fast path stays.)
- Implement full distributed mesh / ES-backed queue in harness (keep simple/local; durable is session-scoped json/sqlite).
- Change existing subagent depth limits, worktree semantics, or persona injection rules.
- Add new LLM roles or change bundled personas/agents (reuse existing).
- Full TUI rewrite (enhance existing panes + catalog).
- External queue (NATS/Redis) or persistent cross-session fleet (scope to one parent session + durable within it).
- Automatic merging of worktree changes (parent still reviews/applies as today via git tools).
- Support for sub-fleets inside child subagents initially (depth still applies; future extension).

---

## Proposed Design

### Core Components (Direct Flume Mapping)

**1. FleetOrchestrator (== Flume `Manager` in `internal/worker/manager.go`)**
- Central heartbeat. Owns `DelegationQueue`, `DelegationClaimer`, `SubagentPool`, `FleetSweeper`.
- `wakeChan` (buffered) for immediate cycles on enqueue/kill.
- `Run(ctx)`: ticker (config `fleet.poll_seconds`, default 5) + wake select; first cycle immediate. On shutdown: `pool.Shutdown`.
- `cycle()`: analogous to Flume L170:
  - Check "paused" (global or per-fleet config flag).
  - `sweeper.RunThrottled(ctx)`.
  - Pre-flight: count delegations by target status (queued/ready equivalents).
  - Fetch "busy" (running delegations mapped to their subagent task_ids).
  - "Node caps": harness equiv is max concurrent subagents (global config + per-model or CPU-aware; start with semaphore default 8-16, adaptive on observed latency).
  - `pool.Sync(...)` for claimed.
  - Publish snapshot (in-memory + to TUI + optional durable "fleet-state.json").
- `TriggerSweep(sweepName)` for manual (e.g. "stuck", "promote").
- Pseudocode sketch:
  ```go
  func (o *FleetOrchestrator) cycle(ctx) {
      if o.isPaused() { return }
      o.sweeper.RunThrottled(ctx)
      avail := o.countByStatus(ctx)  // "queued", "ready" etc.
      busy := o.fetchActiveDelegations()
      caps := o.fetchCaps()
      for _, d := range o.selectClaimable(avail, busy, caps) {
          if claimed := o.claimer.TryAtomicClaim(d); claimed != nil {
              o.pool.Submit(claimed.delegationID, func() { o.spawnAndRun(claimed) })
          }
      }
      o.publishSnapshot()
  }
  ```

**2. DelegationQueue + Claimer (== Flume `Claimer.TryAtomicClaim` + query guards in `claim.go`)**
- In-memory queue (ring or priority) + durable records.
- `Claimer.TryAtomicClaim(delegation)`:
  - Pre-flight avail.
  - Dedup (normalize description/title; optional semantic via harness embed if available).
  - "WIP" gate: per-"project" (workspace/repo) or global max concurrent running.
  - Atomic claim: file-based OCC (seq in meta.json or rename temp) or in-mem CAS under lock. Set `active_subagent`, `claimed_at`, status transition via state machine.
  - Guards: exclude already-decomposed (if using child fleets later), paused fleets, depth.
- `roleToTargetStatus` mapping (general-purpose -> "queued" etc.; or use capability/persona as "role").

**3. SubagentPool (== Flume `Pool` in `pool.go`)**
- Wraps existing `spawn_subagent(..., background: true)` + `get_...`.
- Uses `golang.org/x/sync/semaphore.Weighted` (or harness equiv semaphore) for backpressure.
- `Submit(delegationID, fn)`: acquire, go-routine that calls spawn, wires monitor if needed, on complete: update delegation record + release.
- `SyncDelegations(state)`: for each claimed, submit the runner.
- Graceful shutdown with wg.
- Reuses harness `kill_command_or_subagent` for cancellation.

**4. FleetSweeper (== Flume `Sweeper` + 8+ sweeps in `sweeps.go`)**
- Mutex + `lastRun` map + intervals (stuck:30s, promote/adaptive:2-5s, recon:45s, etc.).
- `RunThrottled`:
  - `requeueStuckDelegations`: running > timeout (5-10min configurable), no heartbeat; reset to queued, clear active, emit reasoning.
  - `promoteQueued` (adaptive): move "planned"/"enqueued" to "ready" when parent delegation done or deps met (handoff files exist + status).
  - `resumeBlocked`: recover "blocked" (non-intentional) if attempts < max.
  - `reconcileChildCounts` / lineage (for nested fleets).
  - `parentCompletionSweep`: if all child delegations terminal, mark parent done.
  - `blockOverloaded`: if pool at cap * 2, block new claims.
  - Consensus-like if review fleets used.
- `ExecuteResumeSweep`, etc. exposed for `fleet_sweep` tool.
- Throttling prevents thundering herd on local LLM.

**5. DelegationStateMachine (== Flume `TaskStateMachine` + `ValidTransitions` in `pkg/types/types.go` + `validation.go`)**
- States: `enqueued` (or `planned`), `queued`/`ready`, `running`, `review` (optional for review fleets), `done`, `blocked`, `archived`, `cancelled`.
- `ValidTransitions` map (port of Flume's; allow recovery paths).
- `EnforceTransition(current, target)` + shadow mode (env `GROK_FLEET_STATE_MACHINE_SHADOW_MODE`).
- `Enforce...OrLog` called by all writers (claimer, pool runner, sweeper, manual kill).
- Evidence for terminal (e.g. summary_file present + child exit 0).
- Centralized in new `internal/fleet/state.go` (harness equiv of pkg/types).

**6. Telemetry Bridge (== Flume `flumelogger.LogAgentReasoning` + ES append in `internal/logger/logger.go` + manager wiring)**
- On child subagent spawn, the orchestrator can register a streaming listener or poll + surface deltas.
- `LogDelegationReasoning(delegationID, role, text, meta)`: writes to parent conversation as "agent reasoning" (or dedicated fleet pane), appends to delegation record's `execution_thoughts[]` (jsonl or array in meta), emits for TUI popout.
- Child's internal thoughts (via harness subagent reasoning capture) bridged upward when `resume_from` or fleet parent link present.
- Worker snapshots: periodic `FleetSnapshot` published to TUI + durable (analog `ClusterState`).
- Node registry equiv: simple local "subagent capacity" tracking.

**7. Durable + Ephemeral Records**
- Ephemeral: in-memory maps (active delegations, last heartbeat).
- Durable (per session or `~/.grok/fleets/<fleet-id>/`): 
  - `delegation-<id>.json`: full record (id, parent_session, enqueued_at, status, subagent_id (when spawned), description, prompt_ref (or inline), resume_from, persona_tag, capability, isolation, claimed_at, active_subagent, attempts, error, handoff_files: {review_file, summary_file, ...}, child_delegations[], thoughts: []ThoughtEntry).
  - `fleet-meta.json`: fleet_id, created, config, status.
  - `index.json` or simple dir scan for list.
- Use atomic write (temp + rename) + file locks for OCC (like Flume script + OCC).
- On resume (session load): rebuild in-memory from durable + re-attach to live subagent task_ids via existing mechanisms.
- Subagent `meta.json` remains the source for the child session; fleet record links bidirectionally (parent_session + child link).

### Runtime & Language Mapping for Harness

**Critical addition per review (Major 1).** The original design ported Flume's Go in-process model (ticker + wakeChan + goroutines + `semaphore.Weighted` + dedicated sweeper goroutine) too literally. The harness is a conversational agent runtime (turn-based agent sessions, compaction, `~/.grok/bin/grok`, session JSONL + meta.json artifacts, skills as .md + .py, MCP/ACP, pager TUI, primitives like `scheduler_create`/`monitor`/`/loop`, `spawn_subagent` + shared background surfaces, no exposed long-running Go service internals under ~/.grok or harness refs). "The orchestrator is a small goroutine/loop equiv in TS/whatever harness lang" was noted in Risks but not resolved; pseudocode/PR1 assumed straightforward port.

**Harness-native implementation (no new core heartbeat assumed for v1):** 
- **Cycle / heartbeat / RunThrottled**: Not a dedicated goroutine/ticker inside an agent turn (conflicts with compaction/turn model per 20-background-tasks.md and review). Instead:
  - Use `scheduler_create` (recurring, `durable: true` opt-in like some monitors) or `/loop <interval> "fleet_recon_or_sweep"` (convenience wrapper; fires new agent turn per 20:81 "Each firing creates a new agent turn"; max 50 active, auto-expire 7d).
  - Or `monitor` tool for file-change event streams (e.g. `inotifywait` or poll on `~/.grok/fleets/.../*.json` with `grep --line-buffered`).
  - Low-cost scheduled "recon" prompt or internal skill logic that calls `fleet_sweep` / status / reconcile (throttled to avoid LLM cost).
  - Wake on enqueue: immediate `scheduler_fire` equiv or enqueue triggers a one-shot via background task.
- **Sweeper (RunThrottled + cadenced sweeps)**: Implemented in the fleet skill (or core extension) using scheduler/monitor as the driver for `RunThrottled` (mutex/lastRun/intervals emulated in file state or in-memory per turn). Sweeps read durable `delegation-*.json`, apply `FleetSweeper` logic (stuck requeue via meta.json cross-check, promote on handoff_files existence, etc.), write with EnforceOrLog. Use file polling or `monitor` for liveness instead of internal ticker. Throttling via adaptive intervals + explicit caps (cf. pr-babysit 8).
- **Claimer (TryAtomicClaim + OCC + guards)**: Pure file-based (atomic temp+rename + optional flock/seq in json for OCC, like Flume script + if_seq/prim; belt-and-suspenders raw+struct). No goroutine; invoked from cycle/sweep/enqueue turns. Dedup (norm + optional semantic via harness vec0/embed if surfaced, per 13-memory.md:97 "vec0 vector search for semantic similarity"). WIP via semaphore or count in fleet-meta. Guards: depth (existing harness), paused, decomposed-equivalent via parent links.
- **Pool / Submit / backpressure**: Wraps `spawn_subagent(..., background: true)` + `get_*` / `kill_*`. Bounded concurrency via in-skill semaphore (or harness equiv; default 8-16 cf. Flume/pr-babysit). Submit = claim record + spawn; on complete update record + release. Graceful via existing wait/kill. No fire-and-forget; every subagent tracked in durable + todo phases.
- **State / Enforce / durable**: File records under `~/.grok/fleets/<fleet-or-parent-session>/` (or per-session fallback). Atomic writes + seq for OCC. On resume/compaction: scan + rebuild in-memory from durable + re-attach live `subagent_id`s via `get_command_or_subagent_output` (addresses staleness). `DelegationStateMachine.EnforceTransitionOrLog` (shadow default via env) called at 100% writers (in skill or thin core shim).
- **Runner / spawn**: Delegates to existing `spawn_subagent` (reuse all semantics: resume_from, worktree, capability, persona-via-prefix, depth). Bridge reasoning via polling get_ + parse or monitor on child outputs + append to `thoughts[]` + `LogDelegationReasoning` (best-effort, like Flume 2s ctx + retry on 409). Handoff files (summary_file etc.) per persona contracts.
- **Telemetry / TUI / ACP**: Bridge to existing surfaces (Ctrl+T grouping via pager hooks or stub, Ctrl+; queue, session updates). ACP: new tools inherit permission_mode/YOLO like spawn_subagent.
- **todo_write integration**: Fleet orchestrator (in skill) uses `todo_write` (merge:false init, then merge:true) for its own phases (e.g. setup/enqueue/claim/sweep/wait), per implement/pr-babysit patterns + reliable-go "structured task tracking for >3 steps".
- **Observability**: Structured logs + reasoning entries on every decision (claim, sweep, transition, Enforce violation). `FleetSnapshot` periodic via scheduled recon.

**Pseudocode (harness-native, scheduler/monitor/file driven; not Go):**
```js
// In fleet skill or scheduled recon turn (triggered by scheduler_create or /loop 5s)
async function cycle(ctx) {
  if (isPaused()) return;
  await sweeper.runThrottled(ctx);  // file reads + EnforceOrLog writes; cadences via lastRun in fleet-meta or scheduler state
  const avail = await countByStatus();  // scan durable delegation-*.json
  const busy = await fetchActiveDelegations();  // cross-ref live via get_command_or_subagent_output
  const caps = await fetchCaps();  // config + observed
  for (const d of selectClaimable(avail, busy, caps)) {
    const claimed = await claimer.tryAtomicClaim(d);  // file OCC (temp+rename + seq), guards, dedup, WIP
    if (claimed) {
      await pool.submit(claimed.delegationID, async () => {
        await spawnAndRun(claimed);  // calls spawn_subagent(background:true, ...), wires monitor if needed, updates record
      });
    }
  }
  await publishSnapshot();  // to durable + TUI via existing
}

// Example heartbeat setup (one-time, e.g. on first fleet_enqueue or skill load)
scheduler_create({
  interval: "5s",
  prompt: "Internal fleet cycle recon (low-cost; call fleet_sweep if needed; no LLM if quiet)",
  recurring: true,
  durable: false,  // or true for cross-session
  fireImmediately: true
});
// Or user-visible: /loop 30s fleet_status --active && report
// For file events: monitor("inotifywait -m ... | grep ...", {persistent:true})
```

**Runner equiv (inside submit):**
```js
async function runDelegation(d) {
  // persona via prefix convention (see Verification Appendix)
  const result = await harness.spawn_subagent({
    subagent_type: d.subagent_type,
    description: d.description,  // includes [tag]
    prompt: (d.persona_tag ? loadPersona(d.persona_tag) + "\n---\n" : "") + d.prompt,
    resume_from: d.resume_from,
    background: true,
    isolation: d.isolation,
    capability_mode: d.capability_mode,
  });
  d.subagent_id = result.subagent_id;
  d.worktree_path = result.worktree_path;
  d.status = "running";
  // optional: start monitor on child output for LogDelegationReasoning deltas
  await waitForCompletionOrTimeout(d.subagent_id);  // via get_ block or monitor
  // on done: read handoff_files, Enforce transition, emit reasoning
  await updateDelegationRecord(d, {enforce: true});
}
```

**1-2 day runtime spike / prototype (PR0, per review):** Use *only* existing surfaces (`scheduler_create`, `monitor`, `spawn_subagent`+`get_command_or_subagent_output`+`kill_*`+`wait_*`, `todo_write`, `run_terminal_cmd` for any polling helpers, `read_file`/`list_dir`/`write` for durable fleet-*.json + atomic, `todo_write` for phases). Prototype minimal FleetOrchestrator + basic enqueue/status/sweep in the fleet skill (extend existing stub at `~/.grok/bundled/skills/fleet/SKILL.md`) + file records. No core changes in spike. Document findings (e.g. "scheduler fires turn reliably for recon", "monitor good for file events", "compaction reseed via todo scaffold works as in implement:45", "TUI grouping requires pager hook or stub via existing Ctrl+T"). If core extensions needed for first-class (e.g. native claim in task mgr, streaming thoughts hook, fleet tool schema in ACP), hand off to ACP/harness runtime team. Update design + 23- doc + fleet skill from spike results before PR1.

**Architecture notes:** The "manager" lives as skill-orchestrated logic + scheduled tasks (alt5 path elevated below). Durability + state machine + sweeps provide the reliability even without core loop. Coexistence: fleet tools additive; raw spawn always available. This honors "minimal core change" while delivering the motivating recovery/backpressure/observability.

(See also elevated Alternative 5 below for pure-skill-first path; current fleet/SKILL.md:8 and :38 already implement this mapping.)

### Concrete Mapping Table (Flume Concept → Harness Equivalent)

| Flume (file:func)                  | Harness Fleet Equivalent                          | Notes / Citations |
|------------------------------------|---------------------------------------------------|-------------------|
| `Manager` + `cycle()` (manager.go:40,170) | `FleetOrchestrator` + `cycle()` | Heartbeat, preflight, publish snapshot. Wake on enqueue. (Updated for harness: via scheduler/monitor in Runtime section.) |
| `Claimer.TryAtomicClaim` (claim.go:56) + guards (`decomposed_at`, raw+struct, dedup, WIP, git overlap, OCC `UpdateDocOCC`) | `DelegationClaimer.Claim` | In-mem/file OCC (seq/rename). Dedup on desc. WIP per workspace. No git for now (opt-in). |
| `Pool` + semaphore + `Submit`/`SyncWorkerProcesses`/`Shutdown` (pool.go) | `SubagentPool` (wraps `spawn_subagent(background:true)` + harness get/kill) | Same Weighted semaphore (default 8-16). |
| `Sweeper` + `RunThrottled` + lastRun/intervals + 8 sweeps (sweeps.go:34,72) e.g. `requeueStuckImplementerTasks`, `promotePlannedTasks` (mget cache, depth, Enforce), `ExecuteResumeSweep` (PM patterns skip), `childCountReconciliationSweep`, `parentCompletionSweep` | `FleetSweeper` + `RunThrottled` + specialized (stuck, promote for handoffs/deps, resume, block, lineage recon, parent completion) | Adaptive promote. Depth still enforced by harness. (Harness: scheduler/monitor driven per Runtime.) |
| `handlePM`/`handleImplementer` + streaming `ChatStream` + `LogAgentReasoning` per delta (runner.go:794,242,323) + `updateTaskLeaseState` | Fleet runner uses `spawn_subagent` (or direct if internal); bridge child reasoning via `LogDelegationReasoning` + thoughts append | No new PM role; fleet for parallel *delegations*. Lease via state machine. |
| `TaskStateMachine` + `Enforce...` + `ValidTransitions` (types.go:356,403; validation.go:33) + `MAX_HIERARCHY_DEPTH` | `DelegationStateMachine` (same) + Enforce at every status write | Shadow default. States adapted (enqueued/queued/running/done/blocked). |
| `flumelogger.LogAgentReasoning` + `appendExecutionThoughtNonBlocking` + script + `SetESBridge` (logger.go:364,273; manager.go:89) + worker snapshots + node registry | `LogDelegationReasoning` + thoughts[] in delegation json + TUI bridge + `FleetSnapshot` | Surfaces to Ctrl+T enhanced + parent stream. |
| `roleToTargetStatus` (claim.go:448) | Same or `personaToTargetStatus` | e.g. implementer persona -> ready-for-claim. |
| Project `WorkPaused`, WIP limits, plan budgets, node caps + latency brake (claim.go, manager.go:454, sweeps) | Fleet config + workspace WIP + global cap + simple latency backoff | Start simpler; use existing harness rate limits. |

**Architecture Diagram (Mermaid)**

```mermaid
graph TD
    Parent[Parent Agent<br/>fleet_enqueue calls] -->|enqueue delegation records| Queue[DelegationQueue<br/>+ durable *.json]
    Queue --> Claimer[DelegationClaimer<br/>TryAtomicClaim<br/>OCC + guards + dedup + WIP]
    Claimer -->|claimed| Pool[SubagentPool<br/>semaphore.Weighted<br/>Submit + spawn_subagent(background)]
    Pool -->|runs| HarnessSpawn[spawn_subagent<br/>(reuse: resume_from, persona prefix, worktree, capability)]
    HarnessSpawn --> SubSess[Child Subagent Session<br/>own context + meta.json]
    SubSess -->|output + reasoning| Bridge[Telemetry Bridge<br/>LogDelegationReasoning<br/>thoughts append to delegation]
    Bridge --> TUI[(TUI: Ctrl+T tasks<br/>grouped by fleet<br/>Ctrl+; queue<br/>reasoning popout)]
    Sweeper[FleetSweeper<br/>RunThrottled<br/>stuck 30s, promote adaptive 2-5s,<br/>resume, block, parent_comp, recon 45s] -->|periodic| Queue
    Sweeper -->|requeue / promote / resume| Claimer
    Manager[FleetOrchestrator<br/>cycle + wakeChan<br/>preflight counts + caps<br/>publish FleetSnapshot] --> Sweeper
    Manager --> Pool
    Parent -->|fleet_status / fleet_wait / fleet_sweep| Manager
    Manager -->|durable updates| Records[delegation-*.json<br/>fleet-meta.json<br/>atomic write + lock]
    Note: Scheduler/Monitor (existing harness) drive cycle/Sweeper (see Runtime section)
```

**Sequence Diagram (Reliable Parallel Batch, e.g. 5-contract job)**

```mermaid
sequenceDiagram
    participant P as Parent Agent
    participant F as FleetOrchestrator
    participant C as Claimer
    participant Po as SubagentPool
    participant S as Subagent (via spawn)
    participant Sw as FleetSweeper
    P->>F: fleet_enqueue 5x (desc, prompt, persona_tag="[implementer]", isolation=worktree, background=true)
    F->>F: write durable delegation records (status=enqueued)
    F-->>P: immediate delegation_ids (5)
    Note over P: parent continues (small context)
    loop Heartbeat cycle (5s + wake; via scheduler_create or /loop per Runtime section)
        F->>Sw: RunThrottled (promote etc.)
        F->>F: preflight avail counts
        F->>C: select claimable
        C->>C: TryAtomicClaim (OCC, guards, WIP)
        C-->>F: claimed delegation
        F->>Po: Submit (delegationID)
        Po->>S: spawn_subagent(..., resume_from if any)
        S-->>Po: subagent_id + worktree_path
        Po->>F: update record (status=running, subagent_id)
        Note over S: child runs (119 tool calls etc.); streams reasoning
        S->>Bridge: (internal) thoughts -> bridged LogDelegationReasoning
    end
    loop On completion or stuck
        Sw->>F: detect done / stuck (timeout or meta.json complete)
        F->>F: update status=done (Enforce + thoughts)
        Po->>Po: release semaphore
    end
    P->>F: fleet_wait(delegation_ids, block or poll) or fleet_status
    F-->>P: results + handoff file paths + aggregated reasoning summary
    P->>P: read summary_files, merge if needed
```

**Runner Pseudocode (inside pool submit)**
```pseudocode
func (o *FleetOrchestrator) runDelegation(d *Delegation) {
    // enrich prompt with persona if tagged (per skill patterns)
    prompt = loadPersona(d.persona_tag) + "\n---\n" + d.prompt
    // or let caller have prepended
    result := o.harness.spawn_subagent(
        subagent_type: d.subagent_type,
        description: d.description,  // has [tag]
        prompt: prompt,
        resume_from: d.resume_from,
        background: true,
        isolation: d.isolation,
        capability_mode: d.capability_mode,
    )
    d.subagent_id = result.subagent_id
    d.worktree_path = result.worktree_path
    d.status = "running"
    // optional: start a monitor goroutine that tails child output/reasoning
    // and calls LogDelegationReasoning on deltas
    // (harness: use monitor tool or scheduled get_ block, per Runtime section)
    waitForCompletionOrTimeout(d.subagent_id)
    // on done: read any handoff files mentioned in prompt, update d.status=done
    // emit final LogDelegationReasoning with exit code + summary
    o.updateDelegationRecord(d, enforce=true)
}
```

---

## API / Interface Changes

### New Tools (agent-callable; modeled on existing `spawn_subagent` + Flume dispatch)
All new tools are registered when feature enabled. They coexist with `spawn_subagent`, `get_command_or_subagent_output`, `wait_commands_or_subagents`, `kill_command_or_subagent`, `todo_write`, etc. (Registered following `spawn_subagent` convention per Verification Log.)

1. `fleet_enqueue`
   - Params (like spawn + fleet extras):
     - `description: string` (required; use `[implementer]` prefix convention for label)
     - `subagent_type: "general-purpose" | "explore" | "plan"` (default general)
     - `prompt: string` (full prompt; parent can include persona instructions + handoff paths like `review_file`)
     - `resume_from?: string` (subagent_id)
     - `persona_tag?: string` (for label + optional prompt prefix; e.g. "implementer")
     - `capability_mode?: "read-only" | "read-write" | "execute" | "all"`
     - `isolation?: "none" | "worktree"` (default per type)
     - `background?: bool` (default true for fleet)
     - `parent_delegation_id?: string` (for hierarchy/nesting fleets)
     - `depends_on?: string[]` (sibling delegation IDs for promote)
     - `handoff_files?: object` (e.g. `{ "review_file": "/tmp/...", "summary_file": "..." }` — for IO contracts)
   - Returns: `{ delegation_id: "fleet-019e...", subagent_id?: "...", status: "enqueued" }` (immediate; non-blocking)
   - Behavior: writes durable record, wakes orchestrator, enqueues. Does *not* spawn yet (claimer/pool does).

2. `fleet_status`
   - Params: `delegation_ids?: string[]` (omit for all in this fleet/session), `include_thoughts?: bool` (capped), `include_children?: bool`
   - Returns: array of delegation records (status, timings, subagent_id, handoff paths, recent thoughts, error, attempts, lineage).

3. `fleet_wait`
   - Params: `delegation_ids: string[]`, `block?: bool` (default false), `timeout_ms?: number` (like get_)
   - Returns: per-id final status + output summary + exit info. Blocks only on requested.
   - Reuses/implements over existing wait + polling.

4. `fleet_kill`
   - Params: `delegation_id: string`, `force?: bool`
   - Kills underlying subagent (via kill_command_or_subagent) + marks cancelled. Wakes.

5. `fleet_sweep`
   - Params: `sweep: "stuck" | "promote" | "resume" | "all" | "parent-comp" | ...`
   - Triggers manual sweep synchronously (for debugging/recovery). Returns counts affected.

6. `fleet_list` (or via status with no ids): list active fleets + high-level counts.

7. Optional: `fleet_configure` (WIP caps, timeouts, shadow mode per fleet).

**Before/After for Parallel Work (e.g. implement skill style)**
- **Before** (manual, from implement/SKILL.md + pr-babysit):
  ```js
  // launch 5
  id1 = spawn_subagent({subagent_type:"general-purpose", description:"[implementer]...", prompt: "...", background:true})
  ... 4 more
  // manual track array of ids
  // later
  for each: get_command_or_subagent_output(id, block:true, timeout...)
  // babysit resume_from in state file
  ```
- **After** (fleet; parent context tiny):
  ```js
  fleet = fleet_enqueue([
    {description:"[implementer] fix-install", prompt: full + "write to /tmp/summary-xxx", ...},
    ... 4 more with depends_on or parent_delegation
  ])
  // ids returned immediately
  // later in loop or via /loop
  statuses = fleet_status(fleet.delegation_ids)
  if all done: results = fleet_wait(...)
  // or fleet_wait(block) for simple
  ```

**Integration Points**
- `resume_from`: passed through to underlying spawn; fleet record stores chain.
- Personas: unchanged (prompt prepend + desc `[tag]`); fleet can auto-inject based on `persona_tag` for convenience.
- TUI: delegations grouped under fleet in Ctrl+T (expandable); Ctrl+; shows fleet tasks distinctly. Subagent catalog extended with "fleet" actions.
- ACP: new tool calls surface in session/update.
- Depth: fleet children count toward harness depth limit.
- `/loop` example: `/loop 30s fleet_status --active && report stuck`

---

## Data Model Changes

**No changes to existing subagent `meta.json` or session format.** Additive only.

**New (durable, per-session or global-under-~/.grok/fleets/ for cross-session if durable=true like scheduler):**
- Directory: `~/.grok/sessions/<encoded-wd>/fleet-<fleet-uuid>/` or top-level `~/.grok/fleets/<session-or-fleet>/`
  - `fleet-meta.json`:
    ```json
    {
      "fleet_id": "fleet-019e8677-...",
      "parent_session_id": "...",
      "created_at": "2026-06-01T...",
      "config": {"max_concurrent": 16, "stuck_timeout_min": 5, "shadow_state_machine": true},
      "status": "active"
    }
    ```
  - `delegation-<id>.json` (one per; or batched index):
    ```json
    {
      "delegation_id": "del-019e8677-614d-...",
      "fleet_id": "...",
      "parent_session_id": "...",
      "status": "running",  // per DelegationStateMachine
      "subagent_type": "general-purpose",
      "description": "[implementer] fix-install",
      "prompt_ref": "/tmp/prompt-xxx.txt" /* or inline if small */,
      "resume_from": null,
      "persona_tag": "implementer",
      "capability_mode": "all",
      "isolation": "worktree",
      "enqueued_at": "...",
      "claimed_at": "...",
      "active_subagent_id": "019e8677-614d-7012-99fa-3cf95a8cd0e0",
      "worktree_path": "/Users/.../worktrees/.../subagent-...",
      "attempts": 1,
      "max_attempts": 3,
      "error_message": null,
      "handoff_files": {"summary_file": "/tmp/summary-xxx.md"},
      "depends_on": [],
      "child_delegation_ids": [],
      "parent_delegation_id": null,
      "thoughts": [  /* capped or rotated; full in subagent logs */
        {"ts": "...", "agent_role": "implementer", "thought": "Starting...", "meta": {"turn":1}}
      ],
      "updated_at": "...",
      "completed_at": null
    }
    ```
- Ephemeral: `FleetOrchestrator` holds `map[delegationID]*Delegation`, active pool count, lastRun for sweeper.
- Migration: none (new feature). On enable, existing manual subagents continue; fleet only for new enqueues.
- Storage estimate: ~2-5KB per delegation record (thoughts capped at 50 or streamed to sidecar). For 20-delegation fleet: <100KB. Subagent transcripts remain in their session logs (separate).

State transitions always via `DelegationStateMachine.Enforce...` before write.

---

## Alternatives Considered

1. **Pure skill-based (no core changes)**: Enhance `/implement`, `pr-babysit`, new `fleet-skill.md` that implements manager/claimer/sweeper logic *inside* a long-running orchestrator subagent using only existing `spawn_subagent` + `get_` + files for state.
   - Trade-offs: Zero harness changes (fast to prototype). But: parent context still polluted by orchestrator transcript; no native TUI integration (Ctrl+T sees flat subagents); sweeps run only while orchestrator alive (no background heartbeat); backpressure/semaphore has to be emulated in prompt/loop; recovery after compaction/orchestrator death is manual; cannot be "first-class primitive". Loses the defense-in-depth of core (OCC, central enforcer). Rejected for scale/reliability goals.

2. **Pure TUI-driven (manual orchestration UI only)**: Add "Fleet" button in Ctrl+T that lets user manually group existing subagents, set deps, trigger sweeps via UI only. No new agent tools.
   - Trade-offs: Good for human-in-loop visibility. But does nothing for agent-driven parallel work (the 5-contract case, /implement with effort=5); agents still do manual tracking. No backpressure/claiming in the delegation path itself. Rejected as incomplete.

3. **Simple in-memory queue (no durable, no sweeper, no claimer)**: Just a `FleetManager` that on enqueue immediately `spawn_subagent(background)` up to cap, tracks map[id] in memory, exposes status/wait.
   - Trade-offs: Simpler/faster to ship (1-2 PRs). Matches current babysitting but centralized. Loses: recovery after restart/compaction (the exact "still running" bug observed), stuck sweeps, backpressure under load (thundering), dedup/WIP, hierarchy promote, telemetry bridge to records. For long-running fleets (hours) this regresses reliability. Rejected; we already know from Flume this is insufficient.

4. **Full external queue (NATS/Redis + sidecar process)**: Offload to real queue + workers.
   - Trade-offs: Excellent durability/scalability/observability. But: massive new dependency for the harness (local-first tool), deployment complexity (Docker? always-on service?), security surface (queue auth), latency for local dev, overkill for the single-user interactive TUI use case. Does not integrate cleanly with existing subagent sessions/worktrees/TUI. Rejected for scope; the Flume model (embedded manager + local durable) is the right fit.

5. **Harness-native fleet via extended scheduler + monitor + file-state orchestrator skill (no new core heartbeat)**: [Full description as in my earlier design thought; trade-offs detailed; see elevated section in v0.2 body above. Current implementation path per fleet/SKILL.md.]

Chosen: embedded orchestrator + durable files + reuse of spawn path (balanced, directly modeled on proven Flume, incremental value). Alt5 provides the harness-native realization with reduced core risk.

---

## Security & Privacy Considerations

**Threat Model**:
- Parent delegates to children with same permissions as today (capability_mode, worktree isolation prevents cross-worktree writes without explicit merge).
- New surface: `fleet_enqueue` could be abused to spawn flood if no caps (mitigation: pool semaphore + WIP gate + global `max_fleet_delegations` config + rate limit on enqueues per turn).
- Claim races / status fights: mitigated by state machine Enforce + atomic file OCC (rename or lockfile + seq) + shadow mode audit.
- Resume hijack: `resume_from` validation (must be valid child of parent session or same fleet lineage); harness already enforces session ownership.
- Data leakage: delegation records contain prompts (may have secrets); store under 0600 like `implement-memory` (see implement/SKILL.md memory helper chmod). Thoughts may contain intermediate reasoning with code snippets—same as current subagent logs.
- ACP exposure: new tools must respect existing permission_mode / YOLO; tool calls go through same ACP session/update + permission hooks. New fleet tools registered with same permission_mode checks as spawn_subagent; delegation records respect parent session ACLs; no new YOLO surface. ACP schema updates (if any) limited to adding the 6 fleet_* entries mirroring spawn_subagent. (See 22-permissions-and-safety.md and 07-mcp-servers.md.)
- Worktree: fleet uses same `isolation:"worktree"` — no change. Cleanup still manual or via existing grok worktree cmds.
- Paused fleets: support `fleet_pause` / emergency stop (analog Flume WorkPaused) to halt claiming without killing running.

**Auth/Handling**: No new creds. All delegation state is local to user. If cloud subagents or MCP, inherit existing. Audit: every status write + claim emits `LogDelegationReasoning` (system role) for traceability.

**Privacy**: Delegation records are user data; provide `fleet_purge` tool + session cleanup on delete. Do not upload to any xAI unless explicit.

Risks called out in Rollout.

---

## Observability

- **Logging**: All key paths use structured (slog equiv in harness): "fleet enqueue", "delegation claimed", "stuck sweep requeued N", "state transition" with delegation_id, subagent_id, latency, reason. Analog to Flume's rich slog + Logloom.
- **Reasoning Streams**: `LogDelegationReasoning(delegation_id, role, delta, meta)` — emitted on child stream deltas (if harness surfaces subagent thoughts) and on internal fleet events (claim, promote, stuck recovery). Appended to delegation json `thoughts[]` (best-effort non-block like Flume 2s script + 4x retry on conflict). Surfaces in TUI task pane "reasoning" drawer per delegation/subagent (enhanced from current).
- **Metrics (conceptual, via logs or future counters)**: `fleet_delegations_enqueued_total`, `fleet_claim_latency_ms`, `fleet_stuck_requeues_total`, `fleet_active_concurrent`, `fleet_state_violation_total{shadow=1}`, child tool call counts (from subagent outputs), parent context savings (est. via record size vs transcript).
- **Snapshots**: Periodic `FleetSnapshot` (active delegations, pool count, last sweep times, node/cap equiv) written to durable + exposed via `fleet_status` + TUI. Analog `ClusterState` + worker snapshots.
- **Alerts (user-facing)**: On stuck > threshold or cap throttle, emit user message + reasoning entry. `/loop` can poll `fleet_status` for custom.
- **TUI**: Ctrl+T shows fleet groups (tree: fleet > delegations > subagents), status badges, recent thought preview, "sweep now" action. Ctrl+; distinguishes fleet tasks. Subagent catalog gets "Enqueue to fleet" entry.
- **Debug**: `fleet_sweep "all"`, `fleet_status include_thoughts=true`, harness logs with FLEET_DEBUG=1. Bridge failures never silent (log + continue).

Wiring reuses any existing subagent thought capture in harness core (via get_ + monitor per 20-background-tasks.md, or future hook). Assumption: harness subagent sessions emit structured reasoning deltas (or accessible via existing get_* + parse); if only full transcripts, bridge falls back to post-completion summary extraction from summary_file + last N turns (per persona contracts e.g. implement/SKILL.md). Confirm in harness core or add minimal capture hook in PR5. (See Verification Log for sources.)

---

## Rollout Plan

1. **Feature Flag (day 1)**: `GROK_FLEET_ORCHESTRATOR=1` (env) or `[fleet] enabled = true` in `~/.grok/config.toml`. Default off. When off, new tools are no-ops or hidden; old paths 100% unchanged.
2. **Coexistence**: `spawn_subagent` untouched. Fleet tools are additive. Existing manual patterns in skills continue to work (and can be incrementally updated to use fleet in later PRs for value).
3. **Staged**:
   - Internal / canary: enable for harness devs + bundled skills testing.
   - Opt-in users: docs + /fleet-help.
   - Default on after 2-4 weeks zero-violation shadow audit + telemetry.
4. **Shadow Mode**: State machine + key transitions default shadow (log violations but proceed). Env to flip.
5. **Data**: New dirs/files only on first enqueue; no impact on existing sessions.
6. **Rollback**: Set flag=0 (or delete config); kills any in-flight fleet heartbeats cleanly (pool shutdown); running subagents continue as before (they are just children). Manual cleanup of fleet-*.json if desired. No schema migration.
7. **Docs**: Update 16-subagents.md + 20-background-tasks.md + new 23-fleet-orchestrator.md (or section). Skills can adopt gradually (e.g. pr-babysit first for its explicit parallel+resume+state-file pattern).
8. **Metrics Gate**: Gate default-on on observed "fleet adoption > X sessions" + "0 shadow violations in 7d" + "avg claim < 50ms".

---

## Risks & Mitigations (Severity)

- **High: Added complexity in core harness runtime (TUI + tool registry + task manager)**. Mitigation: narrow scope (reuse spawn path entirely; orchestrator is small goroutine/loop equiv in TS/whatever harness lang *or harness-native scheduler/monitor/file-state per Alt5 and Runtime section*), extensive tests (unit on state machine/claimer, integration replaying the 5-contract scenario), feature flag + full coexistence, incremental PRs. Owner: core team. (Revisited post-Alt5 elevation: risk qualified as "High if core loop chosen; mitigated to Low/Med if harness-native Alt5 selected for v1 (current path in fleet/SKILL.md).")
- **High: Stale views post-compaction (observed today)**. Mitigation: durable records + on-load reconstruction + reconciliation sweep (child_count style); explicit "last_heartbeat" from subagent meta; `fleet_status` always authoritative from disk + live task query.
- **Medium: Backpressure misconfig leads to under/over utilization of local LLM**. Mitigation: conservative default 8 (cf. pr-babysit), user-visible in `fleet_status` + caps, adaptive from observed subagent latencies (simple EWMA), manual `fleet_configure`, docs with "for 5-contract use 8-12".
- **Medium: TUI pane bloat / notification spam from child reasoning**. Mitigation: fleet grouping + collapse, cap thoughts per delegation in UI, user filter ("only fleet" vs all), volume control like monitor.
- **Medium: Inconsistent persona handling (docs vs skills)**. Mitigation: fleet respects *current* conventions (prompt prepend + desc prefix); provide `persona_tag` sugar that does the right thing without changing core.
- **Low: Performance (extra files/locks per delegation)**. Mitigation: small (KB), only on fleet path; atomic renames cheap; no impact on non-fleet.
- **Low: Nested fleets explode depth**. Mitigation: inherit/enforce existing depth limit; delegation records track hierarchy_depth; promote/claim guards like Flume MAX_HIERARCHY_DEPTH.
- **Ops: User confusion "when to use fleet vs spawn"**. Mitigation: clear docs + skill guidance ("use fleet_enqueue for >2 independent parallel delegations or long-running contracts"); fast path remains default in interactive.

---

## Open Questions

- **Blocking**: Exact tool registration surface in harness (is `spawn_subagent` the registered name, or `task`? Confirm in core tool schema vs docs/skills divergence). **Proposed**: Standardize on `spawn_subagent` (not "task") for new fleet_* tools. (See Verification Log for sources + resolution.)
- Default max concurrent (8 vs 16)? Tie to model or measured context/CPU?
- **Blocking**: Should fleet records be cross-session durable by default (like some schedulers) or strictly per-parent-session? **Proposed**: Per-parent-session default; opt-in durable via scheduler_create(durable:true).
- **Blocking**: TUI enhancement ownership: pager vs core? Can we ship basic fleet grouping without deep pager changes? **Proposed**: Promote minimal read-only to PR1 stub; investigate + spike explicit in PR1.
- **Blocking**: Semantic dedup: does harness have embed capability today for title norm fallback (like Flume LLM embed)? (Harness has vec0 per 13-memory.md:97; norm first, semantic opt.)
- Review fleets: does the state machine need explicit "review-consensus" or keep simple (running/done)?
- Integration with `todo_write`: should fleet enqueues auto-create todo items for phases?
- ACP: do fleet tools need special streaming updates beyond normal tool results?

---

## Pre-Implementation Checklist

- [x] Complete PR0 1-2 day runtime spike / prototype using only existing scheduler/monitor/spawn + file durable records. Document. (Delivered via subs + direct + live e2e test.)
- [ ] Resolve blocking OQs with spike + signoff. (See new OQ Status below; partial via config enable + live test.)
- [x] Re-verify all claims in Verification Log with fresh tools. (check-work PASS + live test + this run.)
- [x] Align with fleet/SKILL.md stub + 23- doc. (Updated.)
- [ ] Security/ACP review. (Reuse spawn; staged; recommend targeted sub review.)
- [~] e2e test skeleton for 5-batch. (Small 1-delegation live spawn+link+handoff done; full batch via scheduler in future session.)
- [x] Coexistence tests. (opt-in if + fallback in skills; manual still works.)
- [ ] Metrics gate plan.

## OQ Status / Decisions (post next-steps implementation)
Blocking open questions from v0.2 + review (documented decisions/proposals; signoff recommended before default-on):
- **Tool name / surface (spawn_subagent)**: Remains the execution substrate (Key Decision 1). fleet_* are skill-dispatched (orchestrate returns params for EMIT) for now. No change to core tool registry in this phase; additive. (Confirmed in live test.)
- **TUI (pager vs core ownership)**: Stubs in 23-/SKILL (Ctrl+T grouping by fleet_id via [tag] prefixes, reasoning drawer from bridged thoughts, sweep action). Minimal change spike in PR1/2 style recommended before deep pager or core task mgr changes. Use `fleet_status include_thoughts` + /loop for previews. (No core edit.)
- **Durability (per-session default + scheduler durable opt-in)**: File records under ~/.grok/fleets/<cwd-hash or parent>/ + recon sweep for rebuild on resume/compaction. scheduler_create(..., durable=false default per-session; opt-in true for cadences). Matches 20-background + design runtime. (Used in all tests.)
- **Embed/vec0 for semantic dedup**: Norm desc (current, Flume-style) sufficient for foundations. Future: use if vec0 surfaced per 13-memory.md (in claimer _find_duplicate). Not blocking now.
- **Security/ACP**: All delegation spawns reuse spawn_subagent params (permission_mode, YOLO, worktree isolation, depth). Records 0600 owner-only. No new privs. Recommend ACP audit sub on flows. (Live test used existing.)
- **Metrics**: Stub in returns (counts, planned, active_now); future Prometheus or internal. Not implemented yet.

Config.toml [fleet] orchestrator = true added + py support for enable check (env or config). SKILLs updated to use "python ... enabled".

These advance the checklist; full signoff + more e2e before flip. See 23- + live test execution for evidence.

---

## References

- Harness: `~/.grok/docs/user-guide/16-subagents.md` (full; agents/personas, task/spawn_subagent, resume_from, worktree, Ctrl+T/Ctrl+; , depth, capability_mode, persona IO contracts).
- Harness: `~/.grok/docs/user-guide/20-background-tasks.md` (background, get_command_or_subagent_output, kill_command_or_subagent, wait_*, /loop, monitor, scheduler_create, Ctrl+; queue pane).
- Harness usage patterns: `~/.grok/bundled/skills/implement/SKILL.md` (detailed spawn + resume + background parallel + manual ID tracking + get_ wait + persona prefix rules + todo scaffold), `~/.grok/bundled/skills/pr-babysit/SKILL.md` (parallel 8-cap, resume_from state, worktree, collect task_ids then block wait), `~/.grok/bundled/skills/review/SKILL.md` (single + spawn_subagent params), `~/.grok/bundled/agents/general-purpose.md` (mentions TaskTool).
- Flume (all under workspace `/Users/jonathandoughty/clients/fremenlabs/flume /flume`):
  - `internal/worker/manager.go` (Manager, cycle, processWorker, wakeChan, pre-flight, caps, ClusterState).
  - `internal/worker/claim.go` (Claimer, TryAtomicClaim, roleToTargetStatus, decomposed_at guard, raw+struct, OCC, dedup, WIP, git overlap).
  - `internal/worker/pool.go` (Pool, semaphore.Weighted, Submit, SyncWorkerProcesses, wg shutdown).
  - `internal/worker/sweeps.go` (Sweeper, RunThrottled, 8+ specialized, promotePlannedTasks unified, ExecuteResumeSweep, parentCompletionSweep, childCountReconciliationSweep, adaptive intervals, Enforce).
  - `internal/worker/runner.go` (handlePM with guards/atomic, handleImplementer multi-turn + AST gate + streaming, LogAgentReasoning, updateTaskLeaseState).
  - `pkg/types/types.go` + `validation.go` (TaskStateMachine, Enforce*, ValidTransitions, MAX_HIERARCHY_DEPTH, Task denorms, Evidence).
  - `internal/logger/logger.go` (LogAgentReasoning, LogStateTransition, appendExecutionThoughtNonBlocking + script bridge, SetESBridge).
- Recent usage: 5 subagents (specific IDs + call counts + durations) for Elastro/LogLoom 4-point close (detailed in conversation history) -- *illustrative example drawn from observed parallel usage patterns in recent sessions and skills (exact IDs from design context; high call counts and babysitting observed)*. See Verification Log.
- Related: Flume design/postmortems referenced in code comments (PM death spiral, 409 races, hierarchy deadlock P0, 74-task explosion, 258-item, field test observations).
- Verification performed via direct tool exploration (see Verification Log / Appendix and revision-v02.md).

---

## Verification Log / Appendix

This appendix provides traceability for key claims in the v0.2 design, based on direct tool exploration during the revision process (see `subagent-fleet-orchestrator-revision-v02.md` for full per-issue status, tool call list, and process; 30+ grep/read_file calls on sources listed below, plus live meta.json and Flume sources under the workspace).

**Harness claims verified (exact sources from tool results):**
- Tool registration / name divergence: `spawn_subagent` (literal in skills code and dispatch) vs "task tool" for UX in `~/.grok/docs/user-guide/16-subagents.md:46`. Confirmed in `implement/SKILL.md`, `pr-babysit/SKILL.md`, `review/SKILL.md`, and `fleet/SKILL.md` dispatch. New fleet_* tools use `spawn_subagent` convention.
- Persona param not supported on spawn: `implement/SKILL.md:59` quote "that parameter is not supported". Use description prefix convention e.g. "[implementer] ..." for pager label (see L317, 16-subagents.md "Persona IO Contracts", pr-babysit L419).
- 5 subagent IDs in motivation (e.g. 019e8677-614d-7012-99fa-3cf95a8cd0e0 "fix-install" with 119 tool calls): illustrative example drawn from observed patterns. Actual meta.json artifacts verified: `~/.grok/sessions/.../019e8576-.../subagents/019e8677-614d-.../meta.json` (tool_calls:119, description matching, status:completed, duration_ms); 4 siblings with 88-101 calls + Elastro/LogLoom desc. Parent session 019e8576-.... Confirmed via grep/read on sessions dirs.
- Scheduler_create / monitor / /loop for cycle/recon: `~/.grok/docs/user-guide/20-background-tasks.md:146` (params incl. durable, interval, prompt, recurring, fireImmediately; "Each firing creates a new agent turn"). /loop for user polling. Confirmed in fleet/SKILL.md:155 scheduler examples.
- Manual ID babysitting in skills: `implement/SKILL.md:404-419` (parallel reviewers: ids=(), spawn loop, get_command_or_subagent_output block waits); `pr-babysit/SKILL.md:407-439` (8-cap groups, state file with subagent_id, block waits). Opt-in fleet section added at end.
- Compaction staleness in get_ despite meta.json: observed in sessions; fixed via durable records + recon sweep + re-attach live subagent_ids via get_ (as in status enrichment). 20-*.md and meta.json schema verified.
- Ctrl+T / Ctrl+; TUI: 16-subagents.md "The Tasks Pane (TUI)", 20-background-tasks.md "The Queue Pane". Fleet grouping to be stubbed early per PR plan.
- No new core goroutine/Manager.Run: harness is turn-based (20-*.md, skills as .md/.py, no long-running Go service internals under ~/.grok). Cycle via scheduler/monitor/file polling only (confirmed in fleet/SKILL.md:35 "harness-native", design Runtime section).
- Flume 1:1 mappings (re-verified via grep/sed on live workspace sources):
  - Manager + cycle: `internal/worker/manager.go:170` (cycle L170, wakeChan, preflight, caps, ClusterState, SetESBridge L89).
  - Claimer.TryAtomicClaim + guards/OCC: `internal/worker/claim.go:56` (L56 TryAtomicClaim, decomposed_at L74/106/463, raw+struct belt-and-suspenders, OCC L313 with if_seq/prim, dedup/WIP, roleToTarget L448).
  - Pool: `internal/worker/pool.go` (semaphore.Weighted, Submit, Sync, Shutdown).
  - Sweeper + RunThrottled + 8+ sweeps: `internal/worker/sweeps.go:72` (RunThrottled L72, cadences L60-65, requeueStuck L77, promote L284 with mget/OCC/depth/Enforce, ExecuteResume L515, parentCompletion L842 2-pass, child recon).
  - LogAgentReasoning + bridge: `internal/logger/logger.go:364` (~L364 LogAgentReasoning + append + script + retries on 409).
  - TaskStateMachine + Enforce/shadow + MAX=6: `pkg/types/{types.go:356, validation.go:33}` (shadow/env, EnforceOrLog, ValidTransitions, MAX_HIERARCHY_DEPTH=6, Evidence).
  - handle* + streaming + Log* per delta: `internal/worker/runner.go:794/242` (handlePM guards/atomic/script L1300, handleImplementer multi-turn + AST + Log*, updateTaskLease L47).
- 5-parallel contract ex (Elastro/LogLoom): matches real usage in sessions + tests/e2e/. Before/after in SKILL/user-guide.
- Reliable-go-systems adherence: todos for phases, rich reasoning on decisions, reconciliation via sweeps, OCC/shadow EnforceOrLog, bounded (caps, MAX_THOUGHTS), explicit errors, observability, no fire-and-forget (every tracked in durable + todo). Applied in revision process (this todo_write, tool verify before assert, bounded reads/greps).
- Alt5 harness-native (scheduler + monitor + file-state + todo orchestration skill, no new core heartbeat): elevated; current fleet/SKILL.md + py implements it (file durable, scheduler for cadences, no goroutines).

All claims double-checked with tools before final v0.2 write. 5 IDs confirmed in live artifacts (grep/read on ~/.grok/sessions/.../subagents/.../meta.json). Aligned with pr-babysit/implement manual patterns + fleet stub already using alt5/harness-native. See revision-v02.md for full 30+ tool call list and "Verification performed (tools only, no assumptions)".

(Full verified quotes and additional harness/Flume claims from the revision process are in the companion `subagent-fleet-orchestrator-revision-v02.md`; this appendix summarizes key ones for self-contained reference. Design line count ~651 after expansion.)

**Live e2e simple request test executed (2026-06-02):** Using orchestrate_claim_spawn (from phase2/3) returned exact spawn_subagent params (description with [tester] prefix, prompt for handoff write, background, worktree isolation, capability execute, general-purpose). Emitted real `spawn_subagent` tool call with those params → sub completed (exit 0, 1 tool call run_terminal for the echo pipeline, wrote /tmp/fleet-e2e-handoff.md with "Fleet orchestrator e2e simple test SUCCESS 2026-06-02T05:10:03Z", worktree_path provided). Then linked back: ensure (workspace fid), enqueue demo del, update_delegation_status (Enforce + Log* via central writer) with the real subagent_id 019e86bd-17b3-77f2-a805-b83e7c04d213 + worktree, append_thought (bridge style, capped, OCC). fleet_status showed linked running + recent_thoughts with bridge + handoff marker. Handoff file verified. Demonstrates full "enqueue → promote sweep → orchestrate → EMIT spawn_subagent (using spec) → post-result update record → status + handoff read + bridge" path. Matches SKILL dispatch code + design sequence. (No rebuild of grok binary needed; ~/.grok/bin/grok is release download; skills + py are live interpreted.)

**Post-phase integration + check-work verifier run (019e86a7-1d09... + prior ee19) results (2026-06):**
- Direct + sub-driven: py_compile multiple OK; CLI smokes (ensure/enqueue --batch w/ depends/handoffs, status --include_thoughts + recent/handoffs, sweep promote/recon/stuck/parent, append_thought bridge) pass; live ~/.grok/fleets/cwd-*/delegation-*.json + meta: 0600, version bumps (OCC), EnforceTransitionOrLog + LogDelegationReasoning in thoughts[] (shadow:True | allowed, promote deps-met, stuck->blocked, etc.), last_sweeps/cadences/snapshot, cap respected.
- E2e sim: batch 2+ (one depends), promote affected 1 + recon, status counts/thoughts, record inspect confirm.
- Reviewer ff19 (v0.2+impls) + this check-work: VERDICT: PASS (all user reqs: pull/review/break phases/run subs/use Flume SKILLs (reliable read+Enforce/recon/OCC/todos/bounded/obs first+everywhere, fleet/SKILL adherence); artifacts updated; harness-native; opt-in in implement/pr-babysit; verif loop; no excess; minor nits polish only (SKILL header, append single-entry, stuck heuristic) non-blocking. Evidence: tool traces, records, compile/smokes, subs 32cd/4a8d/631a/ff19/ee19+ outputs fetched/applied, design v0.2 appendix+PR0+Alt5).
- Fixes applied: append_thought refactored to single entry/call (no dupe Log append); design/appendix extended with this run + smoke excerpts + PASS; todos advanced (merge, phases complete); living spec sync'd.
- Pre-impl checklist progress: PR0 spike (alt5 harness-native) complete via subs+direct; re-verif (this + prior); align skill/23 (yes); e2e skeleton (smoke batch+cycle); coexistence (opt-in if + fallback); blocking OQs noted open (tool=spawn_subagent, TUI=pager stub+spike PR1, durab per-sess+opt-in scheduler, embed=vec0); security/ACP (reuse spawn, staged); metrics (future).
All per reliable-go + Flume port + v0.2. (See also check-work sub output + /tmp/grok-fleet-v02-review.md for full structured.)

Design remains living spec; updated with impl/verif feedback.

---

## Key Decisions

1. **Reuse spawn_subagent as the execution substrate** (not reimplement agent loop inside fleet). Rationale: preserves all existing semantics (worktree, resume, personas via current conventions, depth, TUI, ACP); fleet is pure orchestration layer. Matches Flume's dispatch to handlers but harness handlers are external sub-sessions. (Alternative: embed full runner would duplicate harness agent code and break isolation guarantees.)
2. **File-based durable + atomic OCC for claim/state** (no new DB dep). Rationale: harness already uses FS heavily (sessions, worktrees, memory files with flock, meta.json); simple, no new runtime reqs, portable. Use temp+rename + optional flock for writers; seq in json for optimistic like Flume if_seq/prim. (Tradeoff vs sqlite: sqlite is in harness for search, but adds schema mgmt.)
3. **Shadow mode + Enforce at 100% writers from day 1** (port of PR2). Rationale: safe rollout of central state machine; we saw raw status fights in Flume history. Audit violations via logs before hard enforce.
4. **Specialized throttled sweeps on multiple cadences** (not single poll loop). Rationale: Flume experience showed one-size doesn't fit (promote needs to be fast when backlog, stuck can be slower, recon periodic to avoid cost). Prevents thundering + gives "liveness" without constant work.
5. **Parent visibility via bridged reasoning + structured delegation records + handoff files** (not full child transcript pull-in). Rationale: exactly the context savings goal; mirrors Flume execution_thoughts[] for UI + Logloom. Parent reads summary_file etc. as in persona contracts today.
6. **fleet_enqueue is non-blocking "submit"** (returns id immediately; claim/pool later). Rationale: matches Flume claim in manager cycle, allows batch enqueue without sequential spawn latency, enables backpressure.
7. **Coexistence + fast-path preservation is non-negotiable**. Rationale: interactive ad-hoc must stay simple; fleet for "when you know you need the reliability machinery" (N>2 parallel long-running, resume chains, deps). Alt5 makes this even stronger (see Alternatives). (Revisited post-review per Major 1/4.)

These were chosen after weighing the 4 (now 5) alternatives and Flume post-port lessons (death spirals from races/missing guards, stale denorms, lack of atomicity, insufficient telemetry).

---

## PR Plan (Incremental, Independently Mergeable, Ordered)

**PR0: Runtime spike / prototype (harness-native foundations, per review Major 1 + Alt5)**  
- Title: "harness/fleet: PR0 runtime spike - prototype using *only* existing scheduler/monitor/spawn + file durable records (no new core heartbeat/goroutines; alt5 path)"  
- Affected: `~/.grok/bundled/skills/fleet/SKILL.md` + `scripts/fleet_state.py` (durable records with atomic temp+rename + flock/seq OCC, DelegationStateMachine + EnforceOrLog(shadow default), enqueue/status/sweep/claim helpers + CLI, rich reasoning/thoughts + todo_write phases); basic 23- guide + design refs; no core changes.  
- Deps: none.  
- Desc: Harness-native only (scheduler_create for cadenced recon/wakes + /loop, monitor for streams, file polling for liveness, spawn_subagent reuse for exec, get_/wait_/kill_ for attach, todo_write for phases). Delivers skeleton/records/shadow state/basic surfaces/enqueue hook/claim prepare (as in current fleet/SKILL + py). Addresses "no new core" + alt5. Smoke tests + record verification. Enables PR1+. ~1-2 days. (Current phase1 foundations deliver this; verified in check-work.)

**PR1: Foundations + Observability (skeleton + records + shadow state + basic surfaces)**  
- Title: "harness/fleet: add FleetOrchestrator skeleton (via skill), DelegationStateMachine (shadow), durable record types (file + atomic OCC), and basic fleet_status / fleet_sweep / fleet_enqueue tools (behind flag; harness-native)"  
- Affected: `~/.grok/bundled/skills/fleet/SKILL.md` + `scripts/fleet_state.py` (as delivered); update 16/20-*.md + new section in 23- guide; config parsing stub.  
- Deps: PR0.  
- Desc: Port state machine + record structs + EnforceOrLog (shadow default). Implement basic surfaces (reads/writes durable; enrich with live get_ for compaction safety). No full claimer/pool yet. Tests for transitions + record roundtrip + smokes. TUI stub (basic grouping note). Enables future PRs + early debugging. (Delivered in phase1; verified PASS.)

**PR2: Enqueue + Basic Spawn Path (fast-path wrapper + tracking + basic claim hook)**  
- Title: "harness/fleet: implement fleet_enqueue (writes records, wakes via scheduler, returns id immediately) + basic immediate spawn hook (claim_and_prepare_spawn + emit spawn_subagent; no full claimer/pool yet) + fleet_wait basic + update pr-babysit/implement examples"  
- Affected: fleet/SKILL + py (enqueue + claim hook + spawn emission example); update skills (implement parallel reviewers, pr-babysit groups); docs example + 23- migration; integration test for 1-delegation.  
- Deps: PR1.  
- Desc: `fleet_enqueue` creates record (enqueued), schedules wake. Claim hook prepares spawn_params (under cap). Emit real `spawn_subagent` (background, with params + [tag] desc). Update record with returned subagent_id + worktree on success. `fleet_wait` polls existing get_ + records. Manual tracking eliminated for simple case. TUI shows basic. Value: centralized tracking + status for small fleets. (Phase2/3 subagent in progress.)

**PR3: Claimer + SubagentPool + Backpressure**  
- Title: "harness/fleet: add DelegationClaimer (atomic claim with OCC on records, dedup, WIP gate, Flume-like guards) + SubagentPool (semaphore + Submit/Sync + graceful + spawn emission) + integrate in cycle/sweeps"  
- Affected: py claimer/pool logic + SKILL dispatch; tests for races/OCC (file seq + retry); metrics stubs.  
- Deps: PR2.  
- Desc: Enqueue now truly queues; cycle/claim under cap with backpressure. Dedup (norm + opt semantic), WIP, depth guards. Matches Flume claimer/pool. Allows safe N=8+ bursts. (In phase2/3 subagent.)

**PR4: FleetSweeper + Recovery + Promote/Resume**  
- Title: "harness/fleet: implement FleetSweeper (RunThrottled + lastRun + 6+ specialized: stuck requeue, promote for handoffs/deps, resume blocked, block, parent completion 2-pass, child recon) + integrate + fleet_sweep tool + e2e test task"  
- Affected: py sweeper + SKILL sweep section + scheduler cadences; cycle calls; state updates via Enforce; tests with injected stuck records; 23- troubleshooting; e2e skeleton for 5-batch (assert no manual IDs + durable recovery post-sim compaction).  
- Deps: PR3.  
- Desc: Automatic recovery (addresses compaction "still running" and stuck cases). Adaptive promote on handoffs. `fleet_sweep` for ops. Reconciliation for lineage/denorms. Big reliability win. Include explicit 5-delegation e2e test task. (Phase4 subagent in progress.)

**PR5: Telemetry Bridge + TUI Enhancements + Full State Machine Hardening**  
- Title: "harness/fleet: wire LogDelegationReasoning + thoughts append (best-effort like logger bridge + monitor/get_ deltas for child reasoning) + enhance Ctrl+T (fleet tree, reasoning drawer, sweep action) + Ctrl+; + flip shadow default after audit + fleet_status include_thoughts + TUI ownership spike"  
- Affected: py/SKILL bridge (extend thoughts + monitor); TUI components (pager integration notes + stub); more Enforce call sites; e2e with reasoning visibility; 23- observ section.  
- Deps: PR4.  
- Desc: Observability parity with Flume (child thoughts surface via get_/monitor + append). Makes fleet usable for debugging long runs. After PR, consider default-on. Investigate pager vs core TUI ownership + minimal change spike (per review). (Phase5 subagent in progress.)

**PR6: Polish, Docs, Adoption in Bundled Skills, Rollout Hardening**  
- Title: "harness/fleet: full docs (23-fleet-orchestrator.md complete), example in implement/pr-babysit skills (opt-in for parallel + state with delegation_ids), config surface (caps, timeouts, shadow), error UX, performance (capped thoughts), e2e for 5-delegation batch, flag default flip + migration notes + full verification"  
- Affected: 23- guide (already advanced), 2-3 skills (parallel sections use fleet_enqueue + wait + fleet_status), tests, README, config schema; check-work/reviewer on all.  
- Deps: PR5.  
- Desc: Makes the feature discoverable and adopted. Delivers the "why we did this" story. Cleanup (e.g. deprecate some manual patterns in skills comments). Full rollout after PR0-5 + blocking OQs resolved. (Guide + adoption in progress.)

Total: 6 PRs (+PR0 spike), each <= ~1-3 days for experienced contributor, independently testable with flag, cumulative value (P0: native prototype; P1: status + records; P2: enqueue + spawn hook; P3: safe parallel + backpressure; P4: self-healing + e2e; P5: visible + TUI; P6: productized + adopted).

Per-PRs risks if delayed: e.g., PR0 critical for "no new core" buy-in; PR4 for the motivating recovery/stuck cases; PR5 for observability (review Issue 8); etc. (See review for full.)

---

*End of design document. This is implementable after resolving blocking open questions and completing the PR0 runtime spike; all citations verified via tool exploration of live files (no hallucinated paths). See Verification Log / Appendix and docs/designs/subagent-fleet-orchestrator-revision-v02.md for per-issue status + process.*

---

*End of design document. This is implementable after resolving blocking open questions and completing the PR0 runtime spike; all citations verified via tool exploration of live files (no hallucinated paths). See Verification Log / Appendix and docs/designs/subagent-fleet-orchestrator-revision-v02.md for per-issue status + process.*

## Revision Summary (v0.2 addressing 2026-06 review)

(See separate `subagent-fleet-orchestrator-revision-v02.md` for full per-issue status, tool calls, and process. All 2 major + 3 minor + 4 nits addressed via direct edits + verification (e.g., runtime mapping + PR0 spike for Major 1; full appendix + 5-ID re-audit + tool standardization for Major 2; TUI/PR/e2e/Alt5 elevation/Blocking OQs/checklist for minors). Line count of design file now 651 (expanded with new/expanded sections, appendix, PR0 details, and rephrasing while preserving strong v0.1 content like mapping table, citations, API specs, data model, Key Decisions). Updated fleet/SKILL.md (322 lines) + 23- guide (348 lines) + py helper (635 lines) already align with v0.2 + review fixes + harness-native + reliable-go-systems SKILL (as verified PASS in check-work subagent 019e869c-ee19-71b0-974b-358c1f765cdb, with only minor doc nits addressed here). Phase1 foundations delivered (verified via py_compile + full smokes + record inspection for OCC/Enforce/0600/reasoning). Other phases (2-5 + adoption + verification) in progress via subagents using this v0.2.

(End.)