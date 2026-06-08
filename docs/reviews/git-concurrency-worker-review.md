# Git Concurrency and Clone/Checkout Failure Review for Flume Workers

**Date:** 2026-06-03  
**Reviewer:** Senior Staff Engineer (Grok subagent, Go concurrency + K8s-style queues + git workflows specialist)  
**Scope:** Full review of the user-provided plan ("Fix Git Concurrency Loop and Directory Cleanup") against the exact reported errors from agent reasoning + docker logs. Process followed the mandated thorough steps (8+ tracked todos): all critical file reads (pool.go full; runner.go full relevant incl. EnsureTaskBranch:1360+, gitCheckoutBranch:1742+, RunWorker:138+, handle*:272/560/730, clearStaleClaim:1598+, gitCmd*/classify:1682+, repoPath dups at 576/750/1390; manager.go full incl. cycle:175+, Sync:242, processWorker:254+; claim.go full incl. TryAtomicClaim:53+, isWIPSaturated:249+, checkGitOverlap:332+, isRepoLocked:403, loadRepoWIPLimits:283+), broad+targeted greps (dupe resolution, git calls, Ensure etc across *.go + whole tree), log investigations (docker compose logs worker/dashboard/gateway with live capture of exact errors; grep monitoring.log elastro.log *.log for rflow/clone/Ensure/"chdir /app"/"refs/files-backend"/"stale claim"/"Failed to set up"; ls workspace/, container exec ls/.git/HEAD/refs inside flume-worker-1 for rflow state; planner-debug), concurrency model deep dive, root cause, plan assessment, other issues, verdict + augmented plan + concrete edits. Evidence-based only; no rubber-stamp.

All absolute paths use the workspace root `/Users/jonathandoughty/clients/fremenlabs/flume /flume/`. Code snippets use LINE_NUMBER→ prefix for precision (from read_file).

## 1. Summary of Investigation (Reads, Greps, Logs, Model)

