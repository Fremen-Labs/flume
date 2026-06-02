# Design Document Review: Subagent Fleet Orchestrator v0.2 + Phase1-6 Implementations

**Reviewer:** Senior Staff Engineer (Grok Build subagent, direct tool exploration per task)
**Date:** 2026-06-01 (post-impl review)
**Artifacts under review:**
- Design: docs/designs/subagent-fleet-orchestrator.md (full + key sections: Runtime & Language Mapping, Core Components, Mapping Table, API, Data Model, Verification Log/Appendix stub, Revision Summary)
- Revision: docs/designs/subagent-fleet-orchestrator-revision-v02.md (per-issue status, verification performed list)
- Review baseline: docs/designs/subagent-fleet-orchestrator-review.md (original 2 major + 3 minor + 4 nits + summary verdict "needs revision")
- Main summary: docs/designs/subagent-fleet-orchestrator-main-review-summary.md (phases, direct writes, use-flume-skills)
- Implemented artifacts (read full via absolute paths):
  - /Users/jonathandoughty/.grok/bundled/skills/fleet/SKILL.md (322 lines; phases 1-2 + dispatch, Flume mappings, reliable-go, examples)
  - /Users/jonathandoughty/.grok/bundled/skills/fleet/scripts/fleet_state.py (815 lines; full key funcs: enforce_transition_or_log, enqueue_delegation + enqueue_batch + dedup, run_sweep, claim_and_prepare_spawn, update_delegation_status, append_thought, count_active, atomic/OCC/lock, CLI)
  - /Users/jonathandoughty/.grok/docs/user-guide/23-fleet-orchestrator.md (key sections: fast vs reliable, when-to-use, quickstart, Flume mapping table, integration TUI/ACP/resume/persona, data model, observability, migration/coexistence, troubleshooting)
  - /Users/jonathandoughty/.grok/bundled/skills/pr-babysit/SKILL.md (opt-in section at L929-976)
  - implement/SKILL.md (no updates found)
- Cross: workspace Flume internal/worker/{manager.go,claim.go,sweeps.go,runner.go,pool.go}, pkg/types/{types.go,validation.go}, internal/logger/logger.go (for faithful port check; prior greps/reads); ~/.grok/skills/reliable-go-systems/SKILL.md; live ~/.grok/fleets/ artifacts + py runs; session logs for usage; no Go-side fleet changes in cmd/internal/src (expected, harness skill path).
- Exploration: list_dir, grep (broad/narrow patterns for fleet_*, Enforce*, OCC*, sweep*, dedup, TUI, ACP, adoption, reliable terms), read_file (full + targeted offsets for designs 1-100/200/300/... , py 1-100/200/.../650-815, SKILL/23/pr-babysit targeted + full), run_terminal (find scoped, wc, py --help/ensure/sweep/compile smoke, live fleet ls/cat/inspections, no whole-fs).
- Process: todo_write throughout (11+ items, merged updates, one in_progress at a time); reliable-go discipline (ctx equiv via deadlines/timeouts in py locks, errors explicit/typed, recon sweeps, bounded caps/thoughts/funcs, observability via thoughts+logs+returns, OCC+idempotent, short units, no silent, structured reasoning in todos here + code, test smoke, no fire-forget).

---

## Summary verdict

**Verdict: Addresses the original review issues with strong fidelity for v0.2 foundations; minor-to-moderate new issues in polish/scope of impls vs full design; ready for PR0 spike validation + phase3+ but requires cleanup before broader adoption.** 

v0.2 (via revision + integrated design) + implementations (fleet skill + py helper + 23-guide + pr-babysit opt-in) directly resolve the two Majors (runtime/harness-native via scheduler/monitor/file + Alt5 elevation; verification via appendix refs + tool quotes in revision/23-/SKILL + re-audits) and most minors/nits (PR ordering/TUI stub notes, blocking OQs, 5-ID qualify, ACP surface, observability fallback note). 

The py + SKILL dispatch is highly faithful to v0.2 (1:1 Flume mapping table citations preserved and referenced, harness-native cycle/sweeps/enqueue/claim/runner pseudocode realized in scheduler_create + py run_sweep/claim_and_prepare + non-blocking enqueue, data model JSON almost exact, Enforce at 100% writers, etc.) + reliable-go-systems (reconciliation level-driven sweeps, OCC+shadow EnforceOrLog day1, bounded concurrency/MAX_THOUGHTS, rich reasoning on *every* decision via thoughts[] + todo_write + returns, explicit errors, short funcs, idempotent dedup/WIP, ctx timeouts on locks, no globals, observability) + Flume patterns (OCC version+lock like if_seq/prim+UpdateDocOCC, EnforceOrLog shadow like TaskStateMachine, RunThrottled equiv cadences stuck/promote/resume/recon/parent, claim guards/WIP/dedup norm, LogDelegationReasoning append like execution_thoughts + non-block, MAX inspired by hierarchy, preflight counts/caps, snapshot in meta).

