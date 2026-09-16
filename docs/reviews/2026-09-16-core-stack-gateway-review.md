# Flume core stack / gateway review

**Date:** 2026-09-16  
**Reviewer:** Grok Build subagent (reliable-Go checklist)  
**Scope:** Remaining Flume core after workers left the default Compose stack. Read-only; no source changes.

**Files reviewed:**
- `src/gateway/**` (server, config, secrets, providers, routing, node registry, metrics, skills)
- `src/gateway/Dockerfile`
- `docker-compose.yml`, `docker-compose.gateway.yml`
- `internal/logger`, `internal/es` (as used by gateway)
- `config/openbao.hcl`

**Out of scope (noted only as residual):** dashboard/worker Go still present behind Compose profile `full`; frontend SPA source other than how nginx fronts the gateway.

---

## Summary

The default Compose stack is now the four-service core (OpenBao, Elasticsearch, Gateway, nginx console). Gateway is the product process: it boots without `flume start`, creates `flume-agent-models` and `flume-node-registry` with `number_of_replicas: 0`, exposes `GET /api/stack`, and keeps `FLUME_GATEWAY_STUB` as an explicit skip path. Compose does **not** set the stub flag, which is the correct split.

The implementation has real reliability work (ctx on request I/O, provider fail-closed keys, secret redaction, livez/readyz, graceful HTTP drain, concurrency gates on chat). It is still a local-dev security posture shipped as the default product: Vault `-dev`, well-known passwords published on `0.0.0.0`, no gateway auth, and `InsecureSkipVerify` on every gateway TLS client. Two functional bugs will bite operators on the documented skills API and on any cloud-provider call from the Alpine image.

No issue in this review is a silent data-corruption or unauthenticated-root-on-the-host class problem. The highest-severity findings are process crashes on `/skills*`, secret-store non-persistence, and an open control plane on published ports.

---

## Issues

### Issue 1 -- Severity: major
- File: src/gateway/server.go:110
- Description: The gateway registers chat, embeddings, node CRUD, routing-policy writes, log-level changes, skills, and metrics with no authentication middleware (grep of `src/gateway` finds no `RequireAuth` / `FLUME_ADMIN_TOKEN` / bearer check). Compose publishes those ports on all interfaces (`${GATEWAY_PORT:-8090}:8090`, plus ES `9200` and Vault `8200`). `POST /v1/chat`, `PUT /api/routing-policy`, `POST /api/nodes`, and `POST /internal/level` are therefore reachable by anyone who can hit the host. Combined with Issue 3, a LAN client can spend configured LLM keys, register probe targets (SSRF into the Docker network / RFC1918, metadata IPs are the only block in `isValidNodeHost`), or flip routing to `frontier_only`. `/health` is used as the Compose healthcheck and always returns 200, so it does not gate this.
- Suggestion: Bind published ports to `127.0.0.1` for the local product default. Add a single auth gate (shared secret or OpenBao-backed token) on mutating and spend-capable routes; leave `/livez` unauthenticated. Do not proxy `/metrics` or `/internal/level` on the console port without the same gate.
- Status: open

### Issue 2 -- Severity: major
- File: docker-compose.yml:16
- Description: The service named `openbao` runs `hashicorp/vault:1.15` with `server -dev -dev-listen-address=0.0.0.0:8200`. Dev mode is in-memory, auto-unsealed, and uses `VAULT_DEV_ROOT_TOKEN_ID`. `config/openbao.hcl` (file storage, `tls_disable = 1`) is never mounted or referenced. Gateway hydrates `LLM_API_KEY` from this store at boot (`StartGateway` → `secrets.GetGlobalSecrets`). A `docker compose restart openbao` wipes every KV secret the core stack exists to hold. The HCL file's own comment says TLS must be enabled in production; Compose never gets that far because it never uses the file.
- Suggestion: For the default core, run OpenBao/Vault with the file backend from `config/openbao.hcl` (or a Compose-specific HCL), persist `/vault/file`, and document a distinct production overlay (TLS listener, non-root token, audit device, no `-dev`). Keep `-dev` behind an explicit profile, not `docker compose up`.
- Status: open