**Files read (full or targeted sections):**
- `internal/worker/pool.go` (full, 130 LOC): Pool struct (no running map), NewPool:30 (sem=16), Submit:42 (always launches go after sem/wg if !shutdown; no name dedup), SyncWorkerProcesses:75 (for every ws in state with Status=="claimed" && taskID: calls Submit(name, RunWorker)), ActiveCount etc.
- `internal/worker/runner.go` (key sections + cross reads): RunWorker:138 (fetch task, handle* by role, on err:185 LogAgentReasoning "Worker ... failed: ... Stale claim will be cleared...", 227 clearStaleClaim), handleImplementer:249 (Ensure at 272; on err:274 "Failed to set up task branch: %s"), handleReviewer:560 (repoPath dupe at 576-596), handleTester:730 (repoPath dupe 750-758; gitCheckout at 767), EnsureTaskBranch:1360 (project load, workspace fallback native/cwd vs /app, repoPath=LocalPath or "flume-reg-%s", stat .git:1398 `if _, err := os.Stat(filepath.Join(repoPath, ".git")); os.IsNotExist(err) { ... MkdirAll parent; git clone 2min timeout; on err 1457 `_ = os.RemoveAll(repoPath)`; return clone err; } ... ALWAYS gitCheckoutBranch:1467; on checkout err:1469 classify+ LogAgentReasoning "Failed to set up task branch %s (classified: %s)" at 1477), AutoCommit:1494 (more gitCmd), gitCmd/gitCmdWithTimeout:1682/1689 (cmd.Dir=repoPath; chdir errs surface here), classifyGitError:1705 (cases for auth/lock/conflict/not_repo/branch_not_found/network/missing_ref/unborn/128; default "unknown"; NO chdir/no-such/not-git), gitCheckoutBranch:1742 (cleanStaleLocks, fetch, checkout recovery -B/reset/clean, final err msg at 1800 exactly matches report: "git checkout branch %s failed after recovery (classified: %s/%s). From %s: ... (chdir ... no such file or directory, class=%s). Last resort: ... Consider: the clone may be in a bad state — delete the repo dir..."), cleanStaleLocks:1814 (ONLY index.lock >30s), resolve* etc.
- `internal/worker/manager.go` (full): New:84 (NewPool), Run:136 (immediate cycle + ticker pollSecs), cycle:175 (sweeps, busy=fetchBusyWorkers queue_state=active, workers=Build, process per, saveState, 242 `if !isPaused { m.pool.SyncWorkerProcesses(ctx, state) }`), processWorker:254 (269 `if bw, busy := busyWorkers[worker.Name]; busy { snapshot claimed with bw.TaskID }` — NO ExecutionHost affinity check; 325 claim only for idle), fetchBusyWorkers:395 (ES search queue_state=active → map by ActiveWorker), poll default via config.
- `internal/worker/claim.go` (full): TryAtomicClaim:53 (WIP at 138, checkGitOverlap:145 before OCC), isWIPSaturated:249 (load, count "status":"running" for repo; if limits.MaxConcurrent <=0 {return false}), loadRepoWIPLimits:283 (Get "flume-projects", unmarshal .wip; default {} →0), checkGitOverlap:332 (LocalPath, branchName, isRepoLocked:351, getModifiedFiles which does git rev-parse/diff -C repoPath), isRepoLocked:403 (stat .git/index.lock + refs/heads/BRANCH.lock only), getModifiedFiles:417 (git -C may noop on missing/bad repo).
- `internal/worker/workers.go` (full): BuildWorkers:31 `name := role; if workersPerRole > 1 { name = role + "-" + nodeID + ... }` (default 1 → plain "implementer"), Apply overrides.
- `internal/worker/handlers.go` (full): stubs only (real logic in runner.handle*); no git.
- `internal/worker/sweeps.go` (targeted): promotePlannedTasks feeds ready tasks → claims; requeues etc.
- `internal/dashboard/api_projects.go` (targeted 205+): cloneAndSetupProject:227 `destPath = .../flume-reg-%s` (uses FLUME_WORKSPACE or ./workspace; always RemoveAll:234 before its clone; sets LocalPath), inconsistent with runner.
- `pkg/types/types.go`: Project:196 LocalPath (no WIP embedded in Go struct, but claim unmarshals "wip" dynamically).
- `internal/config/config.go`:109 default WorkerManagerPollSeconds:2, WorkersPerRole:0 (→1).
- docker-compose.yml (targeted): worker deploy replicas: ${FLUME_WORKER_COUNT:-2}, no volumes for /app/workspace (private FS per replica), command worker-manager.

**Greps (dupe + git + calls):**
- EnsureTaskBranch called only in runner:272 (impl); gitCheckoutBranch at 767 (tester), 1467 (ensure), 1742 def.
- repoPath / flume-reg- resolution duplicated: runner:576 (reviewer, with WORKSPACE_ROOT + native), 750 (tester, LocalPath only no full fallback), 1390 (ensure), claim:347 (LocalPath only, early return false on ""), dashboard:227 (FLUME_WORKSPACE), destroy.go:115.
- No other Ensure/git clone sites in *.go.
- No "running" map, no per-repo mutex, no host filter on Sync.
- "stale claim", "Failed to set up task branch", "failed after recovery" only in runner.go:226/275/1635/1800 (exact strings match user reports).

**Log + runtime investigations (as mandated; used run_terminal + grep + docker + container exec):**
- `docker compose logs --tail=200 worker` + piped grep for errors (live capture during run): exact matches for reported patterns on "rflow" (flume-reg-rflow):
  - "Failed to set up task branch: checkout branch feature/Update-documentation-with-CLI-comma-c83ca1b9 (classified unknown): git checkout branch ... failed after recovery (classified: unknown/unknown). From origin/main:  (chdir /app/workspace/flume-reg-rflow: no such file or directory, class=unknown). Last resort: ... Consider: the clone may be in a bad state — delete the repo dir..."
  - "Worker implementer failed: ... Stale claim will be cleared..."
  - "agent reasoning" + "pool-worker: failed" + clearStale (to ready) + later "EnsureTaskBranch: git checkout failed" (classified unknown) + "cloned successfully" in same window.
  - Multiple "EnsureTaskBranch: resolved workspace (project.LocalPath was empty)" + "cloning repository dynamically" at ~2s intervals (e.g., 21:40:44/46/48/50 on worker-1/2) for same rflow; one clone at 44s, success 53s.
  - "pool-worker: executing" "implementer" repeatedly (e.g., 21:40:33 complete → 34 executing; many across tasks).
  - Also WIP backpressure, 409 version conflicts, TaskStateMachine violations (planned→done, ready→done), resume sweeps recovering blocked.
- `docker compose logs ... dashboard gateway`: routing/LLM context, no direct git.
- `find . -name "*.log" ... | xargs grep ...`: only monitoring.log hit (old rflow planner); elastro.log etc. had none of the git strings (errors in docker stdout + ES reasoning).
- `ls workspace/`: old planner-debug (Mar dates, no current clones on host); no flume-reg-* on host FS.
- Container exec on flume-worker-1 (replica): `ls /app/workspace/flume-reg-rflow` (exists, healthy .git/HEAD/refs/heads/ in current state post-run); confirms /app path used inside containers.
- `cat workspace/planner-debug.log | tail`: old, irrelevant.
- ES curl attempt (localhost:9200, elastic/creds from env/compose): not reachable/auth fail in this env (best-effort; docker logs + code strings sufficient as they exactly reproduce user-reported "from agent reasoning logs").
- Docker state: replicas=2 (worker-1/2), both logging Ensures/clones for rflow; private FS (no bind volumes for workspace in compose); confirms name collision + intra-goroutine races.

**Concurrency model (evidence-based):**
- Cycle freq: config:109 default 2s; manager:144 `ticker := time.NewTicker(time.Duration(m.pollSecs)*time.Second)`; 148 immediate cycle; 156/168 on tick/wake → cycle (incl. 242 Sync if !paused).
- Sync vs RunWorker: Sync:85 `_ = p.Submit(...)` (fire-forget) for *every* claimed snapshot (from busy queue_state=active name match, even remote host); RunWorker:138 holds goroutine for full task (Ensure clone 120s+ LLM+commit; can >> poll). No dedup → thundering Submit during long op.
- Claimer gates (pre-claim): isWIPSaturated:249 (only "running" count per repo; `if <=0 { no limit }` from load:286 default {}); checkGitOverlap:145 (skipped if no LocalPath or branch !exist in getModified:420); isRepoLocked:403 (file .lock stats only, claim-time snapshot).
- Pool: global sem 16 (pool:33); no per-name/per-repo; Submit always go-routine + active++ (no running map).
- busyWorkers: manager:398 search queue_state=active (global); process:270 name-match only (no host filter, even if bw.ExecutionHost set at claim: manager:303 / atomicClaim in claim:302).
- Worker names: workers:31 plain "implementer" (perrole=0/1); only node-qualified if >1. Replicas=2 in compose → name collision across managers.
- Result: for a "running" implementer task on rflow, *every* 2s *every* replica's cycle Sync submits its local "implementer" → multiple in-flight RunWorker goroutines (intra + cross) calling Ensure concurrently on same logical repoPath (private per-container but still intra-process race possible).
- WIP/locks: defaults unlimited; claimer git checks racy/inconsistent on partial clones (git -C on non-dir fails silently in overlap).
- Native/container: runner Ensure/reviewer/tester have FLUME_NATIVE_MODE cwd vs /app + WORKSPACE_ROOT; dashboard uses FLUME_WORKSPACE; claim skips; LocalPath often "" (logs) → fallback inconsistency can cause chdir "no such".

**Root cause analysis of exact errors (refs/files-backend abort + no-such on checkout + stale reset loop):**
1. **Concurrent clone abort ("BUG: refs/files-backend.c:3175: initial ref transaction called with existing refs : signal: aborted" + "Cloning into '/app/workspace/flume-reg-rflow'")**: Direct from git when 2+ `git clone` race to same target dir (files-backend init sees partial refs from sibling). Trigger: repeated Submit (pool:85) during 120s clone (Ensure:1451 timeout) because no name check in Submit/Sync; 2s cycle (manager:144) + busy name match (manager:270) launches N goroutines in RunWorker → handleImplementer:272 → Ensure:1398 stat (TOCTOU, both see !.git) → both clone (1455). Evidence: docker logs (multiple "cloning" + "resolved" at 2s intervals for rflow; one aborts while other proceeds; "pool-worker: executing" repeats).
2. **"chdir /app/workspace/flume-reg-rflow: no such file or directory" in checkout recovery**: From gitCmdWithTimeout:1694 `cmd.Dir = repoPath`; exec fails if dir gone at that instant. Surfaces in gitCheckoutBranch:1800 error msg (exact match in report + captured logs: "git checkout branch feature/... failed after recovery (classified: unknown/unknown). From origin/main: (chdir..., class=unknown). Last resort..."). How dir vanishes: concurrent goroutine in failing Ensure:1457 `os.RemoveAll` (on its clone abort) nukes the tree while sibling's Ensure (stat saw .git just before rm) proceeds to 1467 gitCheckout (or recovery inside it does git -C). TOCTOU on stat:1398 vs later use + shared dir mutation w/o exclusion. Also possible if rm from prior failure + re-Ensure sees no .git but recovery paths assume present. Note: checkout called *even on some bad paths*; rm only on clone-err, not checkout-fail.
3. **Stale claim reset loop + "Worker implementer failed: checkout branch ... (classified unknown)"**: RunWorker:177 on err from Ensure → 185 Log "Worker ... failed: %s. Stale claim...", 227 `r.clearStaleClaim` (1598: sets ready/available via updateTaskLeaseState, emits more reasoning 1635 "stale claim cleared..."). Task back to claimable → cycle claims (or promote sweep) → re-RunWorker → re-fail (bad dir state not auto-healed). classify:1738 defaults "unknown" (no case for "chdir"|"no such file or directory"|"not a git" in 1720) → "classified unknown". Suggestion in 1802 error ("delete the repo dir...") is advisory only; no code auto-rm+retry on bad_state from Ensure/checkout err path (unlike clone-err rm). Loop exacerbated by 2-replica name collision (both Sync for same active_worker="implementer").
4. **Contributing design**: Unlimited WIP (claim:256 default 0; no "wip" on rflow proj likely); claim git checks skipped on empty LocalPath (claim:343); no per-repo serialization (only global sem + file locks at claim time); private FS per replica but intra-goroutine races suffice for corruption; duplicated resolution (fragile, env var mismatch WORKSPACE vs _ROOT) leads to "no such" when paths stale.

Logs + code paths *exactly* produce the user-reported strings (runner:275 for "Failed to set up...", 1800+186 for the worker failed + chdir in recovery, clearStale at 1635/1644).

## 2. Does the Plan Correctly Target "Concurrent Worker Execution" and "Directory Cleanup"?

**Partial yes** (good first step; directly attacks two symptoms).

- **running map + Submit changes (pool.go)**: Correctly identifies the re-submission storm. Sync:75 blasts Submit for every claimed (incl. mid-RunWorker long clone) because process:270 marks busy snapshots "claimed" by name. Adding `running map[string]bool + mu` + check "if already running, skip", register on start, unregister on complete (in defer of go) will prevent *intra-pool* duplicate goroutines for same worker name. With default 1 implementer name, this serializes implementer execution (prevents 60+ overlapping RunWorkers during 2min clone). Matches "concurrent worker execution conflicts". (Timing critical: check+set *before* sem/go launch, else window allows dups; release on all paths.)
- **Enhanced .git check + proactive rm in EnsureTaskBranch (runner.go)**: Correctly targets broken/partial clones. Current:1398 only `os.IsNotExist(err)` on .git (file or broken dir passes as "exists" → skip clone → checkout on corrupt → unknown class + chdir if rm'd). Plan's "verify ... exists and is a directory" + "prior to git clone, proactively clean up (delete) repoPath if exists but missing valid .git" ensures fresh dest (like dashboard:234 always rm). On next claim after abort/rm, will see invalid/missing → rm (idempotent) → clone clean. Addresses "the clone may be in a bad state" (1802 suggestion) proactively. Helps the "no such" + abort loop by reducing persistent bad dirs.

Verification plan (test + manual rflow tasks) is sound baseline.

The plan is backend-only (no UI), stabilizes clone/checkout under contention — matches user note.

## 3. Gaps / Incorrect Assumptions in the Plan

- **Assumes "by worker name" fully solves concurrent execution**: Partially (intra-pool for default 1 role). Incorrect for multi-replica (compose: replicas=2 default): workers.go:31 keeps name="implementer" (nodeID only if perrole>1); manager:270 busy name-match + 242 Sync has *no* ExecutionHost filter (despite claim setting host:303). Both replicas Sync their local "implementer" for any task with active_worker="implementer" → cross-container concurrent RunWorker/Ensure (even on private FS: duplicate work, double LLM, potential double-push conflicts on remote). running map (per-Pool) doesn't dedup across managers.
- **Still racy for clone critical section even with map**: The if-stat+rm+clone+checkout (Ensure:1398+) has no lock (in-mem or flock). If perrole>1 (multiple implementer names) or reviewer/tester overlap (tester:767 calls gitCheckout; reviewer:607 git diff), or external (dashboard clone), or timing with sweeps/promote, concurrent git on shared clone dir possible. TOCTOU remains for stat vs rm vs exec. Map helps only default case.
- **No self-healing / auto rm on recovery failure**: Plan's proactive is *only before clone attempt*. On checkout fail (the chdir/unknown case), Ensure:1467 just classifies/logs/returns err (no rm); gitCheckout:1802 suggests "delete... let it re-clone" but code doesn't. clearStale:1598 just resets task (no workspace cleanup). Next claim hits same bad state → loop continues. (Captured logs show repeated fails + "cloned successfully" only after manual timing.)
- **Doesn't address default unlimited concurrency or claim-time fragility**: claim:256 `if <=0 {return false}` (WIP=0 from load:286 on projects lacking "wip" key); isWIPSaturated only "running" count (not all claimed); git overlap/locks (claim:351/403) skipped on empty LocalPath (common per logs) and ineffective for new branches (getModified:420 returns nil). Enables multiple running on rflow → more Ensure pressure.
- **Doesn't fix cross-cutting dups/dupe code/paths**: No change to name qualification, host affinity in Sync/process, or central ResolveRepoPath. 3x dupe in runner + claim/dashboard (different env vars, native handling) remain → future "no such" and inconsistency.
- **Assumes cleanup + dedup sufficient for "fully address root causes"**: No; underlying is lack of exclusive ownership for workspace setup (K8s worker queue style would use lease + owner + worktree or per-job dir; here shared clone + no worktrees).
- **Registration timing / edge cases unaddressed in plan spec**: If register inside go (after Submit check), window for dups from rapid cycles. What on panic? Skip returns what (nil?)? Sem held? Shutdown interaction?
- **No observability/test/verif for the new paths**: Plan's verif is manual + go test (current tests pass but zero coverage for Ensure/git bad-state/concurrent; see worker_test:16 only Build/branch name/claim title).

Plan is "correct approach" directionally but incomplete for full root cause closure (will reduce but not eliminate the loop under load/multi-replica/partial states).

## 4. Other Issues That Must Be Addressed (Code, Logs, Design; with file:line)

- **Name collision + missing host affinity (core enabler of dups)**: workers.go:31 (name=role when WorkersPerRole<=1); manager.go:270 (`if bw, busy := busyWorkers[worker.Name]` — no `if bw.ExecutionHost != "" && bw.ExecutionHost != local { continue }`); 242 Sync; processWorker:292 (host calc only for load, not dispatch). Replicas=2 (docker-compose.yml worker deploy). Leads to both replicas executing same task (logs: worker-1 + worker-2 Ensures on rflow close together).
- **Duplicated fragile repoPath resolution (inconsistent, source of "no such")**: runner.go:576 (reviewer: if LocalPath=="" { WORKSPACE_ROOT or native cwd or /app + flume-reg }), 750 (tester: LocalPath only, else skip), 1388 (Ensure: same), 1390; claim.go:347 (LocalPath only → early false, no lock/overlap); dashboard/api_projects.go:217 (`FLUME_WORKSPACE` or ./workspace — mismatch _ROOT), 227; cmd/flume/commands/destroy.go:115. Native:1380/588/ only in runner. LocalPath often empty (Ensure logs: "resolved workspace (project.LocalPath was empty)").
- **No auto-recovery on bad state + weak classify**: runner.go:1457 (rm *only* on clone err); 1467 (checkout after, no rm on its err); 1802 (advisory text only); 1705 (classify: no "chdir"|"no such file or directory"|"not a git repository" (beyond 1720) → "unknown" always, as in captured "classified unknown"); 1742 (gitCheckout can be called on missing dir post-rm). clearStale:1598 (no workspace action). Result: persistent bad dir feeds loop (user suggestion in error never auto-applied).
- **WIP/locks too weak + racy**: claim.go:256 ( <=0 unlimited; default from load:286/292); 264 (only status=running count); 361 (overlap returns early nil if no branch yet); 403 (only 2 .lock files; no .git dir check); 419 (git -C on partial → err, treated no files); called pre-claim but not re-checked in Ensure. No per-repo in-mem/flock for setup.
- **cleanStaleLocks too narrow**: runner.go:1814 (only index.lock >30s; gitCheckout:1745 calls it, but abort "refs/..." suggests other backend state); no full recovery on "bad state".
- **No serialization around git setup critical section**: Ensure:1398 (if + clone + checkout) + tester/reviewer git calls; no mutex (even package-level per-repoPath). Pool sem global only (16).
- **State machine / lease races visible in logs (compounding)**: runner:233 (Enforce on post), 312 (in claim too), captured: 409 conflicts on update, invalid transitions (planned→done, ready→done), clearStale firing repeatedly. (Related to prior reviews but surfaces in git fail paths via 227.)
- **Missing coverage + no tests for these paths**: internal/worker/worker_test.go:16 (only BuildWorkers, resolveBranch, normalize, registry, tools; 0 for Ensure:1360, gitCheckout:1742, classify:1705, clearStale:1598, concurrent Submit, bad .git recovery, rm paths). sweeps_test.go exists but not for this. No fuzz for partial clones.
- **Path/env fragility + native/container**: As above + in Ensure clone:1455 (no --separate-git-dir or worktree); dashboard clone private to its container.
- **Observability good but spammy on loop**: LogAgentReasoning everywhere (e.g. 275,1477,185) — excellent for diagnosis (we used it); but repeated on fail/reset pollutes reasoning for rflow tasks. No rate limit on "bad workspace" class.
- **Other git ops assume happy path**: AutoCommit:1496 (status on potentially bad post-checkout); claim getModified:430 (diffs); no worktrees (shared clone + concurrent fetch/checkout -B from impl + test/review).
- **Docker/runtime**: replicas=2 + no workspace volume (private /app/workspace) + poll=2s + default WIP=0 + plain names = perfect storm for the observed (even if FS private, intra-goroutine rm races suffice).

All evidenced in reads/greps/logs above; many pre-existing from Python→Go port (per comments in code).

## 5. Recommended Approach (Augmented Plan)

**Verdict: The plan is a solid, correct, minimal first step that will mitigate the reported clone/checkout failures and loops under the observed contention (esp. re-submits + partial .git). It targets the right symptoms with low-risk backend changes. However, it does not *fully* address the root causes (races remain possible cross-replica / multi-role / external; no self-heal on the exact "chdir unknown" path that produces the 2nd error; defaults and naming amplify; dupe code unaddressed). The "stale claim reset loop" will be tamed but recur without broader fixes. Do the plan + the augmentations below for complete, reliable K8s-style worker queue behavior (exclusive setup, self-healing, affinity, serialization). Prioritize: dedup + classify + auto-heal + WIP default + host affinity (quick wins); then central path helper + per-repo guard + tests.**

**Augmented plan (plan + these):**
- Implement the 2 proposed changes (pool running map; Ensure stat+proactive rm). Make registration atomic + pre-launch; unregister in defer on all paths (incl. early returns).
- **Add host affinity + unique naming** (prevents cross-replica dups): always include node in worker name (workers.go); filter in processWorker/Sync (manager.go) to only "my" host's tasks.
- **Self-healing + better classify** (directly kills the loop for "no such"/bad state): extend classify for chdir/no-such/bad repo → "bad_workspace"; in Ensure (post-checkout err) and gitCheckout err paths, if bad class: RemoveAll + Log + *retry the clone/checkout once* (with timeout); only fail after.
- **Enforce sane default concurrency**: in loadRepoWIPLimits or isWIPSaturated: if no explicit or 0, default MaxConcurrent=1 (or 2 for review/test). Document. Set on project create in dashboard.
- **Serialize Ensure critical section**: add package-level `var repoSetupGuards sync.Map` (repoPath → *sync.Mutex); in Ensure: get/lock for the path around stat+rm+clone+checkout (unlock defer). (Covers multi-name + external races; lightweight.)
- **Centralize path resolution** (kill dupe + inconsistency): new helper e.g. `ResolveRepoPath(p ftypes.Project) string` (in runner or new workspace.go; handle LocalPath, FLUME_WORKSPACE_ROOT (prefer) or WORKSPACE, native cwd, /app fallback, sanitize). Update *all* sites (runner 3x, claim:347 (add fallback so locks work), dashboard, destroy). Make dashboard use same.
- **Strengthen locks/clean + git robustness**: expand cleanStaleLocks to other .locks (refs, packed-refs); in claim isRepoLocked + getModified, add "is valid git?" check (or tolerate); consider `git clone --no-checkout` + worktree per-task for true concurrent (future, K8s job style).
- **Tests + verif**: Add to worker_test.go: TestEnsureTaskBranch_BadStateRecovery (tempdir with partial .git or missing, expect rm+clone success or retry); TestGitCheckoutBranch_NoDir (chdir case); TestClassifyGitError_ChdirUnknown; concurrent test (goroutines calling Ensure on same temp repoPath, assert no abort/1 winner). Run under go test -race. Enhance manual: trigger 10+ concurrent rflow tasks (set perrole=2, replicas=2, WIP=0 explicitly); assert no "unknown"/chdir/abort in reasoning + all succeed or cleanly block. Add to docker health or e2e.
- **Observability**: On "bad_workspace" class, emit extra reasoning + metric (if any); consider backoff before re-claim on repeated workspace fails.
- **Out of scope but note**: Full worktree support or per-task clone dirs for high concurrency; flock file for cross-process (dashboard+workers); ES-side exclusive lease for workspace.

This makes it robust (self-healing, exclusive, affinity, defaults safe, no dupe code).

## 6. Concrete Suggested Edits (Beyond Plan; or "plan + these")

**1. pool.go (augment the plan's running map; ensure safe timing):**
```go
// In Pool struct (after active):
running   map[string]bool
runningMu sync.Mutex

// NewPool:
p := &Pool{ ..., running: make(map[string]bool) }

// Submit (replace body after shutdown check; before sem):
p.runningMu.Lock()
if p.running[name] {
    p.runningMu.Unlock()
    p.logger.Debug("pool-worker: skip duplicate submit (already running)", slog.String("worker", name))
    return nil // or ErrAlreadyRunning if callers care
}
p.running[name] = true
p.runningMu.Unlock()

if err := p.sem.Acquire... 

p.wg.Add(1)
p.active.Add(1)

go func() {
    defer func() {
        p.runningMu.Lock()
        delete(p.running, name)
        p.runningMu.Unlock()
    }()
    defer p.wg.Done()
    defer p.sem.Release(1)
    defer p.active.Add(-1)
    ...
}()
```

**2. runner.go (plan's Ensure enhancement + auto-heal + classify):**
```go
// classifyGitError (add cases):
case strings.Contains(combined, "chdir") || strings.Contains(combined, "no such file or directory") ||
     strings.Contains(combined, "not a git repository") || strings.Contains(combined, "does not exist"):
    return "bad_workspace"

// EnsureTaskBranch (around 1397; enhanced per plan + more):
gitDir := filepath.Join(repoPath, ".git")
info, statErr := os.Stat(gitDir)
hasValidGit := statErr == nil && info.IsDir()
if !hasValidGit {
    if repoPath != "" {
        if fi, _ := os.Stat(repoPath); fi != nil {
            r.logger.Warn("EnsureTaskBranch: cleaning bad workspace state (no valid .git)", slog.String("repo_path", repoPath))
            _ = os.RemoveAll(repoPath)
        }
    }
    // ... then existing if project.RepoURL... MkdirAll(Dir(repoPath)) ...
    // clone...
    // on clone err: rm (keep) + return
}
// after possible clone:
branch := ...
if err := gitCheckoutBranch(...); err != nil {
    class := classify...
    if class == "bad_workspace" || class == "unknown" {
        r.logger.Warn("EnsureTaskBranch: bad workspace after checkout; auto-clean + retry once", ...)
        _ = os.RemoveAll(repoPath)
        // re-clone if needed (or just retry checkout after fresh?); for simplicity re-call Ensure or inline retry clone logic
        // (elide full for brevity; emit reasoning)
        flumelogger.LogAgentReasoning(..., "auto-recovered bad workspace by rm + re-clone")
    }
    ...
}
```

Similar retry logic in gitCheckoutBranch on final err3 if bad class (before return).

**3. manager.go + workers.go (host affinity + names):**
- workers.go:31: always `name = fmt.Sprintf("%s-%s-%d", role, nodeID, i)` (or keep if==1 but qualify); update tests.
- manager.go:269 in processWorker: after `if bw, busy := ...; busy { if bw.ExecutionHost != "" && bw.ExecutionHost != worker.ExecutionHost { return snapshot } ... set claimed }`
- Similarly guard Sync or filter state.Workers before Sync.

**4. claim.go (default WIP):**
```go
func (c *Claimer) load... {
    ...
    lim := limits.WIP
    if lim.MaxConcurrent <= 0 {
        lim.MaxConcurrent = 1 // safe default for shared clone (prevent herd on git setup)
    }
    return lim
}
```
(Or in isWIPSaturated.)

**5. New helper + callers (centralize; one place for native/ROOT etc):**
Add `func ResolveRepoPath(proj ftypes.Project) string { ... }` (copy+unify logic from runner:1376 and dashboard:217; prefer consistent "FLUME_WORKSPACE_ROOT").
Replace all 5+ sites. Update claim to always resolve so isRepoLocked etc work.

**6. Tests (worker_test.go add):**
```go
func TestEnsureTaskBranch_BadStateRecovery(t *testing.T) { /* temp repo with .git file not dir, or missing; mock or real git; assert cleanup + success */ }
func TestClassifyGitError_BadWorkspace(t *testing.T) { /* chdir err → "bad_workspace" */ }
```
(For full concurrent: use errgroup + same path, assert <=1 clone attempted or no abort.)

**7. docker-compose / config (optional quick):** Document or default WORKER_MANAGER_POLL_SECONDS=5 or higher for less blast; set FLUME_WORKER_COUNT=1 for smoke; add workspace volume if want shared (but then need locks).

These are minimal, targeted, evidence-based diffs. Can be done in one PR after plan.

## 7. Verification Enhancements

- Automated: `go test ./internal/worker/... -race -count=10` (after adding tests); `go test -run TestEnsure` etc.
- Manual (as plan): rebuild (`docker compose build worker`), up, trigger sequential + burst concurrent tasks on rflow (e.g. via UI or POST /tasks or sweeps "promote:rflow" x N). Monitor: `docker compose logs -f worker | grep -E 'rflow|Ensure|clone|checkout|stale|bad_workspace|chdir'`. Check agent reasoning in dashboard for task (no "unknown" or "Failed to set up" loops; "Branch ready" + success). `docker exec ... ls /app/workspace/flume-reg-rflow/.git` (valid). Assert no git abort in logs. Scale replicas=2, perrole=2, explicit WIP=0 on proj → still clean (with affinity).
- Extra: e2e script or tests/integration that does 5 parallel impl on same repo; assert 1 "wins" setup or serialized cleanly. Check for regression on non-code tasks (no Ensure).
- Monitor: add simple count of "bad_workspace" reasoning events.
- Post-fix: re-run the exact failing task title pattern from report ("Locate-CLI...").

If changes made, re-run `go test`, full docker cycle, burst rflow.

This review is complete, actionable, and directly addresses the "identify and fix these issues seen in agent reasoning" per user. The plan + augments will make git setup reliable under contention (K8s worker style: claim once, exclusive exec, self-heal).

**Primary output file:** `/Users/jonathandoughty/clients/fremenlabs/flume /flume/docs/reviews/git-concurrency-worker-review.md` (this doc). Short summary below.

---

**Short summary (for response):** The plan is a *correct and necessary first step* (running map stops intra re-submits per pool.go:85/Sync:75 + manager:242 2s cycles; .git IsDir + pre-clone rm in runner.go:1398 fixes partial states post-abort at 1457), directly targeting the concurrency (multiple RunWorker/Ensure on "implementer") + cleanup gaps causing the exact "refs/files-backend abort" (concurrent clone race) and "chdir no such" (TOCTOU + rm during checkout:1742/1800) + reset loop (clearStale:1598 after unknown class from classify:1738). Investigation (full reads of pool/manager/claim/runner, greps, live docker logs capturing the strings on rflow + container .git inspection) confirmed roots: name collision (workers.go:31 + no host filter manager:270 despite replicas=2), unlimited WIP (claim:256 default), no auto-heal, dupe paths (runner:576/750/1390 + claim/dashboard), no critical section lock. Plan partially addresses (good for default case) but gaps remain (cross-replica still races effort; no self-heal on exact error path; etc.). Recommended: adopt plan + 6 augments (affinity+unique names, classify+retry rm on bad_workspace, default WIP=1, per-repo guard, central ResolveRepoPath, tests for Ensure/bad-state). Concrete edits sketched above. Full details + file:line evidence in the md. go test ./internal/worker/... passes now; add cases. This stabilizes as intended. (Todos tracked 12 items end-to-end.)

**Files referenced (absolute):**
- /Users/jonathandoughty/clients/fremenlabs/flume /flume/internal/worker/pool.go
- /Users/jonathandoughty/clients/fremenlabs/flume /flume/internal/worker/runner.go (esp. 272,1360,1398,1457,1467,1689,1705,1742,1800,1814)
- /Users/jonathandoughty/clients/fremenlabs/flume /flume/internal/worker/manager.go (242,254,270,395)
- /Users/jonathandoughty/clients/fremenlabs/flume /flume/internal/worker/claim.go (249,283,332,347,403)
- /Users/jonathandoughty/clients/fremenlabs/flume /flume/internal/worker/workers.go:31
- /Users/jonathandoughty/clients/fremenlabs/flume /flume/internal/dashboard/api_projects.go:217
- /Users/jonathandoughty/clients/fremenlabs/flume /flume/internal/config/config.go:109
- docker-compose.yml (worker replicas)
- /Users/jonathandoughty/clients/fremenlabs/flume /flume/docs/reviews/git-concurrency-worker-review.md (this)

Ready for user review + implementation. If needed, I can apply specific search_replace for the augments.