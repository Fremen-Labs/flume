# Core stack leftover review

**Date:** 2026-09-16  
**Scope:** Leftover WORKER / DASHBOARD / CLI code after the default Docker stack became gateway + Elasticsearch + OpenBao + frontend only.  
**Method:** Read-only source review. No production code was modified.  
**Default runtime claim (compose header):** four services — OpenBao, Elasticsearch, Gateway, console — at `http://localhost:8765`. Full agent mesh is opt-in via Compose profile `full`.

---

## Inventory (runtime vs leftover)

### Default runtime path (`docker compose up`)

| Asset | Role | Evidence |
|---|---|---|
| `docker-compose.yml` services `openbao`, `elasticsearch`, `gateway`, `frontend` | Core stack. No profile gate. | `docker-compose.yml:12-104` |
| `src/gateway/**` + `src/gateway/Dockerfile` | LLM router, node mesh, `/api/stack`, `/v1/chat` | `src/gateway/server.go:110-122`, `818-832`; `src/gateway/Dockerfile` |
| `src/frontend/Dockerfile` (`VITE_CORE_UI=true`) + `src/frontend/nginx.conf` | Core console on `:8765`, reverse-proxy `/api` and `/v1` to gateway | `src/frontend/Dockerfile:12-14`; `src/frontend/nginx.conf:1-47`; `docker-compose.yml:86-104` |
| `src/frontend/src/src/App.core.tsx` + `main.tsx` core branch | Core routes: Overview / Nodes / Chat / Settings | `src/frontend/src/src/main.tsx:10-14`; `App.core.tsx:24-29` |
| `src/frontend/src/src/pages/{CoreOverview,CoreSettingsPage,GatewayChatPage,NodesOverview}.tsx` | Core UI surfaces | `CoreOverview.tsx:23-28` (`GET /api/stack`) |
| Gateway ES + OpenBao clients | Core persistence/secrets | `src/gateway/config.go:481-493`; `src/gateway/secrets.go:42-67`; `src/gateway/server.go:734-787` |
| `internal/es`, `internal/logger` | Built into gateway image | `src/gateway/Dockerfile:19-23` |
| `docker-compose.gateway.yml` | Alias that includes the core compose file (no longer a stub) | `docker-compose.gateway.yml:1-3` |

Core stack does **not** set `FLUME_GATEWAY_STUB`. Gateway therefore bootstraps ES + OpenBao (`src/gateway/server.go:737-787`). That is correct for the four-service product and must stay off stub mode.

### Leftover on the `flume` CLI path (still the documented boot)

`flume start` still orchestrates the **old full mesh**, not the four-service core:

| Asset | What it still does | Evidence |
|---|---|---|
| `cmd/flume/commands/start.go` | Wizard, random ES password, `--profile managed_elastic`, Vault init/unseal, then `dashboard` + `gateway` + `worker` | `start.go:39-407`, especially `217-223`, `304-311`, `381-394` |
| `cmd/flume/orchestrator/workers.go` | Service list is `dashboard`, `gateway`, `worker` — **no `frontend`** | `workers.go:95-97` |
| `cmd/flume/orchestrator/vault.go` | File-backend init + unseal + AppRole | `vault.go:55-175`, `505-529` |
| `cmd/flume/orchestrator/elastic.go` | Full index bootstrap for workers/dashboard | `elastic.go:111-120` |
| `cmd/flume/orchestrator/health.go` | Waits on `http://localhost:8765/api/health` (Go dashboard) | `health.go:39-50` |
| `cmd/flume/orchestrator/installer.go` + `recon.go` | Requires host Elastro CLI | `recon.go:23-31`; `installer.go:16-27` |
| `cmd/flume/services/supervisor.go` | Native mode starts gateway **and** dashboard **and** worker-manager | `supervisor.go:58-121` |
| `cmd/flume/commands/{destroy,upgrade}.go` | Same stale `managed_elastic` profile + dashboard/worker service names | `destroy.go:37`; `upgrade.go:65-67`, `116-140` |

Registered leftover subcommands (`cmd/flume/main.go:24-41`):