### Issue 3 -- Severity: major
- File: docker-compose.yml:18
- Description: Defaults are `OPENBAO_TOKEN:-flume-dev-root` and `FLUME_ELASTIC_PASSWORD:-flume-dev-elastic`, injected into gateway (and the `full` profile services). ES HTTP TLS is off (`xpack.security.http.ssl.enabled=false`). Those credentials are the well-known strings from the repo, published on host ports 8200 and 9200. This is acceptable only if the stack is bound to loopback and labeled local-dev. As shipped, any process on the host (or LAN, see Issue 1) can read/write ES and Vault with the documented defaults.
- Suggestion: Keep the strings as documented **local** defaults, but bind `127.0.0.1:${ES_PORT:-9200}:9200` and the same for Vault/Gateway. Fail boot (or print a one-time stderr banner) when the defaults are in use and the process is not in stub/dev. Require overrides in any non-dev compose overlay.
- Status: open

### Issue 4 -- Severity: major
- File: src/gateway/server.go:120
- Description: `NewServer` registers `skills.HandleSkillExecute(s.skills)` (and list/reload) while `s.skills` is still nil. Closures capture that nil pointer. `StartGateway` later does `server.skills = skills.NewSkillRegistry(); server.skills.LoadAll(ctx)` and logs `skills_loaded`, but the mux still holds the nil registry. `SkillRegistry.Get` / `List` / `Reload` immediately `RLock` the receiver, so `GET /skills`, `POST /skills/execute/…`, and `POST /skills/reload` panic and crash the process. The Alpine image also has no skill files and no `FLUME_SKILLS_DIR`, so even a fixed wiring would load zero skills; the panic is the user-visible bug.
- Suggestion: Create the registry before `HandleFunc`, pass that pointer into the handlers, and register routes after `LoadAll`. Nil-guard in `Get`/`List` if tests construct a bare `Server`. Copy skill trees into the image or set `FLUME_SKILLS_DIR` if skills are part of the core product.
- Status: open

### Issue 5 -- Severity: major
- File: src/gateway/config.go:141
- Description: Gateway HTTP clients set `tls.Config{InsecureSkipVerify: true}` unconditionally: Config ES client (`config.go:141`), node registry (`node_registry.go:128`), `NodeConnManager` (`node_conn_manager.go:109`, comment “tighten in Phase 5”), telemetry (`server.go:1438`), and multi-node ES writes. There is no env escape hatch. `internal/es.New` is better — it skip-verifies only for `https://` (`client.go:44`) — but the gateway does not use that client for its own ES calls; it reimplements HTTP with skip-verify always on. Compose currently uses `http://elasticsearch:9200` and `http://openbao:8200`, so this is dormant on the default path and live as soon as anyone points `ES_URL` at HTTPS (ES 8 self-signed in Docker is the stated reason).
- Suggestion: Match `internal/es`: skip-verify only for HTTPS, and only when `FLUME_TLS_INSECURE=1` (or a pinned CA file). Default verify on. Document the Docker self-signed case instead of making MITM the compile-time default.
- Status: open

### Issue 6 -- Severity: major
- File: src/gateway/config.go:167
- Description: `Config.Refresh` takes the write lock and then performs five sequential ES GETs (`loadSystemConfig`, `loadGlobalConfig`, `loadAgentModels`, `loadCredentials`, `loadRoutingPolicy`) with a 3s client timeout each. `ResolveModel`, `GetBaseURL`, `GetRoutingPolicy`, and Prometheus gating all share that mutex. A slow or wedged ES holds every in-flight chat at config resolution for up to ~15s. The singleflight flag correctly prevents a thundering herd of fetches; it does not prevent I/O under the write lock.
- Suggestion: Fetch into locals without `c.mu`, then swap fields under the lock. Keep the CompareAndSwap singleflight. Optionally time-bound the whole refresh with one context rather than stacking five client timeouts.
- Status: open

### Issue 7 -- Severity: major
- File: src/gateway/Dockerfile:36
- Description: Runtime image is `FROM alpine:latest` copying only `/gateway`. Alpine does not ship `ca-certificates`. Go uses the system roots; HTTPS to OpenAI/Anthropic/Gemini/xAI from this container fails with `x509: system roots not found` once a managed provider is configured (`LLM_API_KEY` is already a Compose env passthrough). Local Ollama over HTTP still works, which hides the break on the default path. The same Dockerfile comments claim a “zero-attack-surface distroless” image and “wget for healthcheck” but neither distroless nor `apk add wget ca-certificates` is present. Frontend’s Dockerfile correctly `apk add wget`; gateway does not.
- Suggestion: `apk add --no-cache ca-certificates wget`, pin `alpine:3.21` (or switch to distroless + a compose healthcheck that uses HTTP, not wget). Run as a non-root USER.
- Status: open

