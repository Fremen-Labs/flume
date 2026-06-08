# Flume MCP Integration Design: Robust + Massively Efficient Exposure for Harness Agents

**Author:** Grok Build (software architect, reliable agent orchestration + MCP specialist)  
**Date:** 2026-06-03  
**Status:** Production-grade design (ready for implementation review)  
**Version:** 1.0  
**Related:** Builds directly on "prior discussion of MCP for Flume to help with Plan New Work / Work Queue breakdowns from the interactive side"; unifies Elastro/Logloom contract, work queue state, etc. for agents *outside* the Go worker loop (interactive sessions, subagents, fleet orchestrator, skills). Cross-references: `docs/designs/subagent-fleet-orchestrator*.md` (ACP/MCP for fleet tools), `docs/designs/flume-workitem-orchestrator-design.md` (harness vs Flume comparison, reliable patterns), `docs/designs/plan-new-work-queue-reliability-fixes.md` (breakdown/queue reliability).  
**Process followed (per task + reliable-go-systems patterns):** Used `todo_write` for >3-step design phases (11 tracked, updated live); broad-then-narrow exploration via `list_dir` + `grep` + targeted `read_file` (with offsets/limits for large files like handlers.go:845 LOC) + `open_page`/`read_file` for `~/.grok/docs/user-guide/07-mcp-servers.md`; read-before-cite + exact `file:line` verification on every key artifact (no assumptions); explicit transient/permanent error classification, bounded scope (stayed within workspace + explicitly required external harness MCP docs); reconciliation/observability emphasis (LogAgentReasoning bridge, state machine, budgets); no code edits (read-only architect mode); final output is this polished design doc (plus this file as the artifact). All claims double-checked with greps/reads.

This design delivers **robust** (security-first scoping/auth/audit/graceful degradation/idempotency/no explosion) + **massively efficient** (long-lived pooled clients, direct-ES preference, result shaping + token-aware truncation + pagination + caching, low-overhead stdio primary, reuse of existing executors/RAG paths) MCP surface for Flume. It enables Grok/harness agents to treat Flume capabilities as first-class tools (namespaced `flume__*`).

---

## 1. Context + Requirements (Internalized via Exploration)

