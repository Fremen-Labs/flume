# Code Review: Flume core-stack frontend (post-worker default)

**Review scope**: `src/frontend/**` after workers/dashboard left the Compose default. Focus on `App.tsx` / `App.core.tsx` / `main.tsx`, `AppLayout`, `CoreOverview`, `GatewayChatPage`, `CoreSettingsPage`, `NodesOverview`, `nginx.conf`, `Dockerfile`, `vite.config.ts`, `safeFetch`, and hooks that still hit dashboard APIs (`/api/snapshot`, `/api/settings`, `/api/system-state`). Cross-checked against gateway routes in `src/gateway/server.go` (the nginx upstream). Source was not modified.

**Date of review**: 2026-09-16

## Summary

The core-console split is real and mostly pointed at the right process. `main.tsx` dynamically imports `App.core.tsx` when `VITE_CORE_UI=true`, that module only mounts Overview / Nodes / Chat / Settings, and nginx on `:8765` reverse-proxies `/api/` and `/v1/` to `gateway:8090` with buffering off and a 1200s read timeout that matches the gateway write timeout. Core Overview talks to `GET /api/stack` (a gateway handler), Nodes talks to `/api/nodes` + `/api/routing-policy` + `/api/frontier-models` (also gateway), and Chat posts to `POST /v1/chat`. `AppLayout` gates the snapshot error banner and the full-mesh nav behind `isCoreUI`.

The remaining UI is not yet a core product. Three problems dominate:

1. **The Nodes page still performs a dashboard-only credential write.** `AddFrontierModelModal` `POST`s `/api/settings/llm/credentials`. That route exists on the Go dashboard (`internal/dashboard/server.go:243`) and **does not exist on the gateway**. Under the core stack nginx sends it to the gateway, which 404s. Operators cannot save frontier API keys from the default UI.
2. **nginx publishes the unauthenticated gateway surface on the public console port**, including mutating node CRUD, routing-policy writes, `/v1/chat`, Prometheus `/metrics`, and `/api/gateway-metrics`. There is no CSP, no security headers, and the image healthcheck only hits the SPA.
3. **Tests and `App.tsx` still describe the full dashboard.** `App.test.tsx` enumerates `/mission-control`, `/projects`, `/queue`, etc., and never imports `App.core`. Vitest does not set `VITE_CORE_UI`. There are no tests for `GatewayChatPage`, `CoreSettingsPage`, or the core router. `NodesOverview` is an 865-line statically imported mesh console with no route-level code splitting.

Verdict: the core build will boot and Overview/Chat can talk to the gateway; Nodes' frontier-key flow and the nginx security posture will not hold up as the default stack.

## Issues

### Issue 1 -- Severity: critical
- File: src/frontend/src/src/components/AddFrontierModelModal.tsx:58
- Description: Adding a frontier model from the core Nodes page posts `{ action: 'upsert', provider, label, apiKey }` to `/api/settings/llm/credentials`. That handler is registered only on the dashboard (`internal/dashboard/server.go:243`, `api_settings.go:144`). Gateway `NewServer` / `ListenAndServe` register `/api/nodes`, `/api/routing-policy`, `/api/frontier-models`, and `/v1/chat` — there is no `/api/settings/*` on `src/gateway/server.go`. Core nginx (`nginx.conf:11-18`) sends all `/api/` to the gateway, so the save 404s. The catch at L85-88 logs and clears `isSubmitting` but never renders an error, so the operator sees the button recover with no explanation. Core Settings copy (`CoreSettingsPage.tsx:59-60`) claims "Frontier keys are stored in OpenBao; the gateway reads them at request time" while this UI cannot write them.
- Suggestion: Add a gateway credential upsert (OpenBao-backed, never echoing the raw key) and point this modal at it; or hide "Add New API Key" / the whole frontier mix in `VITE_CORE_UI` until that exists. Surface `res.ok` failures in the modal. Do not keep a dashboard-only path in a page that ships in the core bundle.
- Status: open