- `start`, `destroy`, `upgrade` — compose/orchestrator for the old mesh
- `dashboard`, `worker-manager` — standalone full-mesh processes (`commands/services.go:33-68`)
- `workers`, `tasks`, `dispatch`, `projects`, `logs`, `status`, `config` — HTTP clients against the Go dashboard on `:8765` (`ui/client.go:14-54`)
- `doctor` — probes dashboard `/api/system-state` + `/api/snapshot` and dispatcher `:8766` (`doctor.go:164-241`, `536-564`)

### Leftover packages (not on default compose, still compiled into `cmd/flume`)

| Package | Approx. size | Runtime? | Notes |
|---|---|---|---|
| `internal/worker/**` | ~6.4k LOC (`runner.go` 2380, `sweeps.go` 1189, `handlers.go` 844, …) | Only profile `full` / `flume start --native` / `flume worker-manager` | Task claim/FSM/sweeps |
| `internal/dashboard/**` | ~8.9k LOC (intake 1946, system 1822, tasks 1140, server 733, …) | Same | REST for projects/queue/intake/security |
| `pkg/types/**` | ~700 LOC | Shared by worker + dashboard + `dispatch` | Task FSM (`types.go:23-52`). Gateway does not import it. |
| `src/agents/**` | Role `SYSTEM_PROMPT.md` files | Worker only | `runner.go:2249-2257` reads `src/agents/<role>/…`; Dockerfile copies to `/app/agents` but **no Go code reads `FLUME_AGENTS_DIR`** |
| `cmd/flume/agents/swarm.go` | Stub | Dead | Comment still says “Python worker-manager” (`swarm.go:12-24`) |
| `cmd/flume/commands/dispatch.go` | DAG reconciler on `:8766` | Dead vs default stack | Collides with full-profile dashboard host port `8766:8765` |
| Root `Dockerfile` | python:3.12-slim + elastro + logloom | Profile `full` only | `Dockerfile:41-243`; compose `dashboard`/`worker` `dockerfile: Dockerfile` (`docker-compose.yml:109-114`, `151-156`) |
| `Dockerfile.multistage` | Distroless unified binary | Unused | `CMD ["start"]` (`Dockerfile.multistage:36-37`) |
| `config/openbao.hcl` | File storage + `tls_disable = 1` | Unused | Compose runs `server -dev` instead (`docker-compose.yml:16`) |
| `bootstrap.sh` | Waits then `exec python3 /app/bootstrap.py` | Unused | `bootstrap.sh:23`; no compose service references it |
| `scripts/verify_flume_ready.sh` | Hits dashboard `/api/health` + `/api/snapshot` | Full-mesh smoke | `verify_flume_ready.sh:8-16` |
| `workspace/worker-manager/llm_credentials.json` | Masked credential stub | Leftover host state | Present in tree |
| Frontend full mesh | `App.tsx` full routes, `Dashboard`, `MissionControl`, `Projects`, `Queue`, `Analytics`, `Security`, `Settings`, `IntakeModal`, worker hooks | Built only when `VITE_CORE_UI` is unset (root `Dockerfile` frontend stage) | `App.tsx:34-46`; `Dockerfile:11-18` does **not** pass `VITE_CORE_UI` |

### Dual-mode UI (same SPA, two builds)

- Core image: `VITE_CORE_UI=true` → `App.core.tsx` (`src/frontend/Dockerfile:12-14`, `main.tsx:10-12`).
- Full image: root `Dockerfile` frontend stage has no `VITE_CORE_UI` → `App.tsx` full routes.
- `App.tsx` also contains a duplicate `coreRoutes` tree (`App.tsx:25-32`) that `main.tsx` never loads when `VITE_CORE_UI=true`. Dead duplication.

### What “profile `full`” actually starts

`dashboard` and `worker` are gated (`docker-compose.yml:107-180`). Default `frontend` **still starts** on host `8765`. Full dashboard is published at **host `8766` → container `8765`** (`docker-compose.yml:140`). Result if both run: two UIs, two API dialects, same hostname family.

Compose comment claims profile `full` is “for flume start” (`docker-compose.yml:5`), but `flume start` never passes `--profile full`.

---