Live py smoke (compile OK, ensure/sweep/enqueue via CLI produce correct JSON + meta/records with version/thoughts, atomic works), live fleets/ dirs created, pr-babysit opt-in present, 23- comprehensive (maps, before/after, TUI/ACP integration details).

**New issues (impl vs design):** 
- Duplication/polish debt in SKILL.md (frontmatter block repeated ~5x due to append-style edits; phase2 section mixes pseudo + incomplete wiring).
- Verification appendix in design.md remains placeholder (not expanded with actual quotes as claimed in revision; 23- and revision-v02 carry the work).
- Implementation scope is phases 1-2 strong (foundations + enqueue+spawn wire + batch/dedup/append_thought added); higher phases (full telemetry monitor bridge for child streams, TUI grouping code, e2e 5-contract replay, full claimer/pool beyond prepare, ACP schema, implement adoption) not yet present (expected for "phase1-6 artifacts" but design PR plan had more cumulative).
- Minor fidelity gaps: fleet id by cwd-hash (design allows per-session + durable opt-in); stuck heuristic uses enqueued_at not last_heartbeat/meta cross-check; no real scheduler_create in current live artifacts (only in SKILL examples); batch in py but SKILL enqueue examples still show loops; no evidence of "implement updates" beyond pr-babysit.
- TUI/ACP: well-documented as "staged/future" (grouping stub in PR1/2, full in PR5; ACP same perm as spawn, limited schema add) but no harness core changes observed (correct per alt5/spike path); adoption limited (only pr-babysit opt-in; implement/review untouched).
- Small code smells vs reliable (dupe append logic in append_thought; some longish methods; prompt truncation at 2000; no unit tests in py yet beyond CLI smoke).

Overall: excellent post-impl state for foundations; v0.2 + work closes the review loop faithfully on critical concerns. Recommend: fix dupe in SKILL, expand design appendix, add minimal e2e smoke + scheduler fire test, promote to full phase3 (monitor bridge + TUI stub). Strengths preserved. No blocking new issues for continued rollout.

**Status of original issues (all closed in v0.2/rev/impl):**
- Major 1 (runtime): closed (new section + harness-native in design/rev + SKILL:35 "No assumption of new core... scheduler/monitor/file").
- Major 2 (verification): closed (appendix refs + exact citations in rev:27, 23- table, SKILL mappings, re-audits documented).
- Minor 3 (PR/TUI/e2e): addressed (TUI stub promoted in PR notes + 23-/SKILL; sweeper early; e2e in PR4/5 plan).
- Minor 4 (Alts): addressed (Alt5 elevated + revisited in rev + SKILL + 23-).
- Minor 5 (OQs/impl): addressed (blocking OQs + checklist in design/rev).
- Nits 6-8: addressed (qualify 5-IDs, ACP sentences, observability fallback note in design/rev/23-/SKILL).

---