### Issue 2 -- Severity: critical
- File: src/frontend/nginx.conf:11
- Description: `location /api/` and `location /v1/` proxy to `http://gateway:8090` with no auth_request, no allow-list, and no method filter. Compose publishes this as `${DASHBOARD_PORT:-8765}:8765` on all interfaces. Anyone who can reach the console can: `POST /v1/chat` (spend / local inference), `POST`/`DELETE /api/nodes` and `POST /api/nodes/{id}/test` (register hosts and make the gateway probe them — `isValidNodeHost` in `server.go:1238-1242` only blocks GCP metadata, not RFC1918 or public IPs), and `PUT /api/routing-policy`. `/metrics` (`nginx.conf:40-42`) and `/api/gateway-metrics` (caught by `/api/`) are likewise public. The previous dashboard at least sat behind the Go process; this nginx is a raw gateway expose.
- Suggestion: Do not proxy `/metrics` or `/api/gateway-metrics` from the console. Restrict `/api/` and `/v1/` to the verbs the UI needs. Add a shared secret / session (even a compose-injected `FLUME_ADMIN_TOKEN` header) before this is a default `docker compose up`. Bind `127.0.0.1:8765` in the Compose example if the console is local-only.
- Status: open

### Issue 3 -- Severity: major
- File: src/frontend/nginx.conf:1
- Description: The server block has no `add_header` at all (confirmed: no `Content-Security-Policy`, `X-Content-Type-Options`, `X-Frame-Options`/`frame-ancestors`, `Referrer-Policy`, `Permissions-Policy`, or `server_tokens off` under `src/frontend/`). `index.html` also has no CSP meta. The SPA is a static origin that can call `/v1/chat` and paste API keys into the DOM (`AddFrontierModelModal.tsx:278-284`, `type="text"`). A single XSS (dependency or `dangerouslySetInnerHTML` later) has no CSP backup. Clickjacking the Nodes "Remove" / routing-mode controls is unblocked.
- Suggestion: Add a strict CSP (`default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'` as the starting point given Tailwind), `X-Content-Type-Options nosniff`, `Referrer-Policy no-referrer`, `X-Frame-Options DENY` (or CSP `frame-ancestors 'none'`), and `server_tokens off`. Keep nonce/hash work off the critical path; even a coarse CSP is better than none.
- Status: open

### Issue 4 -- Severity: major
- File: src/frontend/src/src/pages/NodesOverview.tsx:1
- Description: The core `/nodes` route statically imports an 865-line mesh console plus `RoutingModeSelector`, `FrontierModelCard`, `HybridTuner`, `RolePinningPanel`, and `AddFrontierModelModal`. There is no `React.lazy` anywhere under `src/frontend/src/src/`. `App.core.tsx:8-11` imports this module at the top level, so it lands in the main chunk with Overview and Chat. `vite.config.ts:25-28` has no `manualChunks`. Polling is 15s (`['nodes']`) and 30s (`['routing-policy']`) for the lifetime of the page. Unused imports (`useEffect` L1, `Zap` L6) show the file was preserved rather than re-cut for core. This is the size/performance risk for the ~380KB core claim: Overview is small; Nodes is the old full-mesh page.
- Suggestion: Lazy-load `NodesOverview` from `App.core`. Split "local node registry" from "frontier routing command center" (or gate frontier UI until credential writes work — Issue 1). Drop unused imports. Consider a 30s+ interval when `document.hidden`.
- Status: open

### Issue 5 -- Severity: major
- File: src/frontend/src/src/App.tsx:8
- Description: `main.tsx:10-12` correctly loads `./App.core` vs `./App`, but `App.tsx` still statically imports Dashboard, Mission Control, Projects, Queue, Analytics, Security, and the full `SettingsPage` (L9-20) **and** duplicates the core route table (L25-32) behind `isCoreUI`. `App.core.tsx` is a second copy of that core table (L24-29). Any route added to one file and not the other will drift (today they match). Importing `App` in a core build (or forgetting `VITE_CORE_UI` on the Docker ARG) pulls the 2.9MB dashboard graph. `Index.tsx:1` still re-exports `Dashboard`.
- Suggestion: Make `App.tsx` full-dashboard-only (delete `coreRoutes` / `isCoreUI` there). Keep `App.core.tsx` as the only core router. Add a CI grep or bundle test that `mission-control` / `MissionControlPage` does not appear in the core JS. Do not keep a third `isCoreUI` route list.
- Status: open