### Issue 8 -- Severity: minor
- File: src/gateway/server.go:803
- Description: `HealthChecker.Start` and `FrontierProber.Start` are launched with the startup `context.Background()` and never cancelled. `HealthChecker.Stop` and `NodeConnManager.CloseAll` exist and are not called from `ListenAndServe` after SIGTERM. HTTP drain is 25s and correct; background probes and idle transports are not owned. Process exit reaps them, so this is not a leak across restarts, but a hung probe can still run past `Shutdown` and a second `Stop()` would panic on a closed channel if someone later wires it naively.
- Suggestion: Derive a `runCtx` cancelled at the start of shutdown; call `healthChecker.Stop()`, stop the prober, and `connMgr.CloseAll()` before returning from `ListenAndServe`. Make `Stop` idempotent (`sync.Once`).
- Status: open

### Issue 9 -- Severity: minor
- File: src/gateway/server.go:392
- Description: Streaming chat starts a worker goroutine with no `recover` and no join on handler return (`server.go:392`). `persistTokenTelemetry` is `go`’d per request that carries `X-Worker-Name` (`server.go:526`, `server.go:1388`) with a fresh `http.Transport` each time. A panic in the stream worker kills the process (unrecovered goroutine panic). Telemetry goroutines are bounded only by ES 5s timeout and request rate; they are not tracked by shutdown. Chat itself is gated by `globalSem` (default 32) and `maxBodyBytes` (2 MiB) — embeddings are not (Issue 13).
- Suggestion: `defer recover` + send an error chunk in the stream worker; run telemetry through a bounded worker pool or the existing logger thought queue; reuse `config.httpClient` instead of allocating a Transport per index.
- Status: open

### Issue 10 -- Severity: minor
- File: src/gateway/server.go:1418
- Description: Gateway-created indexes `flume-agent-models` and `flume-node-registry` correctly set `number_of_replicas: 0` for single-node ES. Writes to `agent-token-telemetry/_doc` and `agent-security-audits/_doc` (`secrets.go:325`) do not ensure the index. ES 8 defaults replicas=1, so the first telemetry or secret-audit document turns the cluster yellow. Compose healthcheck already accepts yellow (`docker-compose.yml:46`), so the stack stays “healthy” with replica unassigned. `cluster.routing.allocation.disk.threshold_enabled=false` further allows the node to fill disk without ES protecting itself.
- Suggestion: `EnsureIndex` those two write targets with `number_of_replicas: 0` at the same boot path as agent-models/node-registry. Re-enable disk watermarks for any non-dev overlay.
- Status: open

### Issue 11 -- Severity: minor
- File: src/gateway/tool_stream.go:429
- Description: Server constructs a `NodeConnManager` and passes it to `ProviderRouter` (`server.go:94`), but stream paths use a package-level `ollamaConnMgr` (`tool_stream.go:429-455`) with timeout 0. Non-stream `doPost` uses the server-owned manager. Two connection pools to the same Ollama host, and `OllamaConnStats` reports the global one. `getEnvInt` / `getEnvBool` in `node_conn_manager.go:67-75` ignore env and always return the default, so `FLUME_OLLAMA_KEEPALIVE` / `FLUME_OLLAMA_MAX_IDLE_PER_NODE` documented in comments are no-ops.
- Suggestion: Delete the package singleton; pass `s.connMgr` into `StreamOllama*`. Implement the env helpers with `strconv` or drop the comments.
- Status: open

### Issue 12 -- Severity: minor
- File: src/gateway/node_registry.go:118
- Description: Stub mode is otherwise consistent: `NewConfig` leaves `esURL` empty when `FLUME_GATEWAY_STUB` is set (`config.go:126`), `StartGateway` skips ES/OpenBao bootstrap (`server.go:751`), and `handleStack` reports those deps as `skipped` (`stack_test.go` covers this). `NewNodeRegistry` still rewrites empty `esURL` to `http://elasticsearch:9200`. In stub, `POST /api/nodes` would try to persist to a host that is not part of the stub contract. Compose core does not set `FLUME_GATEWAY_STUB` (correct); this is a stub-path hole, not the default `docker compose up` path.
- Suggestion: If `esURL == ""`, leave it empty and no-op ES methods when stub is on. Do not default the registry URL inside `NewNodeRegistry`.
- Status: open

