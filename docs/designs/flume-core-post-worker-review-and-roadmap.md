# Flume after workers: review and enhancement roadmap

**Date:** 2026-09-16  
**Scope:** Remaining code after the default stack became OpenBao + Elasticsearch + Gateway + stripped console. Workers and the Go dashboard are no longer on `docker compose up`.  
**Method:** Direct reads of gateway, frontend, compose, and CLI, plus three specialist reviews (linked below). No production code was changed in this pass.

Detailed evidence:

- [Gateway review](../reviews/2026-09-16-core-stack-gateway-review.md)
- [Frontend review](../reviews/2026-09-16-core-stack-frontend-review.md)
- [Leftover worker/CLI review](../reviews/2026-09-16-core-stack-leftover-review.md)

---

## Verdict

The four-service core **boots and is internally consistent** on `docker compose up`: Elasticsearch is green, OpenBao is unsealed, the gateway serves `/health` and `/api/stack`, nginx on `:8765` serves a compile-time core SPA (~380 KB vs the 2.9 MB full dashboard). Chat, node registry, and routing-policy **reads** talk to the gateway, not to workers.

That is not yet a product. Two systems now share one repo, one CLI, one `/api` prefix, and one advertised port. The documented entrypoint (`flume start`) still boots the old mesh. The default console can change routing and spend LLM budget with no auth, cannot persist frontier keys, and sits in front of Vault `-dev` whose secrets die on restart.

Treat **Flume Core** (gateway + mesh + vault + search + console) as the product you just shipped, and **Flume Full** (dashboard + workers + Elastro) as an optional later profile. Until that split is explicit in CLI, compose, docs, and tests, every new feature will land in the wrong process.

---

## What remains

### On the default runtime (`docker compose up`)

| Piece | Role |
|---|---|
| `openbao`, `elasticsearch`, `gateway`, `frontend` | Core stack |
| `src/gateway/**` | Product process: chat, nodes, routing, stack health |
| `src/frontend` with `VITE_CORE_UI=true` | Overview / Nodes / Chat / Settings |
| `internal/es`, `internal/logger` | Pulled into the gateway image |

### Leftover, still compiled, not on default compose

| Piece | Approx. size | How it still runs |
|---|---|---|
| `internal/dashboard/**` | ~8.9k LOC | `flume start`, `--native`, profile `full` |
| `internal/worker/**` | ~6.4k LOC | same |
| `pkg/types` task FSM | ~700 LOC | dashboard/worker/dispatch only |
| `src/agents/**` | prompts | worker only; `FLUME_AGENTS_DIR` is unused |
| Root `Dockerfile` | python + elastro + logloom | profile `full` |
| CLI `start/destroy/upgrade/workers/tasks/dispatch/projects` | — | still the README path |

Compose already gates dashboard/worker with `profiles: [full]`. Almost nothing else was updated to match.

---

## Findings (severity as shipped, not as a lab demo)

### Critical — default path is broken or unsafe

1. **`flume start` does not start the core stack.**  
   It still passes `--profile managed_elastic` (removed), never `--profile full`, starts `dashboard,gateway,worker` (not `frontend`), then waits on `http://localhost:8765/api/health`. Core nginx proxies `/api/` to the gateway, which has `/health` and `/api/stack`, not `/api/health`. Vault init/unseal targets a file backend; compose OpenBao is HashiCorp Vault `-dev`.  
   *Files:* `cmd/flume/commands/start.go:217-394`, `cmd/flume/orchestrator/workers.go:95-97`, `cmd/flume/orchestrator/health.go:39-50`, `cmd/flume/orchestrator/vault.go:55-175`.

2. **Unauthenticated spend and control plane on published ports.**  
   Gateway registers `POST /v1/chat`, node CRUD, `PUT /api/routing-policy`, `POST /internal/level` with no `FLUME_ADMIN_TOKEN` (or any) check. nginx on `0.0.0.0:8765` proxies `/api/` and `/v1/` with no `auth_request`. Default Vault token `flume-dev-root` and ES password `flume-dev-elastic` are published on `:8200` and `:9200`.  
   *Files:* `src/gateway/server.go:110-122, 818-832`, `src/frontend/nginx.conf:11-42`, `docker-compose.yml:18,39,28-29,51-52,78`.

3. **Core Nodes UI cannot save frontier credentials.**  
   `AddFrontierModelModal` `POST`s `/api/settings/llm/credentials`. That route exists only on the Go dashboard. Gateway has `GET /api/frontier-models` (catalog) and no upsert. nginx 404s; the modal swallows the error. Core Settings copy claims keys live in OpenBao.  
   *Files:* `src/frontend/src/src/components/AddFrontierModelModal.tsx:58-66`, `internal/dashboard/server.go:242-243`, `src/gateway/server.go:818-832`.