## Summary

The compose file was split into a four-service **core** and an opt-in **full** profile. Almost nothing else was. The documented entrypoint (`README.md` / `docs/operations/lifecycle.md` / `docs/reference/cli.md`) is still `flume start`, which boots a product that no longer exists on the default compose graph.

Concretely:

1. **`docker compose up` is the only working core boot path**, and even that path cannot persist frontier credentials from the console (the modal posts to a dashboard-only API).
2. **`flume start` is broken against the new compose file** (unknown profile `managed_elastic`, no `--profile full`, no `frontend`, OpenBao `-dev` vs init/unseal, health probe `/api/health` 404s through nginx).
3. **Two products share one binary, one port (`8765`), and one `/api` prefix**, so CLI tools and leftover UI pages talk to the wrong process depending on which stack is up.
4. Worker/dashboard/Elastro remain large, real code — not stubs. They belong behind a maintained `full` profile (or a separate repo), not on the default path and not half-wired.

Until CLI, compose profiles, OpenBao topology, and docs are aligned, “core vs full” is an unstated dual-mode product with conflicting security models (ephemeral `-dev` Vault + hardcoded tokens vs generated admin token + file unseal that is never mounted).

---

## Issues

### Critical

1. **`flume start` enables a Compose profile that no longer exists, and never enables `full`.**  
   `start.go:217-223`, `304-311`, `391-394`; `upgrade.go:116`, `140`; `destroy.go:37`.  
   Compose profiles in-tree are only `full` (`docker-compose.yml:108`, `150`). Elasticsearch is always-on (`docker-compose.yml:32-50`), not `managed_elastic`.  
   **Effect:** first `compose up elasticsearch openbao` may still start those two default services; the second `compose up dashboard gateway worker` **skips** `dashboard` and `worker` (profile not enabled) and **never starts `frontend`**. `BuildWorkerServiceNames` returns `{"dashboard","gateway","worker"}` (`workers.go:95-97`).

2. **`flume start` health gate waits for the old dashboard health URL, which the core console does not implement.**  
   `health.go:39-50` polls `http://localhost:8765/api/health` and prints “Dashboard & Workers globally synchronized.”  
   Core frontend nginx proxies `/api/` to the gateway (`nginx.conf:11-18`). Gateway has `GET /health` and `GET /api/stack`, **not** `GET /api/health` (`server.go:113`, `818` vs dashboard `server.go:181` / `api_system.go:92-97`).  
   **Effect:** after a “successful” data-grid boot, `AwaitOrchestration` 404s or connection-refuses for 60s and `flume start` returns `timeout awaiting flume ecosystem convergence`.

3. **OpenBao topology fork: compose is HashiCorp Vault `-dev`; CLI still does file init/unseal.**  
   Compose: `image: hashicorp/vault:1.15`, `command: server -dev`, `VAULT_DEV_ROOT_TOKEN_ID: ${OPENBAO_TOKEN:-flume-dev-root}`, **no volume** (`docker-compose.yml:13-30`).  
   Unused file config: `config/openbao.hcl:1-8`.  
   CLI: `DeployVaultTopology` → `InitializeAndUnseal` (`vault.go:505-516`, `55-175`). On an already-initialized unsealed `-dev` server, health 200 takes the “orphaned Vault” branch and calls `GenerateRootToken` (`vault.go:113-123`) which requires unseal-key shares `-dev` does not persist.  
   **Effect:** `flume start` Vault deploy fails against the default compose OpenBao, or mints a token that is not `VAULT_DEV_ROOT_TOKEN_ID`. First compose already started Vault with default `flume-dev-root` *before* `OPENBAO_TOKEN` is injected (`start.go:313-367` vs `364-367`).

4. **Port 8765 is now the core nginx console; leftover CLI and native dashboard still claim it as the Go API.**  
   Core: `"${DASHBOARD_PORT:-8765}:8765"` on `frontend` (`docker-compose.yml:97-98`).  
   Full dashboard: `"8766:8765"` (`docker-compose.yml:140`).  
   CLI client fallback is `http://localhost:8765` and probes `/api/health` (`ui/client.go:46-54`).  
   Native supervisor binds dashboard to `DASHBOARD_PORT` default 8765 (`internal/config/config.go:108`; `internal/dashboard/server.go:84`).  
   Dispatcher listens `:8766` (`dispatch.go:94-101`), colliding with the full-profile host mapping.  
   **Effect:** `flume status|tasks|workers|logs|projects|config` against a core stack hit nginx→gateway and 404. Native `flume start -n` fights the core frontend for 8765.

