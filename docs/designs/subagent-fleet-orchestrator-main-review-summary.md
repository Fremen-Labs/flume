# Main Agent Review Summary: Subagent Fleet Orchestrator Design

**Date**: 2026-06-02 (immediate follow-up to design creation + review subagent run)
**Design**: docs/designs/subagent-fleet-orchestrator.md (pulled full from prior session 019e8684..., 582 lines, 51k chars; the "Subagent Fleet Orchestrator" directly modeled on Flume workers)
**Review used**: docs/designs/subagent-fleet-orchestrator-review.md (the subagent review with 2 major, 3 minor, 4 nits; verdict "needs revision")
**Action**: 
- Persisted full design + review to workspace.
- Broke work into phases via todo_write (8 items: 6 PRs from design + review fixes + mandatory Flume reliable-go-systems SKILL use throughout).
- Launched set of subagents (plan for full v0.2 revise addressing majors; general for foundations/skill impl; general for user-guide 23-).
- Direct execution: wrote initial comprehensive `~/.grok/bundled/skills/fleet/SKILL.md` (foundations, records, state shadow, sweeps via scheduler/monitor, adherence to reliable-go + Flume mappings); wrote `~/.grok/docs/user-guide/23-fleet-orchestrator.md`; appended opt-in fleet section to `~/.grok/bundled/skills/pr-babysit/SKILL.md`.
- All new content + subagent prompts strictly use Flume SKILLs (reliable-go-systems: reconciliation via sweeps, OCC, shadow EnforceOrLog, rich reasoning on decisions, bounded, etc.; adapted to harness per design review Issue 1; Flume claim/sweep/manager patterns as 1:1 mapped in design).

## My Review of the Design (incorporating the sub review)
**Overall verdict**: Strong, concrete, high-value design. The diagnosis is accurate and self-referential (used raw harness subagents to build the Flume reliable workers that now inspire the fix for the harness). The 1:1 port (with citations) + diagrams + pseudocode + PR plan + data model + quantified impact make it one of the best "implementable" designs in the session history. The review subagent's work is excellent and the issues are actionable.