### Major — will fail as soon as someone uses it as a gateway

4. **OpenBao `-dev` is ephemeral; Elasticsearch is not.** Secrets vanish on `compose restart openbao` while ES credential metadata remains. `config/openbao.hcl` (file storage) is unused. Image is `hashicorp/vault:1.15` under the name OpenBao.  
   *Files:* `docker-compose.yml:13-30`, `config/openbao.hcl`.

5. **`GET /skills` panics the gateway.** `NewServer` registers skill handlers with `s.skills == nil`. Closures capture that nil. `StartGateway` later assigns a real registry and logs `skills_loaded`, but the mux still holds nil. `SkillRegistry.Get`/`List` `RLock` the receiver.  
   *Files:* `src/gateway/server.go:120-122`, `src/gateway/skills/endpoint.go:28-54`.

6. **Alpine gateway image has no `ca-certificates`.** Local Ollama over HTTP works; OpenAI/Anthropic/Gemini/xAI HTTPS from the container fails with missing system roots. Comments claim distroless + wget; neither is true.  
   *Files:* `src/gateway/Dockerfile:32-43`.

7. **TLS verification is compile-time off** (`InsecureSkipVerify: true`) on every gateway HTTP client (ES, node health, telemetry, multi-node). No env opt-out.  
   *Files:* `src/gateway/config.go:141`, `node_registry.go:128`, `node_conn_manager.go:109`.

8. **`Config.Refresh` holds the write lock across five sequential ES GETs** (up to ~15s). Chat `ResolveModel` / routing share that mutex.  
   *Files:* `src/gateway/config.go:167-173`.

9. **Routing-policy UI can clobber ES.** A failed `GET /api/routing-policy` synthesizes `local_only` defaults; the first mode click `PUT`s that over the cluster.  
   *Files:* `src/frontend/src/src/pages/NodesOverview.tsx:493-504`.

10. **README / lifecycle / CLI still teach `flume start` and a Python dashboard at `:8765`.** Native mode still starts workers in-process. `flume doctor` probes HTTPS ES with no password and dashboard `/api/system-state`.  
    *Files:* `README.md:42-52`, `docs/operations/lifecycle.md`, `cmd/flume/commands/doctor.go`.

### Minor — quality bar for a core product

11. HealthChecker / FrontierProber use `context.Background()` and are not stopped on SIGTERM (`HealthChecker.Stop` exists, unused). Frontier prober `go`s a probe per model with no bound.  
12. Chat streaming path has no `recover`; embeddings skip the global semaphore and body cap.  
13. `NodesOverview` is still the 865-line full mesh page, statically imported into the core bundle; no CSP/security headers; frontend healthcheck hits `index.html` so nginx is “healthy” while the gateway is down.  
14. `App.tsx` duplicates core routes; tests still assert `/mission-control` etc.; Vite dev proxy has `/api` but not `/v1` (Chat 404s under `npm run dev`).  
15. Gateway only bootstraps two ES indexes; `flume-llm-config`, `flume-routing-policy`, `flume-settings`, telemetry/audit indexes are created on first write with ES 8 default replicas=1 (cluster goes yellow).  
16. Profile `full` dashboard binds host `8766` while `flume dispatch` also uses `:8766`. Compose `deploy.replicas` does not scale workers without Swarm. Worker prompts ignore `FLUME_AGENTS_DIR`.

### What is actually good (keep)

- Compile-time `VITE_CORE_UI` split and `App.core.tsx` not importing Mission Control / Projects / Queue.
- `/livez` vs `/readyz` vs `/api/stack` (degraded deps return HTTP 200 so Compose does not kill the gateway).
- Chat validation, tool sanitization, fail-closed API keys for managed providers, `Node.AuthToken` `json:"-"` and stripped before ES.
- Node ID/host validation (metadata IPs blocked), global + Ollama semaphores, 2 MiB body cap on chat.
- Index create for `flume-agent-models` and `flume-node-registry` with `number_of_replicas: 0`.
- Structured slog with secret-attribute redaction.
- nginx `proxy_buffering off` + 1200s read timeout matching gateway `WriteTimeout`.

---

## Product decision (do this first)

Pick one contract and write it at the top of README and compose:

**Flume Core (default):** OpenBao + Elasticsearch + Gateway + console. Boot: `docker compose up -d --build --wait`. UI: `http://127.0.0.1:8765`. CLI: `flume up` / `flume doctor` against `/api/stack` and `/health`.