### Issue 13 -- Severity: minor
- File: src/gateway/server.go:227
- Description: `handleEmbeddings` does not use `globalSem`, `http.MaxBytesReader`, or `ValidateChatRequest`. Chat/tools apply all three (`server.go:272-292`). A large embeddings body bypasses the slow-body cap that the file comment says exists to protect server goroutines.
- Suggestion: Share the same gate + max-bytes wrapper as `dispatchChat`.
- Status: open

### Issue 14 -- Severity: minor
- File: src/gateway/server.go:761
- Description: Index create failures are `log.Warn` and boot continues. That is reasonable if ES is optional; it is not, because Compose `depends_on` ES healthy and the core UI’s node mesh needs `flume-node-registry`. Comments on `EnsureAgentModelsIndex` (`config.go:530-534`) and `NodeRegistry.EnsureIndex` (`node_registry.go:638-642`) still say creation is centralized in `flume start` and these functions only HEAD; both functions PUT the index with `number_of_replicas: 0`. The warn string still tells operators to run `flume start`.
- Suggestion: Fail `StartGateway` (or fail `/readyz`) if ensure-index fails in non-stub mode. Update the comments to match the create-on-boot behavior.
- Status: open

### Issue 15 -- Severity: minor
- File: internal/logger/logger.go:338
- Description: First `LogAgentReasoning` (node-auth path in `node_registry.go:176`) starts five `thoughtWorker` goroutines. Gateway never calls `SetESBridge`, so workers sit on a 2048 buffer and no-op. They are not stopped on shutdown. Harmless idle threads, but it pulls `internal/es` into the gateway image solely for a worker-era bridge.
- Suggestion: Do not start the queue until `bridgeES != nil`, or skip `LogAgentReasoning`’s ES path in the gateway process.
- Status: open

### Issue 16 -- Severity: nit
- File: src/gateway/Dockerfile:36
- Description: `FROM alpine:latest` is floating. Process runs as root. Comments describe a distroless ~15MB image and wget healthchecks; wget is not installed (busybox wget is usually enough for `wget --spider`, which is why Compose `--wait` can still pass). Image tag in Compose is `flume-gateway:stub` (`docker-compose.yml:56`) after the stack stopped being a stub.
- Suggestion: Pin the base, add a non-root user, install wget+CA certs (Issue 7), tag `flume-gateway:core`.
- Status: open

### Issue 17 -- Severity: nit
- File: src/gateway/config.go:530
- Description: Stale comments on index helpers (see Issue 14) and `StartGateway`’s warn text. Operators following the log will run `flume start` even though the core stack is designed not to need it.
- Suggestion: Rewrite those comments/logs to “gateway bootstraps these indexes; warn means ES auth or cluster issue.”
- Status: open

### Issue 18 -- Severity: nit
- File: internal/logger/logger.go:18
- Description: `sensitiveFragments` includes `"pat"`, so attribute keys containing `path` (e.g. `secret_path`) are redacted. Over-redaction, not a leak. Values of secrets are correctly masked in tests (`gateway_test.go` / `logger_test.go`).
- Suggestion: Match fragments on token boundaries (`_pat`, `password`, `token`, `secret`, `api_key`) instead of substring `pat`.
- Status: open

---

## Strengths

- **Stub vs core split is correct on the default path.** `FLUME_GATEWAY_STUB` is documented and tested (`stack_test.go`); Compose does not set it. `docker-compose.gateway.yml` is a back-compat `include` of the four-service core rather than a second stub stack.
- **Index bootstrap with yellow-replica fix for the two indexes the core actually needs.** Both `EnsureAgentModelsIndex` and `NodeRegistry.EnsureIndex` PUT mappings with `number_of_replicas: 0`. ES healthcheck accepts green|yellow. Gateway `depends_on` ES and OpenBao `service_healthy`.
- **Request path has ctx, timeouts, and classification.** Chat uses request ctx + `X-Timeout-Seconds` / role defaults; OpenBao client is 5s; ES config client is 3s; secret KV reads use `http.NewRequestWithContext`. `ProviderError` + `ClassifyProviderError` replace string-matching in the handler. Managed providers fail closed when no API key is found (`providers.go:345-352`); Ollama does not send empty `Authorization` headers.
- **Secrets are treated as secrets in the happy path.** `SecretStore` comments and `auditAccess` log key names only. `Node.AuthToken` is `json:"-"` and stripped before ES and `AllNodes`. slog `secureHandler` redacts attribute names containing `key`/`token`/`secret`/`password`.
- **Health vs ready vs live is the right split.** `/livez` is process-alive only; `/readyz` returns 503 after SIGTERM so kube can drain; HTTP `Shutdown` is 25s. `/api/stack` uses a 3s ctx and returns `status=down` with HTTP 200 so the console can render a degraded stack without Compose killing the gateway.
- **Concurrency is bounded on the chat path.** Global semaphore (default 32), Ollama semaphore from `/api/ps` or CPU heuristic (5s detect timeout), `MaxBytesReader` 2 MiB, PM per-plan limiter. Node host validation blocks `169.254.169.254` / metadata hostnames.
- **Chat validation and tool guardrails exist and are tested** (`security_test.go`, `ValidateChatRequest`, `SanitizeToolResponse` at the router layer).