5. **Core console cannot save frontier credentials — the write path is dashboard-only.**  
   `AddFrontierModelModal.tsx:58-66` `POST /api/settings/llm/credentials`.  
   That route exists only on the Go dashboard (`internal/dashboard/server.go:242-243`, `api_settings.go:146-148`).  
   Gateway exposes `GET /api/frontier-models` (read catalog + cached creds) (`server.go:832`, `1340-1343`) but no credential upsert.  
   **Effect:** default-stack Nodes UI “add key” always 404s. Core product cannot persist LLM keys except via `LLM_API_KEY` env on the gateway container.

### Major

6. **README / lifecycle / CLI docs still teach `flume start` as the only boot, targeting the old mesh.**  
   `README.md:42-52` (“Boot the Matrix”, dashboard at `:8765`); `docs/operations/lifecycle.md:23-31`; `docs/reference/cli.md:12` (“Python UI backends”).  
   Compose itself documents `docker compose up` + `open http://localhost:8765` (`docker-compose.yml:7-9`).  
   Users following README will not get the core stack, and will hit issues 1–4.

7. **`flume start` still requires host Elastro, which the core stack does not use.**  
   `EvaluateAndInstall` fails closed if Elastro is missing (`installer.go:16-35`, ` recon.go:27`).  
   Elastro is installed only in the heavy `Dockerfile` (`Dockerfile:68-89`) for workers.

8. **Native mode (`flume start --native`) still starts workers + Go dashboard in-process.**  
   `start.go:281-299`; `supervisor.go:58-121`.  
   `StartGateway` ignores the supervisor context and installs its own SIGTERM handler (`supervisor.go:179-187`; `src/gateway/server.go:149-152`). CLI `main.go:59-66` then `os.Exit(1)` on SIGINT, so the 25s drain in `supervisor.go:133-151` is skipped.

9. **`flume destroy` / image purge do not match current images or profiles.**  
   `destroy.go:37` uses `--profile managed_elastic` (undefined), not `full`. Compose `down` without the profile that started `dashboard`/`worker` can leave those containers running.  
   Purge removes `flume-dashboard`, `flume-gateway`, `flume-worker` (`destroy.go:58`). Actual image names: `flume-gateway:stub`, `flume-frontend:core` (`docker-compose.yml:56`, `92`). Dashboard/worker have no `image:` key.

10. **OpenBao `-dev` is ephemeral; Elasticsearch is not. Secret/state split-brain on every restart.**  
    ES volume `es_data` (`docker-compose.yml:43-44`, `182-183`). OpenBao has no volume.  
    Gateway hydrates `secret/data/flume/keys` and per-credential paths (`secrets.go:18-21`, `109-111`). Those vanish on container recreate while ES credential *metadata* remains. Core UI copy even advertises “Dev token is injected at compose time” (`CoreOverview.tsx:88`).

11. **Hardcoded core credentials published on the host.**  
    `OPENBAO_TOKEN` default `flume-dev-root` (`docker-compose.yml:18`, `68`).  
    `FLUME_ELASTIC_PASSWORD` default `flume-dev-elastic` (`docker-compose.yml:39`, `66`).  
    Ports `8200`, `9200`, `8090`, `8765` published. Vault UI enabled via `-dev`.  
    Contrast: `flume start` generates `flume_adm_*` and `flume_es_*` (`env.go:61-77`) that the core compose path never uses.  
    Full-profile `FLUME_ADMIN_TOKEN` is empty-by-default, and empty token **opens** kill-switch APIs (`api_security.go:234-238`).