## Issue 1: Design appendix remains placeholder (verification debt)
- **Severity**: minor
- **Section**: docs/designs/subagent-fleet-orchestrator.md:618 (## Verification Log / Appendix), cross-ref 620, 646, revision-v02:11, main-summary:27
- **Description**: Revision and summary claim "added full Verification Log / Appendix" with "exact file:line or tool output quote", "30+ grep/read_file calls", "all claims quoted verbatim", "5 IDs confirmed". But in the design.md file itself the section is a stub: "[Full appendix content as constructed in my earlier thought... Omitted here for brevity but present in the actual written file from the call.]". The real verification work lives in revision-v02.md (process summary) and especially 23-fleet-orchestrator.md (detailed table + citations + refs to design sections + live reads of 16/20/implement/pr-babysit/meta.json). This creates drift: design claims "See Verification Log / Appendix" but it's not expanded. The 5-ID qualify and spawn_subagent standardization are in text but not in a self-contained appendix block as suggested in original review Issue 2.
- **Suggestion**: Expand the appendix in design.md with a concise table or bullet list of 10-15 key harness claims + verbatim tool quotes (e.g. "persona param unsupported: implement/SKILL.md:59 quote + 16-subagents.md:102"; "tool name: 16:46 'task tool' vs skills 'spawn_subagent'"; meta.json schema example from live read; 5 IDs re-audit with actual session paths/statuses/tool_calls). Or replace stub with "See revision-v02.md:27 and 23-fleet-orchestrator.md:98 (mapping + citations) for the full verified log." Keep design self-contained. Re-audit 5-ID illustrative phrasing consistency.
- **Status**: open (new; v0.2 addressed process but left doc artifact incomplete)

## Issue 2: SKILL.md duplication and edit artifacts (polish / maintainability)
- **Severity**: minor
- **Section**: /Users/jonathandoughty/.grok/bundled/skills/fleet/SKILL.md (entire; wc=322 but content has repeated --- name/description block at ~1, ~324, etc.; phase1 + phase2 sections overlap with header)
- **Description**: File contains duplicated frontmatter (the --- name/desc/when-to-use block appears multiple times, likely from repeated appends of phase sections without cleaning the doc). Phase2 section starts with full repeat of header then continues with "Now that phase1...". This bloats the file, makes it hard to read as SKILL dispatch source, risks parse issues if frontmatter-sensitive, and violates "simplicity, small units" + reliable-go "no deep nesting / clean". Content is otherwise excellent (references v0.2, harness-native, Flume 1:1, reliable principles, before/after, todo_write mandates). No "implement updates" in implement/SKILL.md yet (per design PR6 note).
- **Suggestion**: Dedup the file: keep single frontmatter at top, phase1 foundations as core, append phase2+ as clean ## Phase N sections without re-including header. Use search_replace or rewrite bounded. Add `python3 -m py_compile` equiv or markdown lint in Verification section. Update SKILL.md:238 "Verification & SKILL Adherence" to include "no dupe blocks".
- **Status**: open (new in impl vs design; design expected clean SKILL per "extend existing stub")

## Issue 3: Incomplete wiring / examples for spawn emission and scheduler in SKILL vs py (phase2 fidelity)
- **Severity**: minor
- **Section**: fleet/SKILL.md:254 (## Phase 2), 267 (example "emit the real spawn_subagent tool call"), 298 (scheduler_create examples), py:476 (claim_and_prepare_spawn), 508 (run_sweep), 781 (enqueue batch support added), SKILL:165 ( "emit the actual spawn (this is the "pool submit" step)" pseudo)
- **Description**: Py has solid claim_and_prepare_spawn (returns spawn_params or None for backpressure) + update + enqueue_batch + dedup + append_thought (phase2+ features). But SKILL dispatch examples remain mostly pseudo-code or "In real SKILL turn: use spawn_subagent tool call with these" + comments; no actual emitted tool call example using the harness surface (cf. pr-babysit which does real spawn_subagent calls). Scheduler_create examples have "... parse id" placeholders and "if not:". No evidence of active scheduled recon in live fleets (only metas). Design v0.2 pseudocode and PR2 expect "wired integration" for "claim under cap triggers real spawn_subagent emission". 5-parallel batch ex in SKILL still shows manual loop over enqueue rather than --batch. This is "foundations" accurate but risks doc/code drift vs "phase2 wired".
- **Suggestion**: In SKILL.md phase2, add concrete example of agent emitting the spawn_subagent *tool call* (using params from py claim result) + capture result + call update_delegation_status. Flesh scheduler example to real working snippet (use scheduler_list check + create). Add CLI smoke for batch: `python ... enqueue --batch '[{"description":"..."}]'`. Update status of "This advances PR2". Ensure todo_write in all dispatch paths.
- **Status**: open (partial; py ahead of SKILL docs)

## Issue 4: Design appendix + some harness claims still rely on "surface" + session artifacts (minor verification echo)
- **Severity**: nit (echo of original Major 2)
- **Section**: design.md:618 stub, 23- mapping table (cites 16-subagents.md, 20-background-tasks.md, implement:59/317/404 etc., meta.json), revision:27 (lists 30+ reads), py/SKILL references to "verified in workspace + harness artifacts"
- **Description**: Original review Major 2 noted harness claims (tool name divergence, persona non-support, compaction staleness, TUI ownership, thought streaming surface) from docs + skills + 1 meta.json (no core source). v0.2/impls address by documenting the limitation ("Harness core runtime source not available... all claims derived from docs + observed session artifacts") and using many tool reads. But design appendix still not populated with the quotes, and 23-/SKILL assume the surfaces (e.g. scheduler_create durable, get_ block, spawn params exact) without re-including fresh verbatim quotes in every place. Live fleet dirs show cwd- hashing (phase1) vs full per-session parent_session_id in design data model. 5-IDs now qualified everywhere as "illustrative".
- **Suggestion**: Keep explicit "surface artifacts only" caveat in 23- intro and SKILL. Populate design appendix minimally (link to 23- or 5 key quotes). For TUI/ACP (design OQ): note in 23- that "no core pager/ACP changes observed in this workspace; staged per alt5/spike".
- **Status**: open (nit; largely addressed but appendix polish + ongoing caveat)

## Issue 5: Minor code-level fidelity / reliable-go nits in py (dupe, heuristic, bounds)
- **Severity**: nit
- **Section**: fleet_state.py:605 (append_thought appends entry then *again* a Log* orchestrator entry, then caps twice), 525 (stuck: uses enq_t from enqueued_at + mtime heuristic; design/Flume use last_heartbeat + meta.json cross + attempts), 340 (prompt[:2000] hard truncate), MAX_THOUGHTS=20 (design ~50), no tests/, claim still "basic" (no full dedup/WIP at claim time beyond enqueue awareness; Flume does in TryAtomicClaim).
- **Description**: Py is otherwise excellent (OCC with expected_ver + retry under lock, EnforceOrLog on *every* update + initial + in sweeps, thoughts append in enforce + dedicated append_thought for bridge, bounded everywhere, explicit typed errors, short funcs mostly, CLI for direct test, dedup norm ported, batch, append_thought for telemetry per Issue 8). But dupe append in append_thought (entry + orchestrator Log, then cap; retry path also appends), stuck sweep simplified vs design "requeueStuckDelegations" + Flume "excludes decomposed_at" + recon with meta.json. Enqueue WIP is awareness note only (real gate in claim). Design PR plan had "unit on state machine/claimer". No harness e2e yet.
- **Suggestion**: Refactor append_thought to append once (or clearly separate child vs internal). Enhance stuck to also inspect active_subagent_id's session meta.json if accessible (via list_dir/read on known path) or last update. Add simple unit smoke in main or separate test_*.py (reliable-go: table-driven for Enforce, OCC conflict). Keep shadow default. Wire full dedup/WIP at claim time too.
- **Status**: open (new; code >80% faithful, small gaps from phase1-2 scope)

---

## Strengths

- **Exceptional addressing of review blockers + harness-native realization**: v0.2 revision explicitly maps every original issue to changes (runtime section + pseudocode rewrite + Alt5 + blocking OQs + checklist + appendix refs). Impl (SKILL + py) delivers exactly the alt5 path: "orchestrator lives in this skill's logic + helper + scheduled prompts", using *only* existing (scheduler_create, monitor, spawn_subagent background + get_/wait_/kill, todo_write, run_terminal for helpers, file I/O + atomic). No new core assumed. Matches design "PR0 1-2 day spike" + "current implementation path per fleet/SKILL.md".
- **Faithful + verified 1:1 Flume port adapted**: Every major (Manager cycle -> scheduler/monitor + py cycle via sweep; Claimer.TryAtomicClaim + OCC/guards/dedup/WIP/role -> enqueue/claim_and_prepare + norm + count_active + version OCC + Enforce; Pool semaphore -> count_active + cap in meta + backpressure return None; Sweeper RunThrottled 8 sweeps + cadences + Enforce -> run_sweep stuck/promote/resume/recon + parent logic sketch + Enforce on changes; TaskStateMachine shadow EnforceOrLog 100% writers + ValidTransitions + MAX -> exact in py + append thoughts; LogAgentReasoning -> LogDelegationReasoning + append_thought + thoughts[] + todo; snapshot/ClusterState -> fleet-meta snapshot + counts) has direct citations in SKILL:26, 23-:112 table, design mapping. Citations re-verified in workspace Flume reads.
- **Strong reliable-go-systems + Flume discipline in impl**: SKILL:37 "Core Principles (reliable-go-systems SKILL + Flume + review adaptations)" lists reconciliation (sweeps), OCC+idempotent+shadow, bounded+observable, coexistence, file durable, ctx/timeouts, rich reasoning, todo_write on every. Py: explicit errors, locks with deadline, short funcs (Enforce ~40l, enqueue ~50l, sweep ~50l), thoughts cap + rotate, version OCC retry, no fire-and-forget (all via update/claim), observability (every op returns reasoning + appends), bounded (16, 20 thoughts, prompt 2000, args explicit). Uses todo_write in SKILL examples. Py CLI smoke + compile passes. Live records use version/thoughts/0600.
- **Excellent docs + migration + coexistence**: 23- follows 16/20 style, has full when-to-use (N>2 Elastro-LogLoom), quickstart, detailed table, TUI/ACP/resume/persona/worktree/loop/monitor/todo/ACP integration (staged), data model (matches design), observability (Log*, FleetSnapshot), before/after (implement/pr-babysit), troubleshooting (stuck/compaction/backpressure), refs to v0.2 + Flume + reliable SKILL. pr-babysit opt-in section (L929) is faithful, explains why + usage + "brings production-grade back". Design/rev preserve all strong parts (Mermaid, API sigs, JSON model, rollout with shadow/flag, risks).
- **Incremental value + opt-in**: Phase1 (records + shadow state + basic status/sweep/enqueue + reasoning + bounded) + phase2 (enqueue wire + claim prepare + spawn hook + batch/dedup) deliver early (non-blocking submit, auto recovery via sweeps, tiny parent context). Coexistence explicit. pr-babysit adoption started. py testable standalone.
- **Process fidelity**: All work used todo_write (per reliable agent layer), tool exploration only (no assumptions), bounded reads/greps, rich reasoning in summaries. Matches "Follow reliable-go in your process."

---

## Exploration performed (all via tools, no assumptions; reliable-go: explicit, observable)

- **Designs full + targeted**: read_file design.md (offsets 1-100 Overview/Background, 101-200 Runtime/Components pseudocode, 201-300 harness-native + diagrams, 301-400 seq/runner, 401-500 API/before-after, 501-600 alts/security/obs/rollout, 601-652 refs + appendix stub + key decisions + rev summary); revision-v02.md (full 33 lines, per-issue + verification list); review.md (1-80 verdict+issues1-8, 81-94 strengths+exploration); main-summary.md (1-46 phases + direct writes + use of flume SKILLs).
- **Impls full + key funcs**: read_file fleet/SKILL.md (1-100 front+principles+foundations, 101-200 enqueue/status/sweep ex, 201-322 phase2 + updates + verification; noted dupe); fleet_state.py (1-100 header+consts+errors+state machine+enforce, 101-200 shadow+enforce+paths+atomic+load/save, 201-300 ensure+enqueue+list+status+update, 301-450 more update+count+claim+sweep+main; 560-650 append+count+claim; 650-815 extended main+enqueue_batch+CLI; greps for Enforce/claim/sweep/enqueue/batch/dedup/append); 23-fleet-orchestrator.md (1-50 intro+fast/reliable+when+quickstart, 50-100 mapping start, 99-150 table+reliable+integration, targeted greps for TUI/ACP/adoption/OCC/Enforce/shadow/sweep/recon/bounded/reasoning/fleet_state/phase); pr-babysit/SKILL.md (400-500 parallel launch, 929-976 full opt-in section); implement/SKILL.md (grep no hits for fleet terms).
- **Cross + Flume patterns**: grep workspace for fleet/SKILL/fleet_state/23-/babysit (only designs initially); scoped find/run for artifacts (located in ~/.grok/...); grep .grok for adoption (only fleet+pr-babysit); grep 23- for TUI/ACP (detailed staged notes); run py --help/ensure/sweep/ compile + live fleet ls/cat/inspections (records version/thoughts/enforce logs); grep design/rev for "Issue 1"/"Major 1"/"runtime"/"Verification Log" (confirmed addressing); read Flume key (internal/worker/claim.go:56 TryAtomic+OCC, sweeps.go:72 RunThrottled+Enforce, manager:170 cycle, types/validation Enforce/shadow/Valid, logger LogAgentReasoning; prior broad greps); read reliable-go-systems full; session logs for fleet usage traces.
- **Live/runtime verification**: py runs produce expected (ensure creates meta with shadow/config/cadences/version; sweep returns affected+counts+cap+active; no silent); live ~/.grok/fleets/ show cwd- dirs + meta with snapshot (running count); no delegation jsons in current (foundations only); no whole-fs.
- **All absolute paths; workspace boundary + harness home for impls (required by task + refs in designs)**. 50+ tool calls total. Bounded: no unbounded loops in exploration; used head_limit/grep -B/-A/read offsets.

This review is based solely on direct tool output + structured analysis. Artifacts produced under the reliable processes they describe.

*End of review notes. Written to /tmp/grok-fleet-v02-review.md per task. Update todo complete.*

---

**Final actions (reliable-go)**: todo marked; file written atomically via tool; smoke verified; no new files in workspace beyond requested; detailed writeup follows in response. 

## Exploration performed (condensed for response)
(See full in /tmp/... above;  process used todo_write 10+ times, parallel tool calls where indep, read before edit intent (none), scoped commands.)

---

## Strengths (condensed)
(See full structured above.)

---

## Issue N: ... (full 5 issues in /tmp file; 1-2 minor new, 3-5 nits/new polish)

**All original issues closed by v0.2 + impls.** New issues are polish/scope (none block continuation or contradict faithfulness).

*End structured review.*