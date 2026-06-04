# Flume MCP Server: Detailed Technical Implementation Plan

**Date:** 2026-06-03  
**Status:** Ready for execution (post deep research)  
**Author:** Grok (based on prior design doc + fresh research)  
**Goal:** Add a lightweight, efficient, secure MCP server to the Flume stack. This exposes Flume primitives (elastro RAG, logloom structural queries, ES task/plan data, scoped OpenBao/git creds, Plan New Work triggers, queue snapshots) as first-class tools to Grok harness agents (interactive sessions, subagents, fleet). Primary outcome: **measurably improved code quality** via better-grounded work breakdown (Plan New Work / PM decomp), dynamic codebase context during impl/review, queue awareness to avoid duplication/conflicts, and auditable flows.

**Deep Research Summary (performed before any planning):**
- **MCP Protocol (from spec + guides + web research):** JSON-RPC 2.0 (stateful sessions). Core: `initialize` (capabilities negotiation: tools/resources/prompts), `tools/list`, `tools/call` (with progress if streaming). Transports: **stdio** (newline-delimited JSON over stdin/stdout; lowest latency, ideal for local subprocesses spawned by harness; inherits process security), Streamable HTTP + SSE (for remote; POST for requests, SSE for server->client events/notifications). Servers implement handlers; clients (harness) use `search_tool`/`use_tool` (namespaced `flume__tool`). Config via `.grok/config.toml` (project-scoped highest priority). Security: local stdio trusted (same-user); remote uses OAuth/bearer (harness handles). No built-in auth in protocol — server must enforce. Timeouts per-tool configurable in harness.
- **Go MCP Implementations (research via web_search + go list):** 
  - `github.com/mark3labs/mcp-go` (mature, v0.54+; 400+ dependents; supports stdio, SSE, Streamable HTTP out-of-box; high-level `server.NewMCPServer`, `tool.NewTool` with schema (Go structs or manual), `AddTool` with typed handlers; built-in validation, sessions, logging hooks. Low boilerplate. Adds one dep but proven for production MCP servers. Examples show clean `stdio` + HTTP servers.
  - Pure/manual: Feasible for "max lightweight" (bufio.Scanner on os.Stdin for lines, json.Unmarshal to jsonrpc.Request, dispatch switch on method, write to os.Stdout). Matches "Understanding MCP Through Raw STDIO" articles; minimal deps (only stdlib + existing Flume net/http). Tradeoff: more code for init/handshake, schema validation, error codes, SSE framing.
  - Official SDK emerging (modelcontextprotocol/go-sdk) but community `mcp-go` is the practical choice today (aligns with spec). For Flume (already minimal-deps, direct net/http for ES): Recommend **mcp-go for robustness** (auto schema from structs reduces bugs, transport abstraction, error handling); fallback pure stdio sketch for zero-new-dep. Security: handlers must validate inputs; no eval.
- **Flume Framework (exhaustive tool exploration):** 
  - Tool exposure today is *internal only* (ReAct in workers): `internal/worker/handlers.go:283` (ToolExecutor iface + Registry), `NewToolRegistryWithElastro:324` (FS + elastro + logloom), `ElastroASTQueryExecutor:418` (direct `es.Search` on `flume-elastro-graph` with multi_match + wildcard target_path; no bin for queries; LogAgentReasoning + truncation; 8-12 hits), similar for Logloom:626 (enrichment primary), binary gates (`findElastroBinary` etc for ingest only; os.Exit on missing in worker start), FS safety prefix under repoPath.
  - Clients: `internal/es/client.go:30` (lightweight HTTP wrapper, New with 30s timeout + InsecureSkip for self-signed, do() with ApiKey/Basic, GetDoc/Search/Count/UpdateDocOCC/UpdateDocWithInlineScript; pooled http.Transport).
  - Secrets: `internal/secrets/openbao.go:28` (New 10s client, KVGet/Put), `internal/git/credentials.go:70` (EmbedCredentials + resolveToken strict contract: ES metadata only, real in bao "flume/keys"; env fallback; used in EnsureTaskBranch:1423).
  - Planning/queue: `internal/dashboard/api_intake.go:469` (fetchPlannerRAGContext reuses executors + 8s timeout + inject + truncate 2k like planners), `loadPlanSession:1643`, `checkPlanBudget:1667`, `atomicIncrementPlanCounters:1679` (Painless script), `commitPlan:1707` (smart caps via complexity, build hierarchy, budgets), `buildLLMMessages:415`. SessionDoc/PlanResponse/AgentTaskRecord structs. Routes in server.go:148.
  - Wiring: `cmd/flume/main.go:24` (cobra subcmds), `commands/services.go:34` (DashboardCmd etc), `services/supervisor.go:163` (StartDashboardOnly -> dashboard.New + ListenAndServe; esClient created inside), worker manager standalone, `flumelogger.SetESBridge` + `LogAgentReasoning:363` (slog + appendExecutionThoughtNonBlocking Painless to thoughts[] + ES).
  - Other: `pkg/types`, `internal/config:109` (poll 2s etc), `Dockerfile` (elastro/logloom bins in venv), docker-compose (replicas, no shared workspace vol -> private FS per container), `cmd/flume/orchestrator/elastic.go` (indices bootstrap).
  - Current external access: raw curl to dashboard (/api/intake/*, /api/snapshot) or direct ES — no schemas, no shaping, leak risks, no audit, manual for harness agents.
- **Lightweight/Efficient/Secure Constraints for Flume:** Reuse *existing* long-lived clients (es, bao) + executors (no duplication). stdio primary (zero net overhead, matches harness local use + "flume start" + grok co-located). Direct ES (no repeated bins except ingest). Result shaping/truncation/caching/pagination (prevent token explosions in harness context windows; <2k like planner RAG). Per-project strict scoping + filters + post-filter (reuse api_projects verify logic) + audit (every call -> LogAgentReasoning "mcp-bridge" with task/plan context for UI/Logloom/quality tracing). Auth: stdio inherits env (trusted local); HTTP bearer (harness OAuth). No raw secrets (embed only for git; masked). Idempotent mutates delegate to intake (reuse budgets, Enforce, caps, WorkPaused). Graceful (clear errors + reasoning on down). Minimal new deps (mcp-go or pure). Coexistence: workers unchanged (internal ToolRegistry stays for contract #4). Improve code quality by giving *harness agents* live Flume context (dynamic RAG during interactive planning/impl, queue visibility for non-conflicting decomp, grounded start_plan, scoped git) vs. stale prompts or blind subagents.
- **Code Quality Link (from context + designs):** Current pain: one-shot planner (static RAG prefetch), PM decomp with minimal context, explosion risks, poor visibility leading to dup work or bad hierarchies. MCP gives harness models (the ones doing creative breakdown before/around queue) tool access to same RAG + state machine + budgets that internal workers use. Outcome: higher-signal plans (models query "what modules exist here?" live), fewer explosions (inspect current_items/queue before commit), better impl (elastro in harness subagents), auditable quality loops (reasoning from MCP calls in thoughts), reliable git in agent flows.

This plan is **technical and precise**: phase-by-phase, exact files/functions/lines to touch, code sketches (copy-paste ready with context), order of edits (to keep buildable), testing for quality metrics (e.g., plan leaf counts, reasoning audit coverage), rollback. It refines the prior design into executable steps. Total ~15-22 dev days in 8 small PRs.

## 1. Architecture Decisions (Research-Driven, Lightweight/Efficient/Secure)

- **Transport:** stdio primary (lightweight: no HTTP server unless opted; harness spawns `flume mcp --stdio` as subprocess; newline JSON-RPC; low latency, process isolation). Optional Streamable HTTP/SSE in dashboard (for remote; share esClient for efficiency). Pure Go (no external process beyond the flume binary itself).
- **MCP Lib:** Add `github.com/mark3labs/mcp-go` (mature, handles handshake/schemas/transports/errors/sessions; reduces boilerplate/security bugs vs pure). For max lightweight alternative: pure stdio impl (stdlib only; ~150 LOC handler). Plan uses mcp-go (justified by research; one dep, high ROI for correctness).
- **Server Structure:** New `internal/mcp/` package (server.go for wiring + main loop, protocol.go for types if needed, tools_*.go for handlers, shaping.go for efficiency). Stateless per-call (reuse global clients). Init once: es + bao + logger bridge.
- **Scoping/Auth (Security Core):** Every tool **requires** `project` (or `repo`) string. Handler: (1) load/validate from flume-projects (fail closed), (2) inject ES term filter on `repo`/`project_id` etc., (3) execute, (4) post-filter results by project in paths/fields (for untagged indices like elastro). For git: delegate to `git.EmbedCredentials` (never return raw bao secret). stdio: env-trusted. HTTP: require bearer (or mTLS later). All calls emit `LogAgentReasoning(..., "mcp-bridge", ...)` with correlation for audit/quality tracing. No admin/secrets reveal paths.
- **Efficiency:** 
  - Singleton clients (like dashboard.New:131 creates es once).
  - Reuse/enhance executors (direct ES, no bin for queries).
  - Shaping: default summary (top N compact + count + note); hard truncate (e.g. 1800 chars) + `mode=full` opt-in; pagination.
  - Cache: sync.Map hot (project+queryHash, 30s TTL).
  - Small defaults (size=8-12); ctx timeouts from harness config + overrides.
  - stdio: bufio + direct dispatch (or lib).
- **Telemetry/Quality:** Bridge to existing `flumelogger.LogAgentReasoning` + execution_thoughts (visible in UI drawers, Logloom). Include `task_id`/`plan_session_id` from tool context if passed. Enables post-hoc analysis of "MCP-assisted plans had 40% fewer leaves / higher success".
- **Coexistence/Compatibility:** No changes to worker ReAct paths, claim/sweeps/state machine, gateway/skills. `flume mcp` is new standalone (like `flume dashboard`). Ingest triggers reuse existing (api_projects paths). Mutates (start_plan) delegate exactly (reuse budgets/caps/Enforce to prevent quality regressions like explosion).
- **Error Model:** Map Go errs to JSON-RPC (codes -32602 invalid params, -32603 internal with `flume_error` + `transient: bool`). Always return usable partial + reasoning. Graceful on ES/bao down.
- **Config:** Harness side (`.grok/config.toml` stdio example using `flume mcp --stdio`; env expansion for tokens). Flume side: reuse `internal/config` + env (ES_URL, OPENBAO_*, etc.). Per-tool timeouts in harness (e.g. start_plan=180s).
- **Lightweight:** Avoid new heavy deps beyond mcp-go (or none). Reuse net/http ES client, no full server for stdio mode. Binary size impact minimal (Flume already has cobra/http).

This produces a server < few hundred LOC new (mostly thin wrappers + shaping), reusing 90%+ of existing robust code.

## 2. High-Level Phases (Topo-Sorted, Small PRs)

Follow prior design's 8 PRs but with **precise technical steps, code, and quality gates**. Each PR must build (`go build ./...`), pass existing tests (`go test ./internal/worker/... ./tests/e2e/... -run 'Elastro|Logloom|Contract'`), and add targeted tests.

**PR0 (Prep + Research Validation, 0.5d):** Land this plan doc. Create `internal/mcp/` placeholder. Cross-ref in existing designs/README. Verify all citations from research still match (greps).

**PR1 (Foundations: Scoping + Shaping + Reuse, 2-3d):** Enhance executors + add pure helpers. Centralize for MCP + internal. This is the quality enabler (better RAG shaping for agents).

**PR2 (Intake Factoring for Delegation, 1-2d):** Extract pure funcs so MCP can trigger plans without full *Server.

**PR3 (MCP Core Server + Protocol, 2-3d):** Implement the server (lib or pure). Basic tools skeleton + health.

**PR4 (Read-Only Query Tools, 2d):** elastro/logloom/elastic/list/get_* (safe, scoped, shaped, cached).

**PR5 (Git + Mutate + Telemetry, 2-3d):** get_git_clone_url, trigger ingest, start_plan_new_work (full reuse + budgets + audit). Bridge every call.

**PR6 (CLI + Wiring + Co-Host, 1d):** New subcmd, main.go, supervisor (optional dashboard mount for HTTP). Doctor/status updates.

**PR7 (Tests, E2E, Docs, Quality Metrics, 2d):** Unit (mock ES), harness smoke (if possible), e2e contract extension, update docs. Add quality proxies (e.g. test that MCP-assisted "plan" sessions have lower leaf counts in mock).

**PR8 (Harden + Rollout, 1-2d):** Perf (cache, p95), security (fuzz inputs, cross-project tests, no-leak), graceful, metrics in reasoning. Shadow. Update fleet designs.

**Dependencies:** PR1 before 4/5; PR2 before 5; core before tools. Parallel: queries + CLI after foundations. Add mcp-go in PR3.

**Total:** 12-18 dev days.

## 3. Precise File-by-File Changes + Code Sketches

**New files (internal/mcp/):**
- `internal/mcp/server.go`: Main entry. Init clients once. For stdio: use mcp-go `server.NewStdioServerTransport()` + `s.ServeStdio()`. Register tools. Telemetry wrapper.
- `internal/mcp/tools.go` (or split queries/mutate/git): Handler funcs.
- `internal/mcp/shaping.go`: `ShapeElastroResult(raw string, mode string, maxChars int) string`, similar for others, with project post-filter.
- `internal/mcp/config.go`: MCP-specific (timeouts map, allowed projects?).

**Modifications (minimal, surgical):**

### 3.1 PR1: handlers.go (enhance for project scoping + shaping; ~30 LOC net)
Current executors take `repoPath` (used as target). Add `project` enforcement.

```go
// After line 448 (Elastro...)
func (e *ElastroASTQueryExecutor) Execute(ctx context.Context, args map[string]interface{}, repoPath string) (string, error) {
    project, _ := args["project"].(string)  // NEW: required for MCP + internal future
    if project == "" {
        project = inferProjectFromPath(repoPath) // best-effort fallback
    }
    if project == "" {
        return "", fmt.Errorf("elastro_query_ast: 'project' (or repo) is required for scoping")
    }
    // ... existing query build ...
    // Inject filter (reuse from api_projects.go:637 verify style)
    if target := ...; target != "" {
        // existing wildcard
    }
    // Add project filter (elastro docs often have "repo" or "project_id" or path prefix)
    esQuery["query"].(map... )["bool"].(map...)["filter"] = []interface{}{
        map[string]interface{}{"term": map[string]string{"repo": project}}, // or "project_id"
    }

    hits, err := ...
    shaped := shapeElastroHits(hits, "summary", 1800, project) // NEW shaping + post-filter
    flumelogger.LogAgentReasoning(...) // existing
    return shaped, nil
}

// Add at bottom of file (new helper, used by MCP + future)
func shapeElastroHits(hits []json.RawMessage, mode string, maxChars int, project string) string {
    // Compact: extract file/function/signature/path + score. Post-filter paths containing project.
    // Truncate. Return "N hits for project X (use mode=full): summary..."
    // ~40 LOC. Mirror planner truncation.
}
```

Similar for LogloomASTQueryExecutor:650 (add project filter on enrichment docs).

Centralize binary find if dupe (handlers + api_projects).

Update tests: add project param cases.

**Quality impact:** Ensures all RAG (internal or via MCP) is scoped → no cross-project pollution in plans/impls.

### 3.2 PR1/PR2: api_intake.go (factor pure funcs; add project to RAG paths if needed)
Extract (non-method versions for reuse in MCP without *Server):

```go
// NEW pure funcs (around 1643)
func LoadPlanSessionPure(es *es.Client, sessionID string) (SessionDoc, error) { ... copy body of load... using es.GetDoc ... }

func CheckPlanBudgetPure(sess SessionDoc, proposed int) (bool, string) { return checkPlanBudget(sess, proposed) } // already pure

func AtomicIncrementPure(es *es.Client, sessionID string, itemDelta, tokenDelta int) error { ... inline script via es ... }

func CommitPlanPure(es *es.Client, llmClient *llm.Client, repo string, planDict map[string]interface{}, planSessionID string, logger *slog.Logger) ([]AgentTaskRecord, error) {
    // Extract body of commitPlan, take clients not s. Use Load/Check/Atomic inside.
    // For RAG in planning? Delegate or reuse fetchPlannerRAGContext logic (but that is on Server; make pure variant taking es).
}
```

Also make `buildLLMMessages` pure or export helpers.

In existing methods: delegate to pures (minimal change).

**For start_plan:** MCP will call a new thin wrapper that does session create + goroutine planning (reuse runInitialPlanning logic factored).

Update `fetchPlannerRAGContext` callers or make a `FetchRAGPure(es *es.Client, repo, prompt string) string`.

### 3.3 PR3+: internal/mcp/server.go (core)
```go
package mcp

import (
    "context"
    "log/slog"
    "os"

    "github.com/mark3labs/mcp-go/mcp"  // or server
    "github.com/mark3labs/mcp-go/server"
    "github.com/Fremen-Labs/flume/internal/es"
    "github.com/Fremen-Labs/flume/internal/secrets"
    flumelogger "github.com/Fremen-Labs/flume/internal/logger"
    // ...
)

func NewMCPServer(es *es.Client, bao *secrets.OpenBaoClient, logger *slog.Logger) *server.MCPServer {
    s := server.NewMCPServer("flume", "3.0.0", server.WithToolCapabilities(true)) // etc.

    // Register all
    s.AddTool( /* tool def with schema */ , handleElastro(es, logger) )
    // ...

    flumelogger.SetESBridge(es) // ensure bridge if not global
    return s
}