**Flume Full (opt-in):** Core plus Go dashboard + workers + Elastro image. Boot: `docker compose --profile full up` **and** `flume start --full`. Dashboard API on a **different** host port than the core console. File-backed OpenBao, not `-dev`.

Until that is true in code, do not add features to `internal/worker` or the full SPA.

---

## Roadmap

### Phase 0 — Contract (days, not weeks)

Goal: one supported way to boot core; stop lying in docs and CLI.

| PR | Change |
|---|---|
| 0.1 | README + lifecycle + CLI docs: `docker compose up` is core; `flume start` marked unsupported until 0.2 |
| 0.2 | `flume up` / `flume doctor` / `flume destroy` talk to core (`/api/stack`, `/health`, images `flume-gateway:core` + `flume-frontend:core`). Remove `--profile managed_elastic` |
| 0.3 | Bind published ports to `127.0.0.1` by default; banner if default `flume-dev-*` secrets are in use |
| 0.4 | Rename image `flume-gateway:stub` → `:core`; drop duplicate `coreRoutes` from `App.tsx` |

**Exit:** A new clone following README gets a healthy four-service stack. `flume start` either boots that stack or refuses with a one-line pointer.

### Phase 1 — Safety (the FAANG bar for anything on a network)

Goal: a LAN host cannot spend keys or rewrite routing.

| PR | Change |
|---|---|
| 1.1 | Auth gate on mutating/spend routes: `FLUME_ADMIN_TOKEN` (already generated by CLI) on `POST /v1/*`, node CRUD, `PUT /api/routing-policy`, `/internal/level`, `/skills/*`. Leave `/livez` open. nginx injects the header for the SPA or uses `auth_request` |
| 1.2 | Do not proxy `/metrics` or `/api/gateway-metrics` on `:8765`. Add CSP, `nosniff`, `frame-ancestors 'none'`, `server_tokens off` |
| 1.3 | Create skill registry **before** mux registration; nil-guard handlers. Copy skill files into the image or 404 without panic |
| 1.4 | Gateway runtime: `apk add ca-certificates wget`, pin Alpine, non-root USER |
| 1.5 | `InsecureSkipVerify` only when `FLUME_TLS_INSECURE=1`; default verify on |
| 1.6 | Cancel a `runCtx` on SIGTERM; call `HealthChecker.Stop`, stop prober, `connMgr.CloseAll`. Make `Stop` idempotent |
| 1.7 | Frontend healthcheck `GET /health` (gateway), not `index.html` |

**Exit:** Unauthenticated `curl POST /v1/chat` from another host is 401. `GET /skills` is 200 or 404, never a crash. Cloud HTTPS from the gateway container works.

### Phase 2 — Core completeness (the UI must match the stack)

Goal: Nodes + Settings + Chat work without the dashboard process.

| PR | Change |
|---|---|
| 2.1 | Gateway `POST /api/credentials` (OpenBao KV, never echo `api_key`). Point `AddFrontierModelModal` at it; `type="password"`; show errors |
| 2.2 | Persistent OpenBao: file backend from `config/openbao.hcl`, volume, init/unseal sidecar or documented first-boot. Keep `-dev` as profile `ephemeral` |
| 2.3 | Gateway bootstraps `flume-llm-config`, `flume-routing-policy`, `flume-settings`, telemetry/audit indexes with `number_of_replicas: 0`. Fail `/readyz` if ensure-index fails in non-stub mode |
| 2.4 | Split Nodes: “local mesh” (register/test/delete) vs “frontier mix”. Disable mix controls until policy GET succeeds. Do not PUT synthesized defaults |
| 2.5 | Chat: `stream: true`, AbortController, disable send when `nodes==0` and no frontier mix, `aria-live` transcript |
| 2.6 | `safeFetchJson` on all core fetches; disable LogLoom `/api/telemetry/logs` in core; Vite proxy `/v1` to `:8090` |
| 2.7 | Tests: `App.core` routes, Chat POST, CoreSettings does not hit `/api/settings/*`, `stack_test` for **non-stub** `/api/stack`, compose smoke (`/api/stack` all `ok`) |

**Exit:** From the console, add an Ollama node, optionally save a frontier key that survives `compose restart`, chat, and see stack cards stay green.

### Phase 3 — Reliability inside the gateway

Goal: the process behaves like a small control plane, not a script.