12. **Unauthenticated gateway mutation surface on the core console proxy.**  
    nginx forwards all `/api/` and `/v1/` (`nginx.conf:11-28`).  
    `POST /api/nodes`, `DELETE /api/nodes/{id}`, `PUT /api/routing-policy`, `POST /v1/chat` have no admin token (`server.go:819-831`, `1002+`).  
    Anyone who can reach `:8765` (or `:8090`) can register nodes and spend configured provider keys.

13. **Two `/api/nodes` implementations if profile `full` is combined with default frontend.**  
    Gateway: `server.go:819-822`. Dashboard: `internal/dashboard/server.go:228-231`.  
    Frontend on 8765 talks to gateway; dashboard SPA on 8766 talks to itself. Node mesh can diverge.

14. **`deploy.replicas` will not scale workers on Compose without Swarm.**  
    `docker-compose.yml:179-180` `replicas: ${FLUME_WORKER_COUNT:-2}`.  
    `start.go:383-386` sets `FLUME_WORKER_COUNT`. On Docker Compose (non-swarm) `deploy` is ignored, so “2 workers” is a single container.

15. **Worker system prompts are copied to `/app/agents` but read from `src/agents/` relative to CWD.**  
    `Dockerfile:232-235` `FLUME_AGENTS_DIR=/app/agents`.  
    `runner.go:2249-2257` `os.ReadFile("src/agents/%s/SYSTEM_PROMPT.md")` — env var is never read (repo-wide). Full-profile agents fall back to truncated inline prompts.

16. **`handleVaultStatus` reads `VAULT_ADDR`, but compose sets `OPENBAO_ADDR`.**  
    `api_security.go:142-149` vs `docker-compose.yml:125`. Full-profile `/api/vault/status` always “not configured”. `flume status` then treats vault as sealed (`status.go:75-79`).

17. **`flume doctor` defaults are the old topology.**  
    ES `https://localhost:9200` (`doctor.go:562`) but compose ES is HTTP with basic auth and TLS off (`docker-compose.yml:38-42`). `fetchElasticsearch` sends no password (`doctor.go:148-161`). Dashboard probes `/api/system-state` (`doctor.go:170`). Dispatcher `:8766` suggested via `flume dispatch` (`doctor.go:240`). Core stack is reported as sick even when healthy.

18. **Credential write from core Nodes UI + ephemeral Vault = silent data loss even after a write API is added.** Until OpenBao has a volume (or is replaced by a real server using `config/openbao.hcl`), keys cannot survive `compose down` / crash.

### Minor

19. **Gateway image tag `:stub` is leftover from the gateway-only experiment.** `docker-compose.yml:56` `image: flume-gateway:stub`. Stub env is correctly *not* set.

20. **Service named `openbao` runs `hashicorp/vault:1.15`, not an OpenBao image.** `docker-compose.yml:13-16`. Product/docs say OpenBao. License and image provenance do not match.

21. **Gateway Dockerfile comments claim wget is installed; the runtime stage never `apk add wget`.** `src/gateway/Dockerfile:35-43`. Relies on Alpine busybox `wget` for compose healthcheck (`docker-compose.yml:80`). Fragile if the base image changes; `Dockerfile.multistage` is distroless and would fail that healthcheck.

22. **Dashboard CORS defaults omit `:8766` and include Vite `:8080`.** `internal/dashboard/server.go:99`. Full-profile UI on 8766 is a cross-origin caller if the SPA is ever hosted separately.

23. **`App.tsx` duplicates core routes that `App.core.tsx` already owns.** `App.tsx:25-32` vs `App.core.tsx`. `main.tsx` picks one module; the duplicate is dead in production core builds.

24. **Playwright `baseURL` is `http://localhost:8080`.** `playwright.config.ts:7`. Core console is `:8765`. No compose/core e2e.

25. **`scripts/verify_flume_ready.sh` and `bootstrap.sh` are leftover full-mesh/Python bootstrap.** `verify_flume_ready.sh:8-16`; `bootstrap.sh:23`.

26. **`cmd/flume/agents/swarm.go` is a no-op that still documents Python worktrees.** `swarm.go:12-24`.