func RunStdio(ctx context.Context, es *es.Client, bao *secrets.OpenBaoClient, logger *slog.Logger) error {
    srv := NewMCPServer(es, bao, logger)
    // stdio transport
    stdioTransport := server.NewStdioTransport() // per lib
    return srv.ServeStdio(ctx, stdioTransport) // or equivalent
}

// For HTTP (in dashboard or standalone)
func RunHTTP(...) { /* SSE handler */ }
```

**Tool handler sketch (tools.go):**
```go
func handleElastro(es *es.Client, logger *slog.Logger) server.ToolHandler {
    return func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
        args := req.Params.Arguments  // map
        project := mustString(args["project"])
        // authz/scoping already in executor or wrapper
        result, err := /* call enhanced executor or direct */ 
        if err != nil { return mcp.NewToolResultError(err.Error()), nil }
        // Always audit
        flumelogger.LogAgentReasoning(ctx, getTaskID(ctx), "mcp-bridge", "flume__elastro_query_ast", map... )
        return mcp.NewToolResultText(result), nil
    }
}
```

Use lib's schema helpers for `project` required string etc.

### 3.4 Git tool (PR5):
```go
// In tools_git.go
func handleGetGitCloneURL(gitEmbed func(ctx, url string) string, es *es.Client /* for project load */) ... {
    proj := loadProjectScoped(es, project)
    if proj.RepoURL == "" { ... }
    cloneURL := git.EmbedCredentials(ctx, proj.RepoURL, "")
    // Log every access
    flumelogger.LogAgentReasoning(ctx, ..., "mcp cred access for project", map{"project": project, "note": "embedded short-lived"})
    return mcp.NewToolResultText(cloneURL), nil
}
```

### 3.5 start_plan_new_work (PR5, delegates):
```go
func handleStartPlan(es *es.Client, llm *llm.Client, logger *slog.Logger) ... {
    project, prompt := ...
    sessID, err := StartIntakeSessionPure(...) // from PR2
    if err... 
    go TriggerPlanningPure(...) // async by default
    // or if sync: wait with timeout (respect FLUME_PLANNER...)
    flumelogger.LogAgentReasoning(..., "mcp start_plan", ...)
    return result with sessID
}
```

### 3.6 CLI + Wiring (PR6)
- New `cmd/flume/commands/mcp.go`:
```go
var MCPCmd = &cobra.Command{
    Use: "mcp",
    RunE: func(cmd *cobra.Command, args []string) error {
        ctx := cmd.Context()
        logger := ...
        cfg := config.Load(...)
        es := es.New(cfg.ESURL, ...)
        bao := secrets.NewOpenBaoClient(...)
        mode := "stdio" // flag
        if mode == "stdio" {
            return mcp.RunStdio(ctx, es, bao, logger)
        }
        // http...
    },
}
```
- main.go:42 `rootCmd.AddCommand(commands.MCPCmd)`
- supervisor.go: add StartMCPOnly if needed; optional in StartDashboard: if env { go mcp.RunHTTP(...) }
- doctor.go: add check for mcp binary/paths + "MCP surface: flume mcp available".

### 3.7 Shaping + Cache (internal/mcp/shaping.go + server)
```go
var hotCache sync.Map // key: project+hash(query)