### Flume Architecture Verified (key files/lines)
- **Worker orchestration (internal loop only today):** `internal/worker/manager.go:40` (Manager owns Pool/Claimer/Sweeper), `internal/worker/claim.go:56` (TryAtomicClaim + OCC + dedup + WIP + git overlap), `internal/worker/sweeps.go:72` (RunThrottled + 8+ cadences + promotePlannedTasks with depth/budget/OCC/Enforce), `internal/worker/runner.go:110` (Runner struct), `runner.go:125` (`tools: NewToolRegistryWithElastro`), `runner.go:247` (handleImplementer ReAct loop), `runner.go:415-446` (MANDATORY AST VERIFICATION contract #4: `astVerified` gate before writes; `elastro_query_ast` / `logloom_ast_query` set the flag), `runner.go:1360` (EnsureTaskBranch), `runner.go:1423` (calls `git.EmbedCredentials`).
- **Tool exposure inside workers (ReAct + contract #4):** `internal/worker/handlers.go:24` (consts: `ElastroGraphIndex = "flume-elastro-graph"`, `LogloomEnrichmentIndex`, `LogloomASTIndex`), `handlers.go:283` (ToolExecutor iface), `handlers.go:289` (ToolRegistry), `handlers.go:300` (NewToolRegistry: always FS list/read/write/run_shell), `handlers.go:324` (NewToolRegistryWithElastro: registers elastro + logloom + binary checks + os.Exit(1) on missing), `handlers.go:418` (ElastroASTQueryExecutor: **direct ES** via `e.es.Search(ctx, ElastroGraphIndex, ...)` — *no* shell for query; multi_match + target_path wildcard; LogAgentReasoning + truncation to 8 hits), `handlers.go:447` (Name), `handlers.go:449` (Execute impl), `handlers.go:505` (`hits, err := e.es.Search...`), `handlers.go:626` (LogloomASTQueryExecutor: direct ES on enrichment primary + AST fallback), `handlers.go:652` (Execute), `handlers.go:738` (FS safety: prefix check under repoPath), `handlers.go:554` / `585` (findElastroBinary / findLogloomBinary: PATH + /opt/venv + $HOME + LOGLOOM_BIN etc.).
- **Planner RAG (server-side prefetch, *not* LLM tool calls):** `internal/dashboard/api_intake.go:26` (`planSessionsIndex = "agent-plan-sessions"`, `taskRecordsIndex`), `api_intake.go:469` (fetchPlannerRAGContext: 8s timeout ctx, best-effort), `api_intake.go:484` (calls queryElastroForPlanner + queryLogloom), `api_intake.go:529` (`executor := worker.NewElastroASTQueryExecutor(s.es, s.logger)` — reuses exact executor!), `api_intake.go:548` (truncate to ~2000 chars for planner: "if len(res) > 2200 { res = res[:2000] + ... }"), `api_intake.go:554` (logloom equivalent), `api_intake.go:744` (buildLLMMessages + injectRAG), `api_intake.go:770` (llmClient.Chat with PlanSessionID/budget). Target: Plan New Work <120s.
- **ES client (reuse everywhere):** `internal/es/client.go:30` (Client struct with httpClient), `client.go:41` (New: 30s timeout, TLS skip for self-signed), `client.go:78` (do with auth), `client.go:398` (Search: adds seq_no_primary_term, parses hits), `client.go:443` (Count), `client.go:548` (BulkFlusher), `client.go:619` (SearchRaw). Long-lived; used by dashboard + workers + intake + sweeps.
- **Git + secrets (scoped, masked in ES):** `internal/git/credentials.go:70` (EmbedCredentials), `credentials.go:114` (resolveToken: strict contract — ES holds only metadata + "OPENBAO_DELEGATED"; real in bao KV "flume/keys" or per-id paths via ADO/GH stores; env fallback), `credentials.go:152` (baoClient = secrets.NewOpenBaoClient), `credentials.go:423` (rewrite for x-access-token), `internal/git/host.go` (GitHub/ADO clients), `internal/secrets/openbao.go:28` (OpenBaoClient), `openbao.go:36` (New: 10s timeout), `openbao.go:51` (KVGet), `openbao.go:89` (KVPut), `internal/secrets/github.go:62` etc. (stores), `runner.go:1443` (LogAgentReasoning on cred failure in EnsureTaskBranch). Shared clones racy today (workspace/flume-reg-*).
- **Ingest/onboarding (binaries for *writes*, direct ES for queries):** `Dockerfile:95` (ARG ELASTR0_INSTALL=public, LOGLOOM_INSTALL=public), `Dockerfile:109` (venv), `Dockerfile:155` (pip elastro-client), `Dockerfile:178` (logloom wheel from GH), `Dockerfile:200` (post-build assert -x /opt/venv/bin/...), `Dockerfile:224` (PATH update), `internal/dashboard/api_projects.go:207` (cloneAndSetupProject), `api_projects.go:261` (elastro rag ingest shell), `api_projects.go:278` (runLogloomGraphIngest: build + ship to flume-logloom-ast), `api_projects.go:618` (verifyProjectASTDocs: scoped should-query on repo/project_id/... + total>0 fallback), `api_projects.go:502` (find logloom bin, duplicates handlers logic), `cmd/flume/commands/doctor.go:430` (more duplication), `docker-compose.yml:68` (build args), `cmd/flume/orchestrator/elastic.go:329` (bootstrap "flume-elastro-graph", "flume-logloom-ast" with mappings).
- **Indices (shared; scoping best-effort today):** `flume-projects`, `agent-task-records`, `agent-plan-sessions`, `flume-elastro-graph`, `flume-logloom-ast` / `flume-logloom-enrichment`, `flume-counters`, `flume-node-registry`, `flume-*-tokens` (meta), `flume-settings` etc. (see orchestrator/elastic.go:162 allIndices + api_intake + handlers:24).
- **Types + telemetry:** `pkg/types/types.go:82` (Task with ProjectID/repo, PlanSessionID, HierarchyDepth), `types.go:191` (Project), `types.go:362` (TaskStateMachine + shadow default), `internal/logger/logger.go:363` (LogAgentReasoning: slog + appendExecutionThoughtNonBlocking via Painless to execution_thoughts[] + ES bridge), `logger.go:80` (LogStateTransition), `runner.go:82` (dual emission in updateTaskLeaseState), `api_intake.go:496` (s.logReasoning for RAG).
- **Other exposures:** `src/gateway/skills/registry.go:50` (separate skill registry for mesh routing; *not* ReAct tools), `internal/dashboard/server.go:127` (New + registerRoutes: /api/snapshot, /api/projects, /api/intake/*, /api/tasks/*, /api/security/secrets/reveal (admin-gated)), `cmd/flume/main.go:40` (subcmds including dashboard/worker-manager standalone), `cmd/flume/services/supervisor.go:165` (StartDashboardOnly), `cmd/flume/orchestrator/health.go` + `vault.go:27` (AwaitOpenBao + /sys/health).
- **Current external (pre-MCP):** Shell `curl` to dashboard REST (manual parse, no schemas, no shaping, no audit bridge), direct ES (leak risk), local elastro/logloom bins (dupe discovery), env/PATs for git. No first-class for harness agents doing PM-like queue/RAG-driven breakdowns.

### MCP Harness (verified from `~/.grok/docs/user-guide/07-mcp-servers.md`)
- Config: `~/.grok/config.toml` + project-scoped `<repo>/.grok/config.toml` (highest prio; only [mcp_servers] section). stdio (command+args+env) or HTTP/SSE (url + headers + OAuth auto). `startup_timeout_sec`, `tool_timeout_sec` + per-tool overrides.
- Usage: `search_tool` (discover), then `use_tool` with fully-qualified `server__tool` (e.g. `flume__elastro_query_ast`). `/mcps` modal, `grok mcp add/remove/list/inspect`.
- Transports: Prefer native HTTP/SSE over npx proxy for remote. Local stdio for FS/databases (perfect for co-located `flume start` + local grok).
- Live: `grok_com_github` (43 tools) — namespacing, schemas fetched before call.
- Timeouts/OAuth/compat: detailed; debug with `GROK_LOG_FILE=1`.
- Project scoping in harness: walks cwd to git root for .grok/config.toml.

### Constraints + Goals (from context + designs)
- <120s planner targets, backpressure, per-plan budgets (api_intake:166 ItemBudget/TokenBudget + checkPlanBudget + scripts), state machine (types:356), LogAgentReasoning telemetry everywhere, no explosion (plan-new-work-queue-reliability-fixes.md P0), shared workspace racy.
- Unify for "outside Go worker loop": interactive/fleet/subagents use Flume's Elastro/Logloom contract + queue/plan state + scoped secrets (same as internal).
- Before/after (see table below).

---

## 2. Design Principles (Robust + Massively Efficient)

**Robust (security/ reliability first):**
- **Per-project scoping for *all* tools:** Every call *requires* `project` (or `repo`) arg. MCP layer: (1) validate exists in flume-projects, (2) inject strict ES `term`/`bool` filter on `repo`/`project_id`/`project`/`repository` (see api_projects.go:637 verify logic), (3) post-filter results before return to LLM. Never leak cross-project. For indices without tags (elastro dynamic), fall back + explicit post-filter on doc paths/fields + error if mismatch.
- **Auth:** Reuse existing (OPENBAO_TOKEN, FLUME_ADMIN_TOKEN, per-repo PATs via resolveToken). stdio: inherit env (trusted local co-located); optional `FLUME_MCP_TOKEN`. HTTP: Bearer (static or OAuth via harness), mTLS opt-in. Per-tool: `flume__get_git_*` further delegates to scoped resolve (no raw bao dump). `verifyAdminAccess` (api_security.go:234) pattern for sensitive.
- **Graceful degradation:** If ES/OpenBao down/timeout: clear structured error ("flume: elasticsearch unavailable for project X (index may be empty; check health via flume__get_system_health)"), + always emit LogAgentReasoning. Never panic/hang. Tools return usable partial + reasoning.
- **Idempotency/timeouts/retries:** All mutators (start_plan) reuse intake dedup/budgets/OCC. Per-tool ctx timeout (MCP config + hard 30-180s defaults; planning can use FLUME_PLANNER_TIMEOUT_SECONDS). Retries *only* transient (e.g. 5xx/429 on ES; jittered backoff, max 2). Permanent errors (bad project, budget exceeded, auth) fast-fail + audit.
- **Audit + no explosion:** *Every* MCP tool call emits `flumelogger.LogAgentReasoning(ctx, taskID|planID|"mcp-global", "mcp-bridge", reason, meta{tool, project, duration_ms, args_preview, hit_count, ...})` (reuses logger.go:363 + bridge to execution_thoughts + Logloom). Mutating tools (flume__start_plan_new_work, future requeue) *must* go through exact budget checks (checkPlanBudget api_intake:1667), TaskStateMachine.Enforce (types.go:409), WorkPaused, item caps (getSmartMaxLeafTasks etc.), same as UI/commit. No direct writes bypassing.
- **Follow reliable-go:** Reconciliation (if needed for MCP state), structured logs, OCC preference, explicit errs (sentinels), ctx-first, bounded (no fire-and-forget).

**Massively Efficient:**
- **Client reuse:** One long-lived `*es.Client` + `*secrets.OpenBaoClient` per MCP server process lifetime (init at startup like supervisor.go:207 dashboard.New + es.New). http.Transport pools inside (es: MaxIdleConns etc.; tune in New if needed). No per-call connect/re-auth. Dashboard + MCP can share? (if co-process via pkg; otherwise separate is fine — low overhead).
- **Direct ES preferred (queries):** Exact pattern from executors (handlers:428 comment: "direct queries... more reliable than shelling"). Binaries *only* for ingest triggers (flume__trigger_elastro_ingest). Reuses `worker.NewElastroASTQueryExecutor` / Logloom (exported, already used by intake:529) for contract consistency.
- **Result shaping (never blast tokens):** 
  - Default `mode: "summary"` (or implicit): count + top 5-8 compact entries (path, function/signature, short snippet, score). Full _source only on `mode: "full"` + explicit opt-in.
  - Token-aware truncation (like planner:548): hard 2k-4k char cap per tool result unless "full"; paginate beyond.
  - Pagination: `size` (default 10, max 50), `from` or cursor; return `total`, `next_cursor`.
  - Structured compact wire: not raw JSON blobs; human + machine friendly summary strings + optional `hits_json` for small.
- **Caching for hot:** In-memory (sync.Map or tiny LRU in mcp pkg) for (project, query_hash) -> shaped result. TTL 15-60s (or event-driven invalidate on known ingest success). Hot project graph queries (common for breakdown) hit cache.
- **Low-overhead transport:** 
  - **Primary: stdio** for local co-located (`flume start` + local grok in same workspace). Zero net, direct process, env inherit. Matches filesystem/git MCP examples in 07-mcp-servers.md.
  - **Remote: HTTP/SSE** endpoint (add to dashboard server optionally, or dedicated lightweight listener in `flume mcp --http :9876`). Harness speaks native (no extra npx proxy).
- **Binary/sidecar:** No new sidecars; reuse venv bins only for rare ingest. MCP binary is the `flume` cmd itself (subcommand).
- **Budget/measure:** Every call times + emits to LogAgentReasoning (latency_ms, chars_returned, est_tokens). Per-plan budgets respected for start. SSE for streaming large shaped results if transport allows.
- **Other:** Small default sizes, no full raw unless asked, reuse planner RAG shaping logic.

**Coexistence:** Workers keep direct `ToolRegistry` + ReAct (handlers/runner unchanged for contract #4 enforcement inside loop). MCP is the *external harness surface* (read + controlled mutate). Gateway/skills unchanged (different mesh concern).

---

## 3. Tools to Expose (Minimum + High-Value)

All namespaced `flume__<name>` by harness. Schemas returned on `search_tool`; never guess params.

**Core (Elastro/Logloom/Elastic/OpenBao):**
- `flume__elastro_query_ast` (project: str required, query: str, target_path?: str, mode: "summary"|"full" = "summary", size?: int=12): Direct ES on flume-elastro-graph (reuse executor + added strict project filter + shape). Returns compact RAG summary.
- `flume__logloom_ast_query` (project, query, mode, size=15): Same for flume-logloom-* (enrichment primary).
- `flume__elastic_search` (project: required for safety, index: "agent-task-records"|"flume-projects"|"agent-plan-sessions"|..., query: object, size=10, projection?: ["id","title","status",...]): Safe projections + *mandatory* repo filter in query + post-filter. Whitelist indices/fields; no secrets paths.
- `flume__get_git_clone_url` (project: required): Internally: load project, `git.EmbedCredentials` (reuses bao + resolve logic, audit on fail), return ready-to-clone URL (or {url, note: "contains short-lived embedded token; clone immediately; audited"}). Never raw full bao dump. Alternative: `flume__get_git_credentials_for_project` (returns masked + type for advanced use).

**High-value for breakdown/queue/plan (enables interactive PM-like from harness):**
- `flume__list_projects` (filter_status?, limit=100): Safe list from flume-projects.
- `flume__get_queue_snapshot` (project?): Like server.go:104 handleSnapshot but scoped/filtered + compact (counts by status, recent tasks, no 1000 full docs).
- `flume__get_plan_session` (session_id, include_draft=true, include_messages=false): From agent-plan-sessions; loadPlanSession pattern (api_intake:1643).
- `flume__start_plan_new_work` (project, prompt, async=true, max_items_hint?): Starts session (reuse intake logic), triggers planning (RAG-prefetch + LLM via same path). If async: returns {session_id, status: "planning"} immediately (goroutine or fire to dashboard internal). Respects budgets/pause/caps. Emits reasoning. (Blocks only if async=false, up to planner timeout.)
- `flume__query_agent_reasoning` (task_id?, plan_session_id?, limit=50, since?): Search execution_thoughts or via thoughts API; compact.
- `flume__trigger_elastro_ingest` (project): Shells elastro rag ingest + refresh + verify (like api_projects:261); returns {elastro_ok, logloom_ok, counts}. Rare; long timeout. (Also logloom if separate.)
- Bonus: `flume__get_task` (id), `flume__list_tasks` (project, status?, limit), `flume__get_system_health` (es/bao/indices summary).

**Meta:** `flume__search_flume` (for internal discovery if needed).

All accept optional `context: {task_id, plan_session_id, correlation}` for telemetry bridging.

---

## 4. Architecture + Integration

**MCP Server Process:**
- `cmd/flume mcp [--stdio (default) | --http :port] [--config ...]`
- Inits config (internal/config.Load), esClient, baoClient (once).
- If --http: lightweight http.Server + SSE handler (POST /mcp for calls, GET /mcp/sse for events per spec).
- stdio: os.Stdin/Stdout JSON-RPC loop (initialize, tools/list, tools/call, shutdown). Use bufio, strict.
- Reuses pkgs: `internal/es`, `internal/secrets`, `internal/git`, `pkg/types`, `internal/worker` (for New*Executor + consts + find*Binary), `internal/dashboard` (for loadPlanSession/commitPlan/factories — *after* factoring pure funcs in PR2), `internal/logger` (for bridge).
- Optional: small llmClient for planning if triggering full flow without http loopback.
- Graceful shutdown, health (reuse orchestrator patterns).

**Dashboard integration (optional but recommended for remote):**
- Add flag/env `FLUME_MCP_HTTP_ENABLED=true`; on startDashboard, also listen MCP SSE on secondary port or /mcp path (mux subhandler). Shares the *exact* esClient from server (massive reuse/win). Co-process efficiency.

**CLI/Orchestrator:**
- New `commands/mcp.go` (like services.go:34 DashboardCmd).
- Wire `rootCmd.AddCommand(commands.MCPCmd)` in main.go:36.
- `flume doctor` / status can report "MCP: available (run `flume mcp`)" + bin locations.
- Orchestrator health can include "mcp surface ready".

**Config example (project-scoped for local co-located use):**
```toml
# my-flume-project/.grok/config.toml
[mcp_servers.flume]
command = "flume"  # or absolute /usr/local/bin/flume
args = ["mcp", "--stdio"]
# env inherited or explicit for isolation
env = { OPENBAO_TOKEN = "${OPENBAO_TOKEN}", ES_URL = "http://localhost:9200", ... }
enabled = true
startup_timeout_sec = 8
tool_timeout_sec = 60
tool_timeouts = { "flume__start_plan_new_work" = 180, "flume__trigger_elastro_ingest" = 300, "flume__elastro_query_ast" = 30 }
```

Global `~/.grok/config.toml` fallback. For remote dashboard: `url = "http://flume-host:8765/mcp"` + headers bearer.

**Discovery + coexistence:** `grok mcp add flume-local --command flume --args "mcp"`. Tools appear as `flume__*`. Workers unaffected. Harness agents (and fleet via ACP/MCP exposure per subagent designs) now have first-class access.

**Telemetry bridging:** MCP call → if context.task_id or plan_session_id: `LogAgentReasoning(..., "mcp-bridge", "Harness agent invoked flume__X for project Y", {tool, project, ...})`. All results include "reasoning_emitted: true". Visible in UI popouts + Logloom.

**Error mapping:** Go err → MCP {code: -32603 or custom, message, data: {flume_error: "...", transient: bool}}. Harness surfaces cleanly.

---

## 5. Efficiency + Robustness Tactics (Specific + Quantified)

- **Clients:** 1x es + 1x bao per process (cf. per-call in naive curl). http pools: reuse connections (es default transport clone + idle).
- **Shaping example (elastro):** Executor returns raw-ish; MCP wrapper: `if mode=="summary" { extract keys (file, function, signature, path); join 5 + " + N more (use mode=full for raw; truncated to 1800 chars)" }`; `if len > 2500 { truncate + note }`. Same for logloom (structural signatures/callers).
- **Cache sketch:** `type cacheKey struct{project, queryHash string}`; `var hotCache sync.Map`; on miss: query+shape+store (with expiry goroutine or lazy).
- **Pagination + streaming:** Support in tool schema; for SSE transport emit "partial_result" events.
- **Budgets:** start_plan_new_work does `loadPlanSession` + `checkPlanBudget` + atomic script inc (api_intake:1700) before LLM.
- **Timeouts:** ctx, per-tool from MCP config; default conservative.
- **Measurement:** Every path: `start := time.Now(); defer func(){ LogAgentReasoning(..., "duration_ms": time.Since(start).Milliseconds(), "result_chars": len(shaped)) }()`.
- Targets: query tools <500ms p95 (direct ES + cache); planning via MCP same <120s end-to-end.

---

## 6. Before vs After Comparison Table

| Aspect                  | Before (shell curl + manual / no access) | After (first-class MCP tools) |
|-------------------------|------------------------------------------|-------------------------------|
| **RAG / AST (Elastro/Logloom contract)** | Shell elastro (if local + bin discovery pain, see handlers:554 + Dockerfile duplication) or raw ES curl (leak risk, no shaping, 50k tokens possible) or nothing. Planner used server-only prefetch. | `flume__elastro_query_ast(project, query, mode="summary")` + logloom equiv. Direct ES (reuse executor:handlers:529), strict scoping + filter + shape/truncate (like intake:548 2k cap), pagination, cache. Consistent contract for harness agents. |
| **Queue / plan / work visibility** | Manual curl /api/snapshot (server.go:104, full projects + 1000 tasks), /api/intake/session/*, /api/projects. Parse JSON, no schema, poll loops, no audit. | `flume__get_queue_snapshot(project)`, `flume__get_plan_session`, `flume__list_projects`, `flume__query_agent_reasoning`, `flume__get_task`. Compact, scoped, tool schemas via search_tool, integrated with harness TUI/queue. |
| **Plan New Work / breakdowns from interactive** | Manual: describe → curl intake start/message/commit (heavy, no grounding from queue/RAG in same agent loop), or guess via subagents. Explosion risk visible only post-commit. | `flume__list_projects` + `get_queue_snapshot` + `elastro_query_ast` (dynamic RAG for "what modules exist?") → craft grounded prompt → `flume__start_plan_new_work(project, prompt)` (reuses budgets/RAG/intake:744). Poll `get_plan_session`. Enables PM-like decisions *in harness agent* before committing to Flume queue. Directly addresses plan-new-work-queue-reliability + fleet needs. |
| **Git / secrets (scoped)** | Env vars, Settings → reveal (admin only, full keys, api_security:254), manual curl + parse. Cross-project leak possible via bad scripts. | `flume__get_git_clone_url(project)` (internal Embed + resolveToken + bao, per-project only, audit LogAgentReasoning on every call, masked or one-shot url). Never raw bao unless explicitly scoped tool. |
| **Elastic queries (task records etc)** | Raw curl with risk of full docs or wrong index. | `flume__elastic_search(..., repo_filter=project, projection=safe_fields)`. Whitelisted, always filtered + shaped. |
| **Auth / transport / discovery** | Ad-hoc tokens, manual curl, no discovery. | MCP native (stdio for local efficiency, HTTP/SSE remote + harness OAuth). `search_tool` + namespaced `flume__*`. Config in .grok/config.toml (project-scoped). Timeouts per 07-mcp. |
| **Audit / telemetry / degradation** | None or manual logs. Silent failures. | Every call → LogAgentReasoning (mcp-bridge role) + execution_thoughts + Logloom. Clear errors + reasoning on ES/bao down. |
| **Efficiency** | Per-call reconnects, full dumps, no cache, high token blast, shell overhead. | Pooled clients, direct ES, summary+truncate+cache+paginate, stdio zero-net, reuse existing shaping (intake RAG). |
| **Explosion / reliability** | Easy to bypass (manual commit). | All mutates reuse state machine (types:409), budgets (api_intake:1667), caps, Enforce, pause. Idempotent. |

---

## 7. Risks + Mitigations

| Risk | Likelihood/Impact | Mitigation (in design + impl) |
|------|-------------------|-------------------------------|
| **Secret/credential exposure via MCP results or logs** | Med / High (LLM may log/echo) | Strict per-project required arg + authz gate before any bao call. git tool: embed in url + "use immediately + audited" note; result preview truncated in LogAgentReasoning; never full KV dump. Admin reveal remains separate + gated. Local stdio assumes same-user trust. Emit reasoning on *every* cred access. |
| **Concurrent use races with running workers (workspace/git clones, status fights)** | High (known today per context + runner:1398) | MCP *does not* perform clones/writes to workspace (only triggers ingest which is already bg in api_projects:207 + dashboard path). For git url: pure read. start_plan triggers same async clone/ingest as UI. All status via same claim/sweeps/state machine (no bypass). Document "MCP for visibility + planning; execution still via Flume workers". Future: dedicated MCP workspace root opt. |
| **Performance impact on ES (extra queries from harness agents)** | Med / Med (hot paths during breakdown) | Default summary/small size (10-12), mode=summary, cache hot (15-60s), per-tool timeouts + size caps, same pooled client (no extra conns). Queries shaped like planner RAG (never 50k). Monitor via existing telemetry + new mcp_ metrics in reasoning. SSE streaming for large. |
| **Auth complexity / misconfig (cross-project or remote bypass)** | Med / High | Simple local default (stdio + env); explicit for remote (bearer/mTLS). Project gate in every tool (fail closed). Reuse existing resolve + verifyAdmin. Config docs + examples. No secrets in .grok/config (use ${VAR}). |
| **Queue mutation explosion via MCP start_plan** | Low (if impl wrong) / High | *Must* delegate to exact intake paths: budgets, checkPlanBudget, atomic inc, EnforceTransitionOrLog, getSmartMaxLeafTasks, WorkPaused, depth=0, plan_session. No raw index. Shadow mode helps rollout. |
| **MCP protocol / transport bugs or timeouts** | Med / Med | Prefer lib (mark3labs/mcp-go or equiv; add to go.mod in PR6). Strict jsonrpc, ctx propagation, graceful shutdown. Harness timeouts configurable + override per tool. Test with real grok mcp + search/use. |
| **Index drift / missing tags (elastro dynamic) leading to leak or empty** | Med (see verify:659 fallback) | MCP always adds explicit filter + post-filter results by projectID/name in paths/fields. On mismatch: error or empty + reasoning ("data may need re-ingest; used total proxy"). Reuse verify logic. |
| **Maintenance (dupe bin find, query logic)** | Low / Med | Centralize in PR1 (enhance executors), reuse find*Binary. One source for shaping. |
| **Version / discovery skew** | Low | MCP server version matches flume; tools advertise version in list. grok inspect shows. |

All mitigations follow patterns from existing (OCC, Enforce, Log*, budgets, scoping in verify/claim).

---

## 8. Implementation Phases + Topo-Sorted PR Plan

**Overall phases (design-first, reliable rollout):**
1. Design + cross-review (this doc + fleet/plan designs).
2. Core reuse + shaping (no new surface yet).
3. MCP core + queries (read-only safe).
4. Mutate + git + full tools + telemetry.
5. CLI/integration + tests + docs.
6. Hardening (perf, security audit, graceful) + verification.
7. Rollout (shadow, opt-in, monitor explosion metrics via reasoning).

**PR plan (topological; each builds on prior; small + reviewable; cite files):**
- **PR0 (prep, 1 day):** This design doc (written). Add placeholder internal/mcp/. Update docs/designs/ with cross-refs. Verification: greps for all cited lines still match.
- **PR1 (foundations, 2-3 days):** Enhance AST executors for *mandatory* project scoping + compact shaping. Add `project` param enforcement + filter construction (reuse api_projects:637 logic) + `CompactResult(mode, maxChars)` helper. Export or add `New...WithProjectFilter`. Update all callers (handlers:449 Execute, api_intake:529/558 queries, tests). Add/expand worker_test.go cases. Binary find centralize helper if possible. Verification: existing e2e/test_elastro_logloom_contract.py + unit still pass; new scoped tests; no behavior change for internal (target_path still works).
- **PR2 (intake factoring, 1-2 days):** Extract pure reusable funcs from api_intake (e.g. `StartIntakeSession(ctx, es, llm, repo, prompt) (string, error)`, `TriggerPlanningForSession(...)`, `CommitPlan(...)` wrappers that take clients not *Server). Move SessionDoc/PlanningStatus if needed to pkg/types. Update intake handlers to delegate. Enables MCP without http or full server dep. Verification: greps show intake paths unchanged; planner RAG still <120s target.
- **PR3 (MCP core protocol + server, 3 days):** New `internal/mcp/server.go` (stdio loop + http adapter), `protocol.go` (jsonrpc types, initialize/list/call), `config.go` (timeouts, allowed projects). Add dep on mcp lib if chosen (or pure std if minimal). Basic health/ping tool. Verification: `go build`, manual stdio jsonrpc smoke (echo init + list).
- **PR4 (query + list tools, 2 days):** Impl `elastro_query_ast`, `logloom_ast_query`, `elastic_search`, `list_projects`, `get_queue_snapshot`, `get_plan_session`, `query_agent_reasoning` in `internal/mcp/tools_queries.go`. Reuse shaped executors + esClient. Add scoping/authz wrapper. Caching + shaping. Verification: unit tests with mock es; integration against real indices.
- **PR5 (git/ingest/mutate + telemetry, 2-3 days):** `flume__get_git_clone_url` (reuse git.Embed + secrets), `flume__trigger_*_ingest` (bin + run with timeout), `flume__start_plan_new_work` (delegate to PR2 funcs + budget + async option + reasoning). Full LogAgentReasoning bridge on every call (mcp-bridge role, context passthru). Error mapping. Verification: cred resolution tests (reuse git_test.go), budget enforcement, no-leak tests (cross-project queries return 0 or error).
- **PR6 (CLI + integration, 1-2 days):** `cmd/flume/commands/mcp.go` (MCPCmd + supervisor hook), wire main.go + services. Optional dashboard MCP listener. `flume mcp --help`. Example .grok/config.toml in docs/. Verification: `flume mcp` runs, spawns cleanly.
- **PR7 (tests + e2e + docs, 2 days):** Extend tests/e2e/ (new test_mcp_smoke.py or Go using mcp client lib?); script using real harness if available (grok mcp add + search/use in e2e_agent_loop_smoke.sh). Update README, docs/reference/cli.md, operations, walkthroughs. Add to doctor. Verification: full e2e suite (incl contract) green; MCP smoke exercises list + query + start (dry) + cred.
- **PR8 (harden + rollout, 1-2 days):** Perf (cache hit rates, token counts), security (mTLS example, no raw secrets), graceful (bao/es down tests), metrics. Shadow for any new mutates. Update subagent-fleet + workitem designs with "now possible via Flume MCP". Verification: chaos (kill es mid-call), load (repeated hot queries), audit trail in logs/reasoning.

**Dependencies (topo):** PR1 before PR4/5; PR2 before PR5; protocol before tools. Parallel possible: queries + CLI after foundations.

**Total est:** 14-20 dev days (small PRs for review).

---

## 9. Verification Steps (Post-Impl + Design Validation)

1. **Exploration fidelity (this phase):** All key claims re-grepped/ re-read (e.g. `grep -n "NewToolRegistryWithElastro" internal/worker/handlers.go` → :324; exact truncation logic at api_intake:548; indices at handlers:24 + orchestrator/elastic:329; MCP config from ~/.grok/...:23-35 stdio + 81 project-scoped).
2. **Build/run:** `go build ./...`; `flume mcp --help` (after PR6); `flume doctor` mentions MCP surface.
3. **Unit + contract:** Existing `internal/worker/worker_test.go` (ToolRegistry tests), `tests/e2e/test_elastro_logloom_contract.py` (points 1-4: bins, indices, queries, ingest) still 100% green. New MCP unit for shaping/scoping.
4. **MCP smoke (harness integration):** In live grok: `grok mcp add flume-test --command ./flume --args mcp`; `/mcps` shows running; `search_tool` "flume" → discovers 8+ tools with schemas; `use_tool flume__list_projects` (scoped results); `use_tool flume__elastro_query_ast {"project":"known-repo", "query":"auth", "mode":"summary"}` (compact <2k, matches internal executor output shape); cross-project query → filtered/empty + reasoning. `flume__start_plan_new_work` creates visible session in UI/queue without explosion (budget respected).
5. **Robustness:** Kill ES/OpenBao mid-MCP (clear error + LogAgentReasoning with "unavailable"); verify no leak (grep results for other project IDs); timeout a slow query; start_plan on paused project → blocked with audit.
6. **Efficiency:** Hot query cache hit (log or manual); result chars logged < planner 2k; stdio spawn < startup_timeout; reuse client (netstat or log "reusing" if added).
7. **Telemetry:** All MCP calls appear in `/api/logs`, task thoughts (if context), Logloom. `grep "mcp-bridge" workspace/logs` or ES.
8. **e2e full:** `tests/e2e/test_*.py` + `scripts/e2e_agent_loop_smoke.sh` + new MCP path. Real repo onboarding + plan via MCP.
9. **Security/audit:** No raw secrets in any result; every cred/bao access has LogAgentReasoning entry; admin paths unchanged.
10. **Docs + designs:** This doc + updates to cli.md + fleet designs. Comparison table validated vs code.
11. **Perf targets:** Planner-equivalent via MCP still <120s; query p95 measured in reasoning.

Post-merge: monitor via existing 250s plan monitor + new "mcp tool calls" in telemetry.

---

## 10. Proposed Files / Changes (Summary)

**New:**
- `docs/designs/flume-mcp-integration.md` (this; the artifact)
- `internal/mcp/server.go` (core, stdio/HTTP, dispatch, clients, cache, authz, telemetry bridge)
- `internal/mcp/protocol.go` (MCP types, jsonrpc, schemas)
- `internal/mcp/tools_queries.go`, `tools_mutate.go`, `tools_git.go` (impls)
- `internal/mcp/shaping.go` (compact, truncate, paginate, filter helpers)
- `cmd/flume/commands/mcp.go` (MCPCmd)
- `docs/walkthroughs/mcp-integration.md` or `.grok/config.toml` example (supporting)

**Modify (targeted, minimal):**
- `cmd/flume/main.go` (add mcp cmd)
- `internal/worker/handlers.go` (PR1: scoping + shape in executors; export helpers if needed; ~20 lines)
- `internal/dashboard/api_intake.go` (PR2: extract pure funcs; callers delegate; reuse in MCP)
- `internal/dashboard/server.go` (optional: MCP http mount + flag)
- `pkg/types/types.go` (if SessionDoc etc promoted for sharing)
- `cmd/flume/commands/doctor.go`, `services/supervisor.go` (minor integration)
- `tests/e2e/*`, `internal/worker/worker_test.go` (new tests)
- `README.md`, `docs/reference/cli.md`, `docs/reference/architecture.md`, docker-compose (optional MCP port), related designs (cross-refs)
- `go.mod` (mcp lib dep if chosen)

**No change (by design):** worker ReAct paths, gateway/skills, core state machine/claim/sweeps (reuse), ingest paths (MCP triggers them).

---

## 11. Open Questions / Future (Post v1)
- Native MCP lib choice (mark3labs/mcp-go vs pure stdio impl for zero-dep).
- Streaming thoughts from MCP-initiated plans back to harness (via SSE or polling get_plan).
- Full workspace isolation for MCP-triggered clones (vs shared racy).
- Metrics counters (flume_mcp_tool_calls_total{tool,project}).
- ACP native in harness for Flume fleet tools (per subagent design).
- mTLS + per-project OAuth for multi-tenant remote MCP.

This is a production-grade, thoroughly explored design following all specified constraints and patterns. Implementation will be safe, efficient, and unifying for harness + Flume.

**End of design document.** (Verification of this doc itself: all internal citations match live reads/greps performed 2026-06-03; external MCP guide read in full; todos tracked throughout.)