| PR | Change |
|---|---|
| 3.1 | Fetch ES config off the write lock; swap fields under the mutex |
| 3.2 | Single ES HTTP helper (use `internal/es.Client` or one `esSetAuth`); delete per-file copies and the `"bypass"` drift |
| 3.3 | One `NodeConnManager` (delete `tool_stream.go` package singleton); implement or delete `FLUME_OLLAMA_*` env helpers |
| 3.4 | Embeddings use the same semaphore + max-bytes as chat; stream worker `recover` |
| 3.5 | Bounded frontier prober (no unbounded `go probeModel`); do not probe in `local_only` |
| 3.6 | `golangci-lint` + `go test -race` on `./src/gateway/...` in CI |

**Exit:** No request path holds a mutex across I/O. Race detector clean on gateway tests.

### Phase 4 — Isolate or retire Flume Full

Goal: worker/dashboard code stops contaminating core.

| PR | Change |
|---|---|
| 4.1 | `docker-compose.full.yml` (or `--profile full` that **stops** core `frontend` on 8765, or puts dashboard on a dedicated port documented as such) |
| 4.2 | `flume start --full` is the only path that starts dashboard+workers; it uses file OpenBao, generated secrets, Elastro image |
| 4.3 | Honor `FLUME_AGENTS_DIR`; fix empty `FLUME_ADMIN_TOKEN` opening kill-switch APIs; `handleVaultStatus` reads `OPENBAO_ADDR` |
| 4.4 | Delete or quarantine: `cmd/flume/agents/swarm.go`, `bootstrap.sh`, `Dockerfile.multistage`, `workspace/worker-manager/llm_credentials.json`, `scripts/verify_flume_ready.sh` as currently written, docs for `flume stop` |
| 4.5 | Optional later: move `internal/worker` + `internal/dashboard` + `src/agents` to a `full/` module or a sibling repo so core `go build ./src/gateway/cmd` has no worker graph |

**Exit:** `go test ./src/gateway/...` and core compose smoke do not import `internal/worker`. Full profile is documented as a second product.

### Phase 5 — Production overlay (only after 1–3)

- Real OpenBao image (or keep Vault, drop the OpenBao name), TLS listener, audit device, non-root token.
- ES HTTP TLS or loopback-only + API key, disk watermarks back on, memory limits.
- k8s manifests for the four services; probes use `/livez` and `/readyz`.
- Optional mTLS to Ollama nodes; pin CA instead of skip-verify.
- SLOs: gateway p99 chat queue wait, ES refresh duration, OpenBao KV errors.

Do not start Phase 5 while `flume start` is still the README default.

---

## Suggested PR order (first six)

These are independently reviewable and match the phases above:

1. **Docs + `flume up` against core** (Phase 0) — unblocks every operator.  
2. **Loopback bind + admin token on mutations** (Phase 1.1–1.2) — unblocks treating this as a default stack.  
3. **Skills mux nil + CA certs in gateway image** (Phase 1.3–1.4) — two real runtime bugs.  
4. **Gateway credential upsert + hide/fix frontier modal** (Phase 2.1) — unblocks Nodes as a product surface.  
5. **Persistent OpenBao + index bootstrap** (Phase 2.2–2.3) — unblocks secrets that survive restart.  
6. **Shutdown ownership + config refresh without I/O under lock** (Phase 1.6, 3.1) — reliability.

After those six, core is a small, honest LLM gateway. Full-mesh extraction (Phase 4) can proceed without blocking day-to-day use.

---

## Key decisions

1. **Core is the default product.** Workers are not “temporarily unplugged”; they are a second product. CLI and docs must say so.  
2. **One `/api` owner on `:8765`.** That owner is the gateway (via nginx). Dashboard, if revived, gets another port.  
3. **Secrets live in OpenBao and must persist.** `-dev` is a profile, not `docker compose up`.  
4. **Unauthenticated local-dev is an explicit overlay**, not the default published bind.  
5. **Do not delete worker/dashboard code in the first month.** Isolate it. Deletion comes after Full has a supported `--profile full` path or an explicit sunset date.

---

## Open questions (need a product call)

1. Is Flume Core a **local LLM router** (Ollama mesh + optional frontier) or still an **autonomous coding orchestrator** that will bring workers back in-tree? The roadmap above assumes router-first.  
2. Should OpenBao stay HashiCorp Vault `-dev` for DX, or is file-backed OpenBao required before any external user? Recommend file-backed before advertising secrets.  
3. Is `FLUME_ADMIN_TOKEN` enough auth, or do you want session cookies on the SPA? Token is enough for v1.  
4. Sunset date for `flume start` as currently implemented? Recommend: broken-as-error in Phase 0, replaced in 0.2, old behavior only under `--full`.