27. **Root `Dockerfile` CMD is `["start", "--native"]`**, which inside a container would try to run Docker Compose (`Dockerfile:242-243`; `start.go:212-223`). Compose overrides with `["dashboard"]` / `["worker-manager"]`, so only a raw `docker run` of the heavy image explodes.

28. **`flume config set-provider` PUTs `/api/settings/llm` to the dashboard client.** `config.go:116-126`. No core equivalent.

29. **TLS `InsecureSkipVerify: true` on ES/Vault/node clients** across CLI and gateway (`start.go:246`, `config.go:141`, `elastic.go:50`, …). Acceptable for local HTTP ES, not for a “KMS” story.

30. **`docs/reference/cli.md` documents `flume stop`, which is not registered.** `cli.md:15` vs `main.go:24-41`.

31. **Worker health server binds `:8080` with no compose mapping or healthcheck.** `manager.go:495-506` vs `docker-compose.yml:149-180`.

32. **Frontend tests still assert the full route registry** (`App.test.tsx:32-40`: `/mission-control`, `/projects`, `/queue`, …) and have no `App.core` / `GatewayChatPage` / `CoreSettingsPage` tests. `CoreOverview.test.tsx` mocks `/api/stack` only.

### Nit

33. Supervisor comment line numbers for `start.go` are stale (`supervisor.go:60-61`).

34. README badges still say “Backend: Python 3.9+” (`README.md:8`) and “Python Workers” (`README.md:19`).

35. `docker-compose.gateway.yml` filename implies stub; it is now an include alias.

36. `gateway.StartGateway` log version is hardcoded `"1.0.0"` (`server.go:739`).

37. `PrintDeploymentSummary` always reports workers + dashboard port (`start.go:472-498`; `theme.go:193-194`).

38. `workspace/worker-manager/llm_credentials.json` leftover should not live in the repo even masked.

---

## Recommended extraction/deletion list

Treat **core** and **full** as two products that share `src/gateway` + ES/OpenBao clients. Do not leave full-mesh types on the default boot path.

### Keep on the core path (fix in place)

- `docker-compose.yml` services `openbao`, `elasticsearch`, `gateway`, `frontend`
- `src/gateway/**`, `src/gateway/Dockerfile` (rename image from `:stub`)
- `src/frontend/Dockerfile`, `nginx.conf`, `App.core.tsx`, core pages/hooks that call gateway APIs
- Gateway `/api/stack`, `/api/nodes`, `/api/routing-policy`, `/api/frontier-models`, `/v1/*`
- **Add** gateway (or a tiny core API) `POST /api/settings/llm/credentials` *or* stop rendering `AddFrontierModelModal` in core UI
- `internal/es`, `internal/logger` as used by gateway
- A **core-aware** CLI: `flume compose up` / `flume doctor` against `/api/stack` + `GET /health`, not `/api/health`

### Keep as optional profile `full` (extract or isolate, do not delete yet)

These are still real agent-mesh code (~15k+ LOC) and should move behind `--profile full` **and** a CLI flag (`flume start --full`), or a `docker-compose.full.yml`:

- `internal/worker/**`, `internal/dashboard/**`
- `pkg/types/**` (task FSM — only if full remains in this repo)
- `src/agents/**` (and actually honor `FLUME_AGENTS_DIR`)
- Root `Dockerfile` (python/elastro/logloom)
- Compose services `dashboard`, `worker` (already profiled)
- CLI: `flume dashboard`, `flume worker-manager`, `flume workers`, `flume tasks`, `flume dispatch`, `flume projects`
- Orchestrator: `vault.go` file-unseal, `elastic.go` full index set, `workers.go` replica resolution — but only after OpenBao is **not** `-dev`

Required wiring if `full` stays:

1. `flume start --full` passes `--profile full` (not `managed_elastic`).
2. Core `frontend` must not bind 8765 when the Go dashboard does (or stop starting `frontend` in full mode).
3. Replace `-dev` Vault with `config/openbao.hcl` + volume when full (or always).
4. Document host ports: console 8765 vs dashboard API 8766 vs gateway 8090.

### Delete or quarantine (dead vs both modes)