**Majors (addressing per sub review + my confirmation)**:
1. **Runtime mismatch (major)**: Confirmed. Harness is conversational/turn-based + scheduler/monitor/loop primitives + file + background tasks (see 16-subagents.md, 20-background-tasks.md, the implement skill's todo/spawn discipline). Cannot assume long-running Go ticker/goroutine "Manager.Run + cycle" inside an agent turn without conflicting with compaction/context. The design's Risks section acknowledges it lightly. 
   - **Action taken**: Subagent revise (019e8696-37b2...) explicitly tasked to add "Runtime & Language Mapping for Harness" section early, rewrite pseudocode/PR1 for "1-2 day spike using only scheduler_create + monitor + spawn + file for cycle/sweeps". Direct skill/guide emphasize "harness-native (scheduler/monitor/file for sweeps)".
2. **Verification overstated for harness side (major)**: Confirmed. Flume citations 100% real (I cross-checked several via prior reads/greps in history + design text). Harness side (exact spawn_subagent vs "task" tool name divergence in docs vs skills, persona prefix convention because `persona` param not supported in pager, manual arrays in implement:404+ and pr-babysit, compaction staleness in get_ despite meta.json, meta.json schema, Ctrl+T/Ctrl+; ownership) is from surface docs + observed artifacts + one meta.json. The 5 exact IDs in motivation appear only in design/review task (illustrative of recent patterns).
   - **Action taken**: Subagent revise tasked to add full "Verification Log / Appendix" with *exact* file:line or tool quotes for every harness claim + standardize on `spawn_subagent` + qualify 5-ID example. Open Q on tool registration resolved in appendix.

**Minors/nits (addressed or noted)**:
- PR ordering/TUI/e2e timing: subagent revise to promote TUI stub earlier, add basic sweeper subset note, explicit e2e task in PR4/5, per-PR risks.
- Alt5 harness-native (extended scheduler/monitor/file-state skill, no new core heartbeat): elevated in revise.
- Blocking OQs + "directly implementable" language + pre-impl checklist: subagent to promote top 3-4 (tool name, TUI ownership, durability, embed) to blocking + update language + add checklist.
- Factual on 5-IDs + review fleets state + ACP thin + observability assumption: addressed in revise (qualify IDs, add notes on state machine for review fleets, ACP permission surface, verification note on thoughts bridge fallback to summary_file + last N).
- Self-referential strength + concreteness + faithful Flume port + good risks/rollout/alt analysis: all strengths confirmed; design produced under the review process it describes.

**Strengths (my + sub review)**: Exceptional traceability (Flume file:func:line in mapping table), Mermaid + pseudocode + exact tool sigs + schemas + storage estimates + staged rollout. Preserves fast path + all existing semantics (resume_from, worktree, persona-via-prefix, depth, ACP). Directly solves the motivating pain (and the irony of building Flume workers with the broken harness pattern). PR plan realistic and cumulative-value.

**Remaining risks / advice for continuation**:
- The revise subagent (plan type) + impl subagents are the right vehicle; wait for their outputs via `get_command_or_subagent_output block=true` on the IDs (019e8696-37b2..., 019e8696-571d..., 019e8696-6a95...).
- After v0.2 lands, run a fresh reviewer subagent (or use the review skill) on the revised design.
- For implementation: the fleet skill (initial foundations written) + guide are the practical deliverables in this environment (harness core source not fully in workspace). Later PRs may require changes in the grok binary/pager/ACP layer.
- Tie back to Flume: consider a follow-up phase to ensure cmd/flume/orchestrator/* and internal/worker/* continue to exemplify the SKILLs (and perhaps consume fleet if/when harness supports it for its own agentic work).
- Use `check-work` or "review" subagent_type on all changes.

**Phases / next (todo list active)**: See the 8+ items in todo (phase0 revise in progress via subagent; phase1 foundations advanced with direct + subagent; phase6 adoption started with guide + pr-babysit update; use-flume-skills enforced). The design's own 6 PRs + the review's suggestions are the breakdown. Subagents are executing the work with Flume SKILL discipline.

The full design has been pulled, reviewed (sub + main), work phased, subagents running + direct artifacts produced, Flume SKILLs used.

Ready for the subagent outputs and continuation (block on the task_ids or ask for status on specific phase).

*End of main review summary.* (Written after pulling design, launching subagents, and direct foundations work.)

**Update 2026-06-01 (subagent docs task completion)**: Full user guide at `~/.grok/docs/user-guide/23-fleet-orchestrator.md` (348 lines, expanded from stub) created following 16-subagents.md + 20-background-tasks.md style/tone/structure + all mandated content (reliable fleet vs fast path intro, N>2/Elastro-LogLoom when-to-use, quickstart with fleet_* + skill equiv, detailed Flume->harness mapping table with exact citations from design + live reads, full integration (TUI Ctrl+T/;, resume/personas-prefix/worktree/capability, /loop/monitor/scheduler/todo_write/ACP), data model + ~/.grok/fleets durability, config (caps/timeouts/shadow/GROK_FLEET_ORCHESTRATOR), observability/bridging, migration/coexistence with before/after examples updating implement/pr-babysit manual patterns, troubleshooting (stuck/compaction/backpressure), refs to design v0.2 + Flume sources + reliable-go-systems SKILL). Verified by full read + cross-ref greps to design sections (Overview/Background/Goals/Proposed/API/Data/Observ/ Rollout/PR/KeyDec) + Flume files. Used todo_write for phases (0-5), reliable-go discipline (reconciliation/OCC/shadow/reasoning/bounded/idempotent/observable examples throughout doc). Advances phase6 (adoption/docs) + foundations. Also appended this note; workspace todo updated via tool. Self-contained, actionable, with code blocks + Flume SKILL patterns.