---

## Residual risks

- **OpenBao is a Vault `-dev` container.** Using `hashicorp/vault:1.15` while the product name and HCL file say OpenBao is a licensing/branding mismatch and means `config/openbao.hcl` is dead config. Production needs a real OpenBao/Vault server, TLS, unseal, and audit — none of that is in the default compose.
- **ES 8 security is “password over HTTP.”** `ELASTIC_PASSWORD` implies xpack security (ES 8 default on), but HTTP SSL is explicitly disabled and port 9200 is published. Fine on a private Docker network bound to loopback; not fine as a cluster default.
- **`InsecureSkipVerify` is a repo-wide habit** (`cmd/flume/orchestrator/elastic.go`, dashboard, git host). Fixing gateway clients (Issue 5) does not fix `flume start` / workers when profile `full` is used.
- **Frontend nginx (`src/frontend/nginx.conf`) proxies `/api/` and `/v1/` to the gateway with 1200s read timeout and no auth.** Even if gateway ports are loopback-only, the console port (`8765`) is the public surface. Not in the strict file list; it amplifies Issue 1.
- **Gateway does not use `internal/es.Client` at runtime.** The image copies `internal/es` because `internal/logger` imports it. Duplicate ES HTTP stacks (config, registry, routing_policy, telemetry, secrets audit) each reimplement auth (`esSetAuth` vs inline `ES_API_KEY` / Basic). Drift is already visible: `routing_policy.go` does not ignore API keys containing `"bypass"`; `esSetAuth` does.
- **Frontier prober bills 1-token chat completions every 60s** when a frontier mix is configured (`frontier_prober.go:34-64`). Default routing is `local_only`, so this is off until someone PUTs a policy (which is unauthenticated today).
- **Workers/dashboard Go remains in-tree and in Compose profile `full`.** A `docker compose --profile full up` still starts them against this gateway. This review did not re-audit that mesh.
- **No Compose `restart` policy, no memory limits on ES (`512m` heap only), disk watermarks disabled.** Local-dev acceptable; not a production overlay.

---

## Checklist (reliable-Go)

| Item | Verdict |
| --- | --- |
| ctx first param, timeouts on I/O | Mostly: request paths yes; `detectFromOllamaPS` uses `client.Get` without ctx (5s client timeout); health/prober use Background ctx |
| errors wrapped, classified | Yes on provider/handler path; several ES helpers return raw `err` from `http.NewRequest` |
| no unbounded goroutines; shutdown ownership | HTTP drain yes; health/prober/telemetry/thought workers not owned |
| structured slog, metrics, health/ready | Yes; `/health` is liveness-shaped despite the name |
| no secrets in logs | Happy path yes; OpenBao HTTP error bodies (512B) could theoretically echo server messages |
| TLS/auth defaults | Fail: no gateway auth; skip-verify; HTTP ES/Vault |
| OpenBao dev vs production | Dev-only as default; HCL unused |
| ES security, index bootstrap, yellow replicas | Password set, HTTP TLS off; two indexes replica=0; telemetry/audit indexes not |
| InsecureSkipVerify | Present on all gateway TLS clients; `internal/es` only on HTTPS |
| healthchecks, compose secret defaults | Healthchecks exist; defaults `flume-dev-root` / `flume-dev-elastic` |
| stub vs non-stub split | Correct for default Compose; `NewNodeRegistry` default URL leaks into stub |