### Issue 6 -- Severity: major
- File: src/frontend/src/src/test/App.test.tsx:32
- Description: The App "route registry" still requires `/mission-control`, `/projects`, `/queue`, `/analytics`, `/security` and does not mention `/chat`. The `it.each` body is a no-op (`expect(() => {}).not.toThrow()` at L46-49). Page-import tests load Dashboard, MissionControl, SettingsPage, etc., and never `App.core`, `CoreOverview` (beyond its own file), `GatewayChatPage`, or `CoreSettingsPage`. Vitest does not set `VITE_CORE_UI` (`vitest.config.ts`). `NodesOverview.test.tsx:43-47` mocks `GET /api/frontier-models` as `{ catalogs: [] }` while both the gateway (`server.go:1322`) and `types/index.ts:604-606` use `{ providers: [...] }`. `SettingsPage.test.tsx:33` still drives `/api/settings/llm`. CI can go green while the default Compose UI is broken.
- Suggestion: Add `App.core` tests that `/`, `/nodes`, `/chat`, `/settings` render and `/mission-control` 404s under `VITE_CORE_UI=true`. Delete or gate the full-dashboard route list. Fix the frontier-models fixture to `providers`. Add a Chat test that `POST /v1/chat` is called, and a CoreSettings test that `/api/settings/*` is not.
- Status: open

### Issue 7 -- Severity: major
- File: src/frontend/src/vite.config.ts:13
- Description: Vite dev proxies `/api` and `/ws` to `http://127.0.0.1:8765` and does **not** proxy `/v1`. `GatewayChatPage.tsx:33` calls `fetch('/v1/chat')`. `npm run dev` (port 8080, `vite.config.ts:9`) therefore 404s Chat unless the developer happens to hit nginx. `/ws` is proxied, but core nginx has no `/ws` location (gateway has no websocket upgrade path in `server.go` either). `server.host: "::"` binds all interfaces. `hmr.overlay: false` hides compile errors.
- Suggestion: Proxy `/v1`, `/health`, `/livez`, `/readyz` to the gateway (`8090`) in core-dev, not to the dashboard port. Drop `/ws` or document it as full-stack only. Bind `localhost` for dev. Turn the HMR overlay back on.
- Status: open

### Issue 8 -- Severity: major
- File: src/frontend/src/src/pages/NodesOverview.tsx:493
- Description: If `GET /api/routing-policy` fails, `policy` is undefined and the page synthesizes `{ mode: 'local_only', frontier_mix: [], ... }` (`L493-499`). There is no `isError` UI for that query (only `['nodes']` errors at L790-795). `RoutingModeSelector` still mounts (`L611-616`). The first click calls `savePolicyMut.mutate(updated)` (`L501-504`) and **PUTs that default over Elasticsearch**. A transient policy GET failure plus one click clobbers production routing. Debounced weight/budget edits (`L485-490`) are not cleared on unmount, so a slow PUT can also fire after navigation.
- Suggestion: Distinguish "no policy yet" from "fetch failed". Disable mode/mix controls while `isError` or `isLoading`. Do not PUT until a successful GET (or a 404-from-empty that the gateway documents). Clear `debounceRef` on unmount.
- Status: open

### Issue 9 -- Severity: major
- File: src/frontend/src/src/utils/safeFetch.ts:23
- Description: `safeFetchJson` exists specifically because HTML 502/404 bodies blow up `.json()`. Core pages do not use it: `CoreOverview.tsx:24-29`, `CoreSettingsPage.tsx:9-12`, `NodesOverview.tsx:76-137`, `GatewayChatPage.tsx:33-42` all `fetch` + `res.json()`. Gateway errors from `http.Error` are `text/plain`; nginx 502s are HTML. Chat's `res.json().catch(() => ({}))` then reports `HTTP 502` with no body hint. Production logger transport (`logger.ts:45-48, 62`) `POST`s `/api/telemetry/logs` whenever `import.meta.env.PROD` — another dashboard-only path that 404s on the gateway on every 5s flush. `useSnapshot` (`hooks/useSnapshot.ts:8`) and `useSystemState` (`hooks/useSystemState.tsx:35`) remain in the repo and still hit `/api/snapshot` and `/api/system-state`; `AppLayout.tsx:9,110` still imports `SnapshotErrorBanner` (DCE-dependent on `isCoreUI` being inlined).
- Suggestion: Route all core JSON through `safeFetchJson`. Disable LogLoom transport when `isCoreUI` (or no-op if `/api/telemetry/logs` is absent). Keep `useSnapshot` / `useSystemState` out of the core module graph with a core-specific layout that does not import the banner.
- Status: open