| Item | Why |
|---|---|
| `cmd/flume/agents/swarm.go` | No-op, stale Python comments |
| `bootstrap.sh` | Python `/app/bootstrap.py` not in compose |
| `Dockerfile.multistage` | Unused; `CMD start` wrong for core |
| `config/openbao.hcl` **or** compose `-dev` | Pick one topology; the file is currently orphaned |
| `managed_elastic` references in `start.go`, `destroy.go`, `upgrade.go` | Profile does not exist |
| Duplicate `coreRoutes` in `App.tsx` | `App.core.tsx` is the real core tree |
| `workspace/worker-manager/llm_credentials.json` | Leftover state |
| Docs claiming Python dashboard / `flume stop` / metrics on 8766 | Drift |
| `scripts/verify_flume_ready.sh` as-is | Rewrite against `/api/stack` + `/health` or move under `full/` |

### Rewrite (do not keep behavior)

- **`flume start` default** should mean `docker compose up` of the four core services (optionally generate/print the dev token/password, seed gateway nodes). Worker count, Elastro, AppRole, dashboard health, and init/unseal do not belong on the default path.
- **`flume destroy`** should `docker compose --profile full down -v` (or `down` all profiles) and prune `flume-gateway:stub` + `flume-frontend:core`.
- **`ui.FlumeClient`** should prefer `GET /api/stack` / `GET /health` on `:8765`, with dashboard `:8766` only when `FLUME_MODE=full`.

---

## Residual risks

1. **Docs vs compose vs CLI will keep shipping two truths.** README `flume start` vs compose `docker compose up` is already a support incident generator. Until one is canonical, security reviews and onboarding will target the wrong attack surface (Python workers vs nginx console).

2. **Secret durability.** Core OpenBao `-dev` + published root token is fine only if the product is explicitly “local demo, secrets die on restart.” The UI and gateway code treat OpenBao as a real KMS (`secrets.go` header). Mixing that with ES persistence will strand credential metadata and confuse operators who “lost keys after reboot.”

3. **Auth gap on the core proxy.** Putting `/v1/chat` and `/api/nodes` behind unauthenticated nginx on `:8765` is a cost and SSRF foot-gun (node host validation allows loopback — `server.go:1238-1244`). Full-profile admin token is also off by default (`api_security.go:236-238`).

4. **Image and dependency supply chain on `full`.** The heavy Dockerfile installs elastro/logloom from PyPI/GitHub during build (`Dockerfile:154-183`) and curls a remote install script from the CLI (`installer.go:47`). That path must not be reachable from `docker compose up` of core, and should be pinned if `full` remains.

5. **No automated test proves the core path.**  
   - `stack_test.go` only covers **stub** mode (`FLUME_GATEWAY_STUB=1`) skipping ES/OpenBao — the opposite of production core.  
   - No test asserts compose profiles, `VITE_CORE_UI` routing, nginx `/api/stack` proxy, or `flume start` service list.  
   - `internal/e2e/cli_test.go` only builds `--version`/`help`.  
   - Frontend `App.test.tsx` still encodes the full mesh.  
   A single `docker compose config --services` test (expect `openbao elasticsearch gateway frontend`, not `dashboard worker`) plus an httptest that `GET /api/stack` is registered without stub mode would have caught most of this.

6. **Extraction hazard.** `cmd/flume` links worker, dashboard, and gateway into one binary (`supervisor.go:26-29`, `Dockerfile:4-8`). Deleting leftover packages without splitting the module will not shrink the core gateway image (already a separate `src/gateway/cmd` build) but **will** break `flume start --native` and profile `full`. Extract `internal/worker` + `internal/dashboard` behind a Go build tag (`full`) or a second module before deleting.

7. **Operator mix-up of modes.** Running core, then `flume start --native`, then `--profile full` on the same Docker project will contend for 8765/8090/8200/9200, leave orphan workers, and point CLI clients at whichever process last bound 8765. There is no mode lock file or compose project name separation.

---

*Reviewer note: citations are 1-indexed file lines as read on 2026-09-16. Compose and CLI were not executed in this pass; Docker Compose profile semantics and Vault `-dev` generate-root behavior are inferred from the Compose spec and HashiCorp Vault `-dev` contract plus the in-repo control flow.*