func shapedQuery(...) {
    key := ...
    if cached, ok := hotCache.Load(key); ok && !expired { return cached.(string) }
    raw := executor.Execute(...) // or es
    shaped := shape(raw, mode, 1800, project) // post-filter + compact + truncate
    hotCache.Store(key, shaped)
    return shaped
}
```

### 3.8 Telemetry (everywhere in mcp handlers)
Always:
```go
flumelogger.LogAgentReasoning(ctx, taskIDFromCtx(ctx), "mcp-bridge", fmt.Sprintf("invoked %s for %s", tool, project), map[string]any{
    "tool": tool, "project": project, "duration_ms": ..., "result_preview": truncate(result, 200), "args": preview(args),
})
```

### 3.9 Other (small)
- `pkg/types`: promote SessionDoc etc if not already exported enough (or keep internal copies for mcp).
- `internal/dashboard/server.go`: optional mux.Handle("/mcp", mcpHandler) behind flag.
- Dockerfile/docker-compose: expose optional MCP port if HTTP; ensure bins in PATH for ingest tools.
- go.mod: `go get github.com/mark3labs/mcp-go` (in PR3).

## 4. Security Model (Precise)
- **Input:** All tools validate `project` (load from ES or error). No trust in caller beyond transport.
- **Output:** Mandatory filters + post-filter (e.g. only return hits where path contains project or doc.repo == project). For lists: scope queries.
- **Secrets:** `get_git_clone_url` only; result is embed url (token short-lived by design) + note. Log every access. Never KV dump.
- **Transport:** stdio = same UID/process trust (Flume already assumes for workers). HTTP: enforce token (reuse FLUME_ADMIN or per-project; harness OAuth for remote).
- **Audit:** 100% calls -> LogAgentReasoning (immutable in ES + thoughts). Enable "mcp-bridge" filter in UI.
- **Resource:** No direct FS write (except via start_plan -> existing safe ingest). No arbitrary ES write.
- **DoS:** Size caps, timeouts, cache, small defaults. Rate via harness or dashboard.
- **Tests:** Cross-project query returns empty/error + reasoning; cred access logged but no secret in result.

## 5. Efficiency & Lightweight Measures
- Clients: init once in main MCP entry (passed down).
- Queries: direct ES (handlers pattern); no `exec elastro` for read.
- Results: shaping + truncate + summary default + cache + pagination (lib supports streaming progress).
- stdio: direct, no net/alloc overhead.
- Startup: fast (reuse config load); harness startup_timeout_sec=8-10.
- Memory: small cache + existing pools.
- Measure: every handler times + logs duration/chars (visible in reasoning).

## 6. Code Quality Outcomes & Measurement
- **How it improves:** 
  - Dynamic/live RAG in harness planning subagents or interactive → more accurate minimal hierarchies (models explore AST before emitting JSON plan) → fewer post-commit explosions.
  - Queue snapshot + plan session visibility → agents can "see" in-flight work, existing similar tasks, budgets → better decomp decisions, less dup.
  - start_plan from agent (with prior RAG calls in transcript) → grounded intake (vs blind prompt).
  - Scoped git creds in agent flows → reliable clone/push in harness-driven tasks without env hacks.
  - Full audit (MCP calls in thoughts + UI) → quality reviews can trace "why this plan?".
- **Proxies in verification:** In e2e/MCP smoke: trigger plans via MCP vs manual; assert lower avg leaf count / higher "success without refine" rate; count "mcp-bridge" reasoning entries per task; measure token usage in RAG calls (shaped < raw); no cross-project leaks in results.
- **Long-term:** Fleet/subagents use Flume tools for reliable parallel quality work (per prior designs).

## 7. Testing Strategy (Quality-Focused)
- Unit: mock es in mcp handlers (scoping, shaping, errors, cache).
- Integration: against real ES (use existing test indices); test pure factored funcs.
- Harness smoke: `grok mcp add flume --command ./flume --args mcp`; search/use for elastro (verify shaped + scoped), list, start_plan (creates session visible in /api + queue, budget respected, reasoning emitted).
- E2E: extend `test_elastro_logloom_contract.py` + new `test_mcp_flume.py` (or Go); real repo + MCP-driven plan + impl cycle; assert quality (e.g. plan commits without explosion, implementer uses similar RAG).
- Concurrency/Security: parallel MCP calls + workers on same project; cross-project attempts; bad creds; down ES (graceful).
- Perf: hot cache hit; p95 < targets.
- Race: `go test -race` on mcp pkg + worker.
- Manual: `flume mcp --stdio` (manual jsonrpc or via grok); docker `flume mcp`; rflow project via MCP tools.
- Regression: all existing e2e/contract + worker tests 100% green.

## 8. Rollout & Ops
- Opt-in: `flume mcp` explicit; dashboard MCP HTTP behind `FLUME_MCP_HTTP=1`.
- Doctor: reports MCP readiness + bins.
- Monitoring: new reasoning events; existing plan monitors + "mcp tool calls".
- Deprecate: none (additive).
- Backcompat: executors unchanged for internal; MCP adds project param optionally.

## 9. Risks & Mitigations (from research + design)
- Dep bloat: use mcp-go (small); or pure.
- Protocol drift: pin version; test with real harness `grok mcp`.
- Races (workspace): MCP doesn't own clones (triggers existing safe paths).
- Token bloat: shaping mandatory.
- See full risks table in prior design.

## 10. Verification Checklist (End-to-End)
1. `go build ./... && go test ./...` (targeted).
2. `flume mcp --help`; standalone run + stdio handshake (init/list/call smoke via echo or simple client).
3. Harness: add via `grok mcp add`, discover, call queries (compact results, scoped), start_plan (visible in dashboard/queue, audit in reasoning).
4. Quality: MCP plan sessions show richer reasoning + successful minimal decomp vs baseline.
5. Security: no leaks, all calls audited, down services graceful.
6. E2E full suite green + new MCP contract test.
7. Perf/chaos as above.
8. Docs updated (cli.md, this plan as reference, example .grok/config).

This plan is executable, cites exact prior research/lines, keeps Flume lightweight (reuse-first), secure-by-construction (scoping everywhere), and directly targets code quality via agent empowerment. Execute PR-by-PR with reviews. After PR1+2, the surface can be added safely.

**Next:** User approval -> spawn implementer subagent or direct edits via search_replace for PR1.

(End of plan. Written after full research phase; all todos completed.)