### Issue 10 -- Severity: minor
- File: src/frontend/Dockerfile:26
- Description: Image and Compose healthchecks (`Dockerfile:26-27`, `docker-compose.yml:99-100`) `wget` `http://127.0.0.1:8765/`. nginx `location /` (`nginx.conf:44-46`) always serves `index.html` 200, even when the gateway is down. The frontend container reports healthy while Overview shows failed cards. `/health` is already proxied (`nginx.conf:31-33`) to the gateway.
- Suggestion: Healthcheck `http://127.0.0.1:8765/health` (or `/readyz`) so console readiness includes the upstream the UI depends on.
- Status: open

### Issue 11 -- Severity: minor
- File: src/frontend/src/src/pages/GatewayChatPage.tsx:25
- Description: Chat is a single buffered `POST /v1/chat` with no `stream: true`, no `AbortController`, no model/provider/credential fields, and no `aria-live` region. nginx (`nginx.conf:10,18`) already disables proxy buffering and sets `proxy_read_timeout 1200s` to match gateway `WriteTimeout` (`server.go:140`) and `wantsStream` (`server.go:657-664`). The UI does not use that path, so long local/frontier completions look hung (only a spinner on the send button). Errors are appended as `role: 'system'` turns (`L46,53`), then later user sends filter those out (`L37-39`) — good — but there is no retry, no empty-node guidance beyond the subtitle, and the textarea has no accessible name.
- Suggestion: Send `stream: true` (or `Accept: application/x-ndjson`) and append deltas; abort on Clear / unmount; `aria-live="polite"` on the transcript; `aria-label` on the textarea. If no nodes/frontier are configured, disable send and link to `/nodes`.
- Status: open

### Issue 12 -- Severity: minor
- File: src/frontend/src/src/pages/CoreOverview.tsx:48
- Description: Stack polling is 5s with default React Query retries (3). A down gateway therefore hammers `/api/stack` and delays the error UI. Only the Gateway `GlassMetricCard` receives the query `error` (`L70`); Elasticsearch/OpenBao cards get `data?.*.error` and otherwise render `"—"` with no failed state. `depBadge` maps `ok`/`healthy` → `healthy` and unknown (including gateway `down`) → `failed` (`L32-45`), which is fine, but the hero values are raw `"ok"` strings (`L67`) rather than the badge labels. Help text on OpenBao (`L88`) says "Dev token is injected at compose time" — not the token itself, but it documents the compose secret channel in the UI. Help text on Gateway (`L71`) cites `:8090`, which is the internal docker port; users hit `:8765`.
- Suggestion: `retry: false` + a slower interval on error (pattern already in `useSnapshot.ts:27`). Pass fetch errors to every card or add a page-level banner. Map card values through `depBadge`. Soften/remove the OpenBao token hint. Say "this console proxies /api to the gateway."
- Status: open

### Issue 13 -- Severity: minor
- File: src/frontend/src/src/components/GlassMetricCard.tsx:96
- Description: Trend rendering defaults `suffix` to `'%'`. `NodesOverview.tsx:642-645` passes `trend={{ value: healthy, label: '…' }}` for node counts, so 2 healthy nodes display as `+2%`. The same default fires for "Degraded / Offline" and "Avg Load" (the latter is actually a percent, but still gets a leading `+`).
- Suggestion: Require an explicit `suffix` (including `''`) or pass `suffix: ''` from Nodes. Do not default to `%`.
- Status: open

