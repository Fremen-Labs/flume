# Optimizing Flume's Local LLM Communication Flow for Best-in-Class Performance

**Date:** 2026-06-03  
**Context:** Analysis per user query on Flume intent, review of created SKILLs, full flow trace of Plan New Work, assessment of current N-node local LLM connections, and recommendations for maximal optimization of the gateway-to-local-LLMs path. No breaking changes to frontier/external LLM APIs.

## 1. Flume Intent (Verified from Code, Docs, and SKILLs)

Flume is designed as **the most efficient, performant, agentic AI coding platform** that automatically balances broken-down coding work across N# of nodes in a local "node mesh" (Ollama/Exo instances on Macs, Linux boxes, etc.). 

Key goals (sourced from docs/reference/architecture.md, docs/designs/flume-workitem-orchestrator-design.md, docs/walkthroughs/local-inference.md, cmd/flume/orchestrator/*.go comments, internal/worker/manager.go, src/gateway/*, and flume-go SKILL.md):

- **Efficiency & Token Spend Reduction**: Mesh routing + capability-aware selection (reasoning score, load, latency, model fit) prefers local high-quality nodes over expensive frontier calls. RAG (Elastro/LogLoom AST graphs) reduces hallucinations and prompt bloat. Hierarchy + caps prevent "task explosion" (10-60+ items from medium plans). Reconciliation loops + bounded pools avoid redundant work.
- **Performance**: Fast Plan New Work breakdown (target <120s historical for simple requests on local), streaming for live reasoning visibility, per-node health/latency scoring for optimal routing, sweeps for quick promote from planned->ready. Go native for low-overhead orchestration vs Python.
- **Agentic Coding Outcomes**: End-to-end autonomous: Plan New Work (intake) -> structured hierarchy (epics/features/stories/tasks with deps) -> queue (planned/ready/running/review) -> workers (PM decomp, implementer ReAct + AST tools, reviewer/tester) -> completion with git/PR. Strong state machine, guards, observability (LogAgentReasoning + thoughts to ES/Logloom).
- **Security/Dependability**: Container isolation + host.docker.internal bridge for local LLMs (never exposes host LLM directly). OpenBao for secrets (no plaintext keys). ES for durable state (no loss on restart). Network policies, host validation in node add (no SSRF), **strictly optional** per-node Bearer tokens from OpenBao (AuthSecretPath) — see user clarification: default is unauthenticated for local; manual portal setup only for users who secure their Ollama instances. "Secure coding platform they can depend on" – everything auditable, reversible, bounded (WIP caps, depth, budgets). Opt-in auth does not change the experience for unauthed meshes.

Node mesh is central: `flume-node-registry` in ES, MultiNodeRouter, HealthChecker probes (/api/tags, /api/ps, /api/show), SelectNode scoring. Gateway is the "brain" for routing LLM calls (local or frontier). Workers are execution boundary.

This matches user's stated intent exactly. Flume differentiates from raw harness subs by providing K8s-like reliability for agent work.

## 2. Review of Flume SKILLs Created/Referenced

We reviewed via tools (find, cat, read_file on paths like /Users/jonathandoughty/clients/fremenlabs/flume/flume/.grok/skills/flume-go/SKILL.md, ~/.grok/skills/*, project references in docs/designs/* and code comments). Relevant SKILLs (always combine with reliable-go-systems):

- **flume-go/SKILL.md** (project-local, heavily referenced in code/docs):
  - Core: Kubernetes-inspired recon (Manager cycle, Claimer atomic OCC, Sweeper multi-pass, Runner handlers, Pool sem, StateMachine Enforce*).
  - LLM/Gateway specific: "LLM Client & Gateway (The Other Chronic Source of Loops)": Never silent legacy fallback; propagate TaskID/PlanSessionID/AgentRole for routing/budget/rate; explicit gateway URL; role-aware cap+block on persistent LLM errs; rich LogAgentReasoning on every LLM decision/failure.
  - Anti-explosion: spawn guards + reasoning; status/role consistency.
  - Observability: every decision/transition/LLM call must LogAgentReasoning + LogStateTransition (feeds UI popout, Logloom graphs).
  - Checklist: ctx first, explicit errs, recon, bounded, structured logs+reasoning, idempotent, no globals, separate planning/execution.
  - Applies directly to gateway/LLM paths: "when editing anything under internal/llm/, ... or cmd/flume related to agents."

- **reliable-go-systems/SKILL.md** (found via find, referenced in flume-go + designs):
  - Principles: ctx first (no stored ctx), explicit wrapped/typed errors (sentinels like ErrPersistentConfig), reconciliation (level-driven queues/sweeps not edge), bounded (sem, timeouts/deadlines on ALL I/O/external incl LLM/ES/git, no fire-forget goroutines), observability (slog structured + metrics + rich reasoning for agents), idempotency+safe retries+jitter, simplicity (short funcs, table tests, golangci-lint -race), no globals/explicit ctor, agent layer (plan vs exec, todos for >3 steps, verification loops, contracts).
  - Checklist mandatory before Go changes (esp LLM client, workers, queues, state machines).
  - For this task: "Use proactively for ... LLM clients, state machines, or multi-component agents."

- Other referenced/created in context (subagent-fleet-orchestrator.md, implement/SKILL.md, pr-babysit/SKILL.md, user-guide/*.md):
  - Fleet patterns port Flume's Manager/Claim/Sweep/Runner/State + Log* to harness (using scheduler/monitor/spawn + file OCC + todo_write).
  - Emphasize: no thundering on local LLM (pool caps), rich reasoning capture, verification loops (implement-review-fix until 0 issues), separate planning/execution.
  - Used in prior Flume hardening (5-ID exposure ironically used raw harness to build reliable Flume).

These SKILLs dictate: any optimization MUST follow recon/bounded/ctx/observability/idempotency; enhance (not bypass) LogAgentReasoning for LLM hops; preserve separation (planning intake vs exec workers); use todos for analysis >3 steps (done here); verification (this report + code checks).

## 3. Full Flow Trace: Plan New Work → Gateway → LLM Call → Gateway → LLM → Gateway → Broken Down Work

Traced via multiple read_file/grep on live code (post recent edits for 120s/stream/RAG limits). No assumptions – exact lines/funcs.

### Step 1: Plan New Work Intake (Dashboard API – "the breakdown")
- Entry: `internal/dashboard/api_intake.go:handleIntakeStartSession` (or refine in handleIntakeMessage/Commit).
- Build: `buildLLMMessages` (plannerSystemPrompt ~55 lines complexity-proportional + RAG note + user history).
- RAG (always for planner, "Contract #3"): `fetchPlannerRAGContext` (with 8s timeout ctx per recent) → `queryElastroForPlanner` + `queryLogloomForPlanner` (use worker executors: New*ASTQueryExecutor.Execute → ES search size~5-12 on flume-elastro-graph/flume-logloom-ast, compact/truncate res to ~2000 chars to avoid bloat).
- Inject: `injectRAGIntoMessages` (extra system msg after planner prompt).
- LLM Call: `s.llmClient.Chat(ctx, llm.ChatRequest{ Messages:..., AgentRole:"intake", TaskType:"planning", PlanSessionID:..., TimeoutSeconds:120, MaxTokens:4096 })`.
  - Updates planningStatus to "requesting_plan" + ES index *before* call (so UI sees progress).
  - On err/empty parse: fallback placeholderPlan (minimal 1 epic etc).
- Parse: `parseLLMResponse` (strip think, JSON unmarshal to message+plan).
- Commit (on user "commit" or auto?): `handleIntakeCommit` or internal → `commitPlan` (smart cap `getSmartMaxLeafTasks` based complexity + bushiness, buildFastPath or `buildTaskHierarchy` (seq IDs, org items "done" with DecomposedAt/ChildCount to skip PM), IndexDoc to agent-task-records + plan-sessions update, budget atomicIncrement).
- Response: enriched session with draftPlan, planningStatus (elapsed, provider, model, baseUrl, stage=ready).

Status updates + ES writes happen sync in handler (hence past 60s write timeout issues).

### Step 2: Gateway Ingress for the LLM Call
- From client: `internal/llm/client.go:Chat` (if gatewayAvailable) → `postGateway` (JSON payload with messages, agent_role, task_type? via derivation, plan_session_id, temperature etc; 3 retries but fast-fail on DeadlineExceeded; uses ctx timeout from caller 120s).
- (Legacy fallback if gateway down: direct to LOCAL_OLLAMA_BASE_URL + /api/chat stream+agg + strip think.)
- Gateway receive: `src/gateway/server.go:handleChat` → `dispatchChat`.
  - Global sem acquire (capacity).
  - Decode/Validate.
  - Per-plan-pm rate limiter (if role=pm; now logs plan_session_id correctly).
  - Derive taskType = req.TaskType || agentRoleToTaskType(req.AgentRole) ("intake"/"planner" → "reasoning"; "planning" explicit for intake).
  - isComplexTask = planning || pm || reasoning.
  - If streaming wanted (not for initial intake Chat, only ChatStream): async go routine + write NDJSON.
  - Else: route.
  - Acquire ollamaSem if ProviderOllama.
  - **Routing**:
    - If ensemble + complex: ExecuteEnsemble.
    - Else if multiRouter + nodeRegistry: `s.multiRouter.ExecuteSmartRoute(ctx, &req, taskType, withTools)`.
    - Else direct Route.
  - On success: persist telemetry, return.

### Step 3: Gateway LLM Routing / Mesh Logic (for local)
- `src/gateway/multi_node_router.go:ExecuteSmartRoute` (policy: local_only default, hybrid, frontier_only).
  - For local_only: `executeLocalOnly`.
    - Resolve provider (Ollama?).
    - If no nodes: direct Route.
    - minScore based taskType (planning/reasoning/pm=5, code=3).
    - SelectNode(taskType, minScore, requiresHighParam, withTools).
    - If planning/intake: special `executePlanningWithMeshResilience` (patient: 180s overall ctx, primary + all healthy secondaries with 90s per-attempt child ctxs, fresh for fallbacks; only after exhaust → frontier).
    - Normal: routeToNode primary; on fail tryFallbackNodes (others); on all fail frontier.
  - Resilience uses "fresh context" to not poison on slow primary.
  - Telemetry update (async ES for task).
- Node selection (`node_registry.go:SelectNode`): filter healthy + min score + tools; score = modelFit (reasoning + task boost + memory) * weights + loadInverse + latencyInverse + ensemble. Sorts, picks top. Auto-repair model tags etc from health.
- `routeToNode`: nodeURL="http://"+node.Host; clone req; `router.RouteToNode(ctx, cloned, nodeURL, node.AuthToken, withTools)` (attaches telemetry NodeID/Host/Model).

Registry refreshed from ES flume-node-registry (nodes seeded at `flume start` via wizard/YAML → POST /api/nodes → UpsertNodeToES; health auto-updates).

### Step 4: Actual Call to Local LLM (Gateway → Node)
- `providers.go:RouteToNode` → `ollamaWithNode(baseURL, authToken, ...)`.
  - (Special for planning/intake: force StreamOllamaChat even non-thinking models.)
  - Build options (temp, num_predict=MaxTokens, num_ctx from env/8192).
  - Inject no-think if needed.
  - Tools? StreamOllamaToolCall.
  - Thinking? StreamOllamaChat.
  - Else: ollamaNonStream (stream:false).
- Low-level (tool_stream.go for stream; providers for non):
  - `url = baseURL + "/api/chat"`.
  - Payload: {model: node.ModelTag (override!), messages (RAG-injected for planner), stream:true/false, options (temp/num_predict/num_ctx/keep_alive:24h), tools if any}.
  - New http.RequestWithContext (the passed ctx which has 120s/90s etc).
  - For stream: `&http.Client{}` (no Timeout, relies on ctx; NDJSON scanner, aggregate, ThinkMill strip, return content/thoughts/usage).
  - For non: doPost (client with explicit Timeout e.g. 300s, 4 retries exp backoff, extraHeaders support – but authToken *not* injected here for chat, only logged "has_auth"; contrast health_checker which does set Bearer if node.AuthToken).
  - Result: ChatResponse with Content (or Thoughts), Usage (prompt/completion/total from Ollama counts or OpenAI), Telemetry (node info).
- Return up the chain to client, to dashboard.

Note: gateway also handles /health, node CRUD (/api/nodes), metrics.

### Step 5: Gateway → (Response) → Broken Down Work
- Gateway returns full resp to llmClient (parseChatResponse gets content/usage/telemetry).
- Back to api_intake: elapsed recorded, parseLLMResponse, if success set draft/ready + IndexDoc plan-sessions + messages.
- User "commits" (or flow): commitPlan creates hierarchy tasks (fastpath flat or epic/feat/story/task with depends_on, HierarchyDepth, DecomposedAt for org, PlanSessionID, explosion_evidence, budget), Index to agent-task-records (with initial status "planned" or "done" for org, ready for first leaves), update session committed.
- **Broken down work**:
  - ES has planned tasks.
  - Worker Manager (internal/worker/manager.go): recon loop (ticker + wake), builds per-role workers, TriggerSweep (promote etc).
  - Sweeper (sweeps.go): promotePlannedTasks (ES search planned + repo filter + deps met + not blocked + depth<=MAX + per-repo WIP; early count opt; set ready; IndexDoc promote event; hierarchyCompletion for parents).
  - Claimer (claim.go): TryAtomicClaim (OCC, dedup, WIP caps per-repo/role, git lock, role→target status e.g. pm→planned but intake already done).
  - Runner (runner.go): claim task, dispatch by role (handlePM: decomp with guards/budgets/streaming LLM again via client; handleImplementer: ReAct loop + mandatory AST gate elastro_query_ast/logloom_ast_query before write + streaming + thoughts; handleReviewer etc.), EnforceTransition, clearStale on fail (role-aware reset), LogAgentReasoning *per turn/thought/tool/delta*.
  - Pool: bounded semaphore.
  - State: pkg/types TaskStateMachine enforced on 100% writes.
  - On complete: PR etc via git.

Full loop can call LLM again (PM decomp, implementer thinking) – same gateway path, with task_id/plan_session_id for correlation/rate/budget.

Observability: all via Log* + ES (agent-task-records execution_thoughts, telemetry).

## 4. Assessment of Current Connection from Flume to N# Local LLMs

**Current Mechanism (traced):**
- **Discovery/Registration**: Explicit (YAML/wizard at flume start → SeedNodes POST /api/nodes → ES flume-node-registry + Upsert). Not auto (no broadcast).
- **Health/Capability**: Dedicated HealthChecker (15s ticker, parallel probes per node): http GET /api/tags (models, quant), /api/ps (load/VRAM), POST /api/show (context/family/params). Updates registry Health + Capabilities (auto-repair model tags, estimate TPS/memory/reasoning from data + heuristics). Circuit breaker per node (3 fail → offline/degraded; half-open recovery).
- **Selection/Routing**: NodeRegistry.SelectNode (filter healthy + minReasoning + tools; weighted score modelFit(0.4)+load(0.3)+latency(0.2)+ensemble(0.1); task boosts e.g. high mem for planning). MultiNodeRouter: primary + fallbacks (normal) or full mesh exhaust with per-attempt ctx for planning/intake (resilient to avoid quick frontier). Route overrides model + baseURL.
- **Transport/Call**: Always HTTP (net/http). Base `http://<Host>` (from registry, e.g. 192.168.0.x:11434; host.docker.internal for local host Ollama from containers). 
  - Path: /api/chat (native Ollama, not always /v1 for compat).
  - Payload: model (node's), messages (RAG-injected for planner), stream:true/false, options (temp/num_predict/num_ctx/keep_alive:24h), tools if any.
  - Clients: Often fresh `&http.Client{Timeout: X}` (nonstream 300s etc) or `{}` (stream, ctx-driven). No shared Transport/KeepAlive explicit in most paths (Go defaults some, but per-call new hurts handshake/latency). doPost supports extraHeaders but authToken not passed for ollama chat.
  - Streaming: NDJSON scanner (long-lived POST), ThinkMill, aggregate for non-stream consumers like intake parser.
  - Nonstream: full wait for response (Ollama buffers until done if stream:false) → prone to header timeouts.
  - Auth: Optional Bearer from node.AuthToken (in-mem only, from AuthSecretPath intent via OpenBao). **Set on health probes + ensemble + some**, **passed to RouteToNode/ollamaWithNode** (logs has_auth), **but NOT injected in StreamOllamaChat / ollamaNonStream / doPost for /api/chat** (only Content-Type). Security incomplete for locked-down Ollama.
  - Timeouts/Control: Caller ctx (120s for planner etc) + client.Timeouts + per-attempt in resilience. Retries in doPost (4x exp) + client postGateway.
  - Fallback: If 0 nodes or all fail: routeFrontierFallback (gpt-4o etc via openai-compat, using secrets).
- **Integration Points**: Gateway (in-proc or separate container) as sole router for workers/dashboard (via FLUME_GATEWAY_URL or localhost:8090). Workers use llm.Client (prefers gateway for routing/telemetry; legacy direct only fallback). Dashboard intake forces planning type for resilient. Same path for all LLM (planner, PM, implementer, reviewer).
- **Other**: ollamaSem global + per-node sems for concurrency. Metrics (node load/latency/requests). Telemetry attached to responses (node_id/host/model). keep_alive:24h to avoid cold starts.

**Strengths vs Intent**:
- Matches "balances ... N# nodes": scoring + health + task-type min scores + resilient for high-value (planning).
- Efficient: local prefer, RAG grounding (token save), streaming (visibility, no full buffer for long), keep_alive, model override per node.
- Secure: OpenBao secrets (frontier + intended node), host validation, container net isolation + explicit bridge, no plaintext, ES durable, auth surface (even if incomplete).
- Performant for mesh: health for latency/load awareness, fresh ctxs prevent poison, ctx everywhere.
- No change to frontier: abstraction (ProviderOllama vs others, Route vs RouteToNode) keeps OpenAI/Anthropic/etc intact.

**Inefficiencies/Gaps (not best-in-class yet)**:
- **Connection Mgmt (big perf hit)**: Fresh http.Client per call/stream (in tool_stream, doPost, many places). No shared *http.Transport with DisableKeepAlives=false, MaxIdleConns, MaxIdleConnsPerHost=10+, IdleConnTimeout, TLSHandshakeTimeout. Each request = TCP+TLS handshake to node (esp bad for 35b slow gens or high RPS). Stream clients {} have no keepalive tuning. Results in higher latency, more resource use vs persistent conns.
- **Auth for Local (security gap)**: Declared (AuthSecretPath, in-mem token, passed around) but **not wired into inference HTTP headers** for chat (only probes). **Important (user clarification):** this is *strictly opt-in*. Most local Ollama users (LAN, containers via host.docker.internal, dev machines) do **not** run auth on Ollama and should not be forced to — it would be pure overhead. Phase 2 makes the path complete *only for users who choose to secure specific nodes*: they manually place a bearer token in OpenBao at a `auth_secret_path` they control, then include that path when registering the node via the portal (POST /api/nodes or the node mesh UI). Default/unauthenticated behavior is unchanged and zero-cost. If no AuthSecretPath (or load fails), **no header is ever sent**. Token resolution was incomplete (only passed, never loaded from bao for nodes; never injected on /api/chat).
- **Streaming vs Non-Stream Inconsistency**: Intake planner (and some) used nonstream historically (full buffer wait → header timeouts on slow local). Recent force to stream for planning good, but not universal (e.g. non-thinking non-tools fall to nonstream; tools use special stream). Nonstream doPost has client.Timeout which can kill mid-gen ("awaiting headers"). Ollama with stream:false can be slow to first byte.
- **Resilience/Planning Overhead**: Special path good for avoiding quick frontier, but per-attempt still sequential (primary 90s + secondary 90s = wall >120s target easily if gens marginal). No parallel probe or "best of N quick" for planner. 180s overall still allows long burns. Fallback to frontier on exhaust (but user wants local success).
- **Observability/Tracing**: Good (telemetry node attached, duration_ms, request_id, per-plan-pm, Log* in workers), but gateway LLM hops lack full distributed trace (span per node call, correlation across gateway<->node). RAG calls in planner not always timed in same context. Health vs inference separate.
- **Efficiency for N Nodes**: Selection good, but no conn reuse means N nodes = N handshakes per burst. No node-local caching of conns/pools. Health probes add load (though necessary). For very large N, registry refresh full scan.
- **Defaults & Fallbacks**: Default base often host.docker.internal (works but DNS resolve cost). If mesh 0 nodes: direct (no mesh benefits). Legacy direct from client bypasses all gateway goodness (routing, rate, telemetry).
- **Other**: No HTTP/2 (Ollama may not prioritize), no connection upgrade, raw http vs official ollama Go client (which might have better defaults/conn mgmt). Timeouts duplicated (client, ctx, resilience). For "best class": feels like 80% there but transport/auth/always-stream are low-hanging for "maximally optimize".

### Consideration of Alternative Transports (gRPC/HTTP/2 and Sockets)

During the assessment of the current HTTP-based connection (plain `net/http` to Ollama's native REST + NDJSON streaming on `/api/chat`), the following alternatives were explicitly considered for "most efficient, secure, and useful" local LLM communication across a distributed node mesh:

**gRPC (over HTTP/2):**
- **Potential benefits**: Binary protocol, multiplexing (multiple streams over one TCP conn), better flow control, built-in streaming (no manual NDJSON parsing), lower per-call overhead once connected, strong typing via protobuf (could define `Chat` service mirroring Ollama's API).
- **Why not recommended as primary path (at this time)**:
  - **Ollama compatibility**: Official Ollama (and Exo) expose only HTTP/REST (OpenAI-compatible `/v1` + native `/api/*`). There is no stable gRPC API in upstream Ollama. Implementing gRPC would require either (a) a sidecar/proxy (e.g., grpc-gateway or Envoy) on every node, (b) forking/patching every Ollama instance, or (c) a custom bridge inside the Flume gateway that translates gRPC <-> REST. All add operational complexity, another failure mode, and deployment surface—directly conflicting with Flume's goal of "zero modification to the LLM runtime" and "secure platform you can depend on" with stock hardware nodes.
  - **Mesh reality**: Nodes are heterogeneous and often remote (192.168.x LAN, multiple Mac Minis). gRPC adds TLS/mTLS requirements that are already partially solved (or intended) at the HTTP layer. The perf gain from multiplexing is real for high-concurrency, but Flume's primary hot path (one planning call + per-task streaming) benefits more from *connection reuse* than from multiplexing within a single call.
  - **Breaking changes risk**: Would require new client code paths in `internal/llm/client.go`, gateway `providers.go`, and potentially worker streaming handlers. Frontier paths (OpenAI, Anthropic, etc.) are already OpenAI-compat HTTP; diverging the local path too far increases maintenance.
  - **In the report's recommendations**: The proposed `NodeConnManager` *does* explicitly call for `ForceAttemptHTTP2` on the shared `*http.Transport`. This gives many HTTP/2 benefits (multiplexing, header compression, better connection reuse) **without changing the application protocol**. Go's `http.Client` + `http2` package will automatically upgrade when the server (Ollama) advertises support over ALPN. This is the pragmatic "HTTP/2 where possible" approach that stays 100% compatible with current Ollama deployments.
  - **When it could make sense later**: If a large subset of nodes run a gRPC-capable runtime (e.g., vLLM with its gRPC server, or a future Ollama gRPC mode), we could add a `Node.Capabilities.SupportsGRPC` flag and a parallel `grpc` transport path in the conn manager—behind a feature flag, without touching the REST paths or any frontier code.

**Unix Domain Sockets (or raw TCP sockets):**
- **Potential benefits**: For *co-located* cases (gateway container talking to Ollama on the same host), UDS avoids the TCP stack entirely (lower latency, no loopback overhead, better security via filesystem permissions instead of network ACLs). "Socket connection" would feel more "direct."
- **Why not suitable as a general solution**:
  - **Distributed mesh design**: Flume's `flume-node-registry` and health checker are built around IP:port hosts (e.g., `192.168.0.227:11434`, `node-88aud`). The entire point of the "N# of nodes within a node mesh" (Mac Minis on LAN, mixed Linux/Windows boxes) is *network distribution*. Unix sockets only work on the *same machine*. Remote nodes would still require TCP (or SSH tunnels, which reintroduces all the problems we're trying to solve).
  - **Container-to-host bridge**: Today, workers/gateway use `host.docker.internal:11434` precisely because of Docker network isolation. Passing a host Unix socket into containers (`-v /var/run/ollama.sock:/var/run/ollama.sock`) is possible but brittle (permissions, SELinux/AppArmor, different paths on macOS vs Linux, not portable to remote nodes).
  - **Protocol still needed**: Even over a socket, you need *some* framing. You'd end up reimplementing (or wrapping) the HTTP/1.1 or HTTP/2 protocol semantics that Ollama expects, or inventing a custom binary protocol on top of the socket. This loses the "use stock Ollama" simplicity.
  - **Current code already has the hook**: The `baseURL` in `ollamaWithNode` / `StreamOllamaChat` is just a string. In theory one could support `unix:///var/run/ollama.sock` or `http+unix://...` schemes (Go's `http.Transport` + custom `DialContext` can do this). But because the mesh is multi-machine, this would only ever be a special-case optimization for "local-only single-host" deployments—not the general N-node case.
- **Recommendation in this context**: Keep the transport as (upgraded) HTTP. The biggest win for same-host performance is already achievable with the persistent `Transport` + tuned keep-alives recommended in the report. If a user has a purely local single-host setup, they can already point nodes at `127.0.0.1` or `host.docker.internal`; adding explicit UDS support could be a small follow-up (custom Dialer in the conn manager) without affecting the distributed path or frontier compatibility.

**Overall decision for the report**: The recommended path (shared per-node `*http.Transport` with explicit keep-alives, `ForceAttemptHTTP2`, and injection into all Ollama call sites) is the highest-leverage, lowest-risk improvement that stays within the existing contract. It delivers most of the efficiency of "better than plain per-request HTTP" while preserving the ability to talk to unmodified Ollama/Exo instances across a real multi-machine mesh. gRPC and UDS were evaluated but deprioritized for the reasons above; they are noted here for future consideration (e.g., capability flags per node) rather than as the primary optimization.

This keeps the "no breaking changes with connections to external frontier LLM API calls" guarantee: all changes are isolated to the `ProviderOllama` + mesh paths inside the gateway.

Current is functional and better than raw direct (thanks to mesh + gateway), but not "best in class" for perf (handshakes), security (auth incomplete), usefulness (limited reuse/trace for distributed N nodes).

## 5. Recommendations: How to Maximally Optimize This Flow for Best-in-Class Local LLM Comm (No Breaking Frontier Changes)

**Principles (from SKILLs + intent)**: Follow reliable-go (ctx first, explicit err, recon/bounded/obs, idempotent) + flume-go (propagate ids/roles, rich reasoning on every LLM hop/decision/fail, role-aware, no silent legacy). Separate planning (intake fast path) from exec. Verification loops. No fire-forget. Preserve gateway as single brain for routing/telemetry/budget/rate (workers/dashboard always prefer it). Abstraction for providers must stay (Ollama local mesh vs frontier OpenAI-compat etc unchanged; only enhance ollama* paths).

**Core Optimization: "Node-Aware Persistent Connection Manager" in Gateway**
- Introduce `NodeConnManager` (or enhance ProviderRouter) owning per-node `*http.Transport` (or pooled clients).
  - Config: MaxIdleConnsPerHost=20, IdleConnTimeout=90s, TLSHandshakeTimeout=5s, DisableKeepAlives=false, ForceAttemptHTTP2 (if backend supports).
  - Shared across streams/nonstreams for that node (reuse conns for /api/chat, /api/tags probes?).
  - Lifecycle: tied to node health (on offline, close idle conns; on healthy, warm?).
  - Inject into StreamOllama* / doPost / ollama* (pass transport or client factory instead of always new {} or Timeout-only).
  - Benefits: eliminates repeated handshakes (huge for latency on local mesh gens, esp 35b+), lower CPU, better "useful" for high N (concurrent to different nodes efficient). Matches "efficient/performant".
- Always-stream for *all* local Ollama calls (planning + exec). Deprecate nonstream ollama path for ProviderOllama (or gate behind flag). Ollama stream:true keeps conn alive, first tokens sooner, avoids buffer/timeout issues. Update intake to use ChatStream internally if needed (collect full for JSON parse, but stream the NDJSON to caller for UX). Nonstream only for frontier compat if required.
- Unify timeout: prefer ctx.WithTimeout everywhere; remove/minimize hard client.Timeouts (or make them generous + ctx wins). In resilience, the 90s per-attempt already good.
- Make auth complete for local (opt-in only): in ollamaWithNode / stream funcs, if authToken != "", set `req.Header.Set("Authorization", "Bearer "+authToken)` (like health does). **Explicit user requirement:** auth is optional; "allow users to manually setup auth with their Ollama instances within the portal". Add lazy load in RefreshFromES/Get/route/health: if AuthSecretPath set, use SecretStore to populate AuthToken from OpenBao (in-mem only, never persisted/logged). Add `GetNodeAuthToken` helper. Expose in node test API + docs. Portal already works (proxies the JSON field). This makes "secure platform" real for the subset who opt-in, without any overhead or change for the (vast) majority running unauthenticated local Ollama. if "" or load fail → skip header exactly as today.
- Enhance node health to also probe auth (dedicated /api/version or tags with auth test).

**Routing/Planning Specific (for <120s simple local success)**:
- For intake planner (AgentRole=intake, TaskType=planning): add "fast local path" option: pick *top* node only (no full mesh exhaust unless primary hard fails fast). Or parallel quick probes (race first healthy response within 60s budget, cancel others). Still use stream. This + slim RAG (already) + 120s budget should hit historical target for trivial/medium doc-like requests.
- Planner RAG: already improved (timeout+truncate), consider skip for pure "doc" prompts (detect via keywords + complexity=1-3) or make optional via env/setting. RAG great for code changes, overhead for docs.
- Propagate more: ensure every gateway LLM call (incl planning) emits structured log + (if in worker context) LogAgentReasoning with node_id, duration, tokens, model, error. Dashboard intake should too on RAG/LLM steps.
- Model selection: allow per-role "preferred local model" in config, bias SelectNode.

**Observability & Usefulness**:
- Distributed tracing: wrap node calls with trace spans (or simple parent request_id + child "llm_hop" logs with node_id, duration, tokens, model, error).
- Per-node conn stats: expose in /health or metrics (idle conns, inuse, last used, errors).
- "Why slow" UX: if planner >60s, surface "node X latency Y, load Z, gen took T" in session planningStatus + reasoning.
- Metrics: node-specific LLM call rates/latency/p99 (beyond current).
- For N nodes: support "ensemble local" (query 2-3 nodes in parallel for same prompt, pick best by confidence/latency) for critical planning – behind policy.

**Security Hardening (no frontier impact)**:
- mTLS for mesh: if nodes register with certs, gateway Transport.TLSClientConfig with client certs per node (from OpenBao too). Validate server certs (no InsecureSkip for nodes).
- Network: document/enforce (e.g. compose network rules, or WireGuard for remote nodes).
- Rate per node: already sems, enhance with token bucket in conn manager.
- Audit: every node call (success/fail) to ES "agent-llm-calls" or existing telemetry, with auth used, conn reused flag.

**Implementation Notes (Follow SKILLs, No Breaks)**:
- Add in src/gateway/: new node_conn_manager.go (or extend providers.go Config/ Router).
- Modify only ollama* paths + RouteToNode (pass manager).
- Keep all Provider* (openai etc) + Route + frontier paths 100% unchanged.
- Update health_checker to reuse conns from manager (bonus).
- Add to flume-go SKILL checklist: "LLM local calls use shared conn manager + always stream + full auth injection + reasoning emitted".
- Tests: extend gateway_test, node_api_test; add conn reuse test (e.g. idle count after calls).
- Config: new envs like FLUME_OLLAMA_KEEPALIVE, FLUME_OLLAMA_MAX_IDLE_PER_NODE (defaults safe).
- Migration: zero-downtime (new code compatible with old nodes; old conns just slower).
- Verification: use todo_write, check-work subagent, golangci + race, local mesh repro (as in past monitors).
- Measure: before/after handshake time (tcpdump or logs), p99 gen latency for planner on simple request, conn count under load.

**Expected Outcomes**:
- Local planner for simple requests: reliably <60-90s (conn reuse + stream + fast path + slim RAG).
- Higher throughput on mesh (reuse saves ~10-50ms + resources per call).
- Secure by default for protected nodes.
- Best-in-class: feels like "local LLM is first class citizen" with enterprise observability/reliability, while frontier is seamless fallback.
- Token spend down (more local success, less frontier), outcomes up (faster feedback, less stuck), dependability (audited, bounded, no silent fails).

**Risks/Mitigations**: Conn mgmt bugs → thorough tests + feature flag (FLUME_OLLAMA_CONN_POOL=1). Auth breakage on unauth nodes → conditional header only if token. Performance regression on frontier → untouched code.

This report is the "maximally optimize" plan. Next: implement via small PRs (conn manager, auth wire, always-stream for local, fast planner path), following SKILLs + verification.

(End of report. Can be expanded with code sketches or run as design doc review.)