### Issue 14 -- Severity: minor
- File: src/frontend/src/src/components/AppLayout.tsx:78
- Description: Theme toggle and sidebar collapse buttons have no `aria-label` / `aria-expanded` (`L78-81`, `L100-104`). Collapsed `SidebarNavItem` (`SidebarNavItem.tsx:31`) renders only an icon with no `title` or `aria-label` (the visible label is omitted). `nav` (`AppLayout.tsx:70`) has no `aria-label`. `AddNodeModal` (`NodesOverview.tsx:372`) is a `fixed` overlay with no `role="dialog"`, no focus trap, no Escape handler; close control is a raw "✕" (`L381`) without a name. `RoutingModeSelector` buttons (`RoutingModeSelector.tsx:38`) have no `aria-pressed`. `CoreSettingsPage.tsx:38-48` `<Label>Theme</Label>` / `<Label>Skin</Label>` are not associated with the buttons (`htmlFor` missing). Chat send/clear have `aria-label` (`GatewayChatPage.tsx:103,109`) — that bar is not applied elsewhere. `NotFound.tsx:19` uses `<a href="/">` instead of `Link`, forcing a full reload.
- Suggestion: Label icon-only controls, `aria-pressed` on mode/theme, dialog semantics + focus trap on modals, `Link` on 404. Treat collapsed nav items as the name of the destination.
- Status: open

### Issue 15 -- Severity: minor
- File: src/frontend/src/src/components/AddFrontierModelModal.tsx:279
- Description: The new API key field is `type="text"` with no `autoComplete="off"` / `autoComplete="new-password"`. The key sits in React state and is posted in JSON (`L61-66`). Browser form-history and shoulder-surfing apply. Combined with Issue 1 this is currently a failed request, but once a gateway write exists it is a real secret in the client (acceptable if OpenBao stores it and the GET catalog only returns `has_key`, which `server.go:1333-1337` already does).
- Suggestion: `type="password"`, `autoComplete="off"`, never log `apiKey`. Confirm GET `/api/frontier-models` never returns material (gateway `CredentialPublicInfo` is ID/label/has_key only — keep it that way).
- Status: open

### Issue 16 -- Severity: nit
- File: src/frontend/src/index.html:9
- Description: Title/description/OG/Twitter still describe "Next-generation AI operations center for autonomous software delivery orchestration". Core footer (`AppLayout.tsx:117`) correctly says "Flume core: OpenBao, Elasticsearch, Gateway, console." `robots.txt:13` is `User-agent: * / Allow: /` for a local operator console. `App.core.tsx` and `App.tsx` both construct a module-scope `QueryClient` with default retries.
- Suggestion: Core-build HTML title "Flume core". `robots.txt` `Disallow: /`. Optional: instantiate `QueryClient` inside the component or set `retry: 1` for core.
- Status: open

### Issue 17 -- Severity: nit
- File: src/frontend/src/src/pages/NodesOverview.tsx:188
- Description: Node Test failures swallow the exception (`catch { log.warn('Silent catch block executed'); … error: 'Connection test failed' }` at L194-196), hiding gateway messages such as `"node not found"`. Edit is a `POST /api/nodes` upsert (gateway `handleAddNode` always `201 created`, `server.go:1099-1100`) rather than a dedicated PATCH; workable because ES upsert exists, but the UI always says "Saving…" / "created" semantics. `window.confirm` for delete/remove-model (`L528,811`) is the only destructive guard.
- Suggestion: Show `err.message` from the test endpoint. Keep POST-upsert if that is the gateway contract, but do not pretend it is a different resource. Prefer an in-page confirm dialog with the node id.
- Status: open

## Strengths

- `main.tsx:10-12` + `lib/core.ts:2` + Docker `ARG VITE_CORE_UI=true` (`Dockerfile:12-14`, `docker-compose.yml:90-91`) is the right compile-time split. `App.core.tsx` does not import Mission Control, Projects, Queue, Analytics, or Security, so those pages should stay out of the core graph as long as nothing else pulls them.
- Core Overview is actually a core page: `GET /api/stack` matches `handleStack` (`server.go:818,909-964`), including `service: flume-core` and ES/OpenBao/node counts. `CoreOverview.test.tsx:28-43` asserts that contract.
- Nodes, when not saving credentials, is aimed at the gateway: `/api/nodes`, `/api/nodes/{id}/test`, `DELETE`, `/api/routing-policy`, `/api/frontier-models` are registered in `server.go:819-832`. JSON field names (`id`, `host`, `model_tag`, `providers`) match the Go structs. Error copy at `NodesOverview.tsx:793` says "Unable to reach the Gateway." Empty state for zero nodes (`L763-787`) is present.
- Chat request/response shapes match gateway `ChatRequest` / `ChatResponse` (`messages[].role/content`, `message.content`, `error`). Send/Clear controls have `aria-label`s. Enter-to-send with Shift+Enter for newline is correct.
- `AppLayout` core nav is Overview / Nodes / Chat + Settings (`L24-28,96`) and the snapshot banner is gated (`L110`). Footer copy is core-specific (`L116-118`).
- nginx `proxy_buffering off` + `proxy_read_timeout 1200s` + HTTP/1.1 (`nginx.conf:10-18,21-28`) is the right streaming posture, even if Chat does not stream yet. SPA `try_files` (`L44-46`) is correct. gzip is on.
- Gateway `AuthToken` is `json:"-"` (`node_registry.go:82`); `GET /api/nodes` will not leak node bearer tokens into the browser. Frontier catalog credentials are public summaries only (`has_key`).
- `safeFetch.ts` is a sound primitive (content-type guard, JSON error message extraction). It just is not on the core call path yet.
- Compose default is four services; dashboard/workers stay on profile `full` (`docker-compose.yml:107-108,149`). Frontend image does not bake the Go dashboard.

## Residual risks

- **Bundle DCE is implicit.** `AppLayout.tsx` statically imports `SnapshotErrorBanner` → `useSnapshot` → `GET /api/snapshot`. That is dead in core only if Vite inlines `isCoreUI === true` and drops the JSX. A bundler or a runtime `import.meta.env` miss puts dashboard polling back on the core console (404s, extra bytes). A core-only layout file would make this structural instead of optimizer-dependent.
- **Credential write is a product hole, not just a 404.** Even after pointing the modal at a new gateway route, OpenBao policy, key rotation, and "never echo apiKey" tests have to exist. Until then, hybrid/frontier modes on Nodes are configuration theater.
- **Unauthenticated console port.** Issue 2 is not unique to the frontend, but nginx is the thing that made the gateway browsable. Local-trusted Compose may be an accepted risk; it should be explicit in the compose comments, not an accident of `proxy_pass`.
- **Full dashboard pages remain in the same package.** Tests, Playwright `baseURL: 8080`, and `App.tsx` will keep steering contributors toward `/api/snapshot` and `/api/settings`. Without a packager-enforced allow-list, core UI will re-grow dashboard coupling.
- **Chat without nodes is a silent failure.** Gateway routing will error; the page only shows the HTTP message. Overview's node count and Chat are not cross-linked.
- **No route-level splitting** means Nodes' framer-motion-heavy mesh (and Add Frontier modal) always ships with the first paint of Overview.
- **Healthcheck vs `/health`.** Operators can believe the stack is up because wget got `index.html`.
- **`App.tsx` dual list** will drift the first time someone adds a core route in only one file.

---

Reviewed files (primary): `src/frontend/src/src/{main.tsx,App.tsx,App.core.tsx,lib/core.ts,vite-env.d.ts}`, `components/{AppLayout,AddFrontierModelModal,SnapshotErrorBanner,GlassMetricCard,SidebarNavItem,RoutingModeSelector}.tsx`, `pages/{CoreOverview,GatewayChatPage,CoreSettingsPage,NodesOverview,NotFound}.tsx`, `hooks/{useSnapshot.ts,useSystemState.tsx,useTheme.tsx}`, `utils/{safeFetch.ts,logger.ts}`, `test/{App.test.tsx,pages/CoreOverview.test.tsx,pages/NodesOverview.test.tsx,pages/SettingsPage.test.tsx}`, `src/frontend/{nginx.conf,Dockerfile}`, `src/frontend/src/{vite.config.ts,vitest.config.ts,index.html,package.json}`, plus gateway route registration in `src/gateway/server.go` and dashboard `/api/settings/llm/credentials` in `internal/dashboard/server.go`.
