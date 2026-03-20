# Flume — Installation & operations

An AI-powered agent workflow platform — plan, implement, test, and review code changes
using a coordinated team of LLM agents, with a real-time dashboard.

The **repository root README** is [`../README.md`](../README.md) (short overview + diagram). **This file** is the full install, architecture, and troubleshooting guide.

---

## Architecture

### Components

| Component | Role |
|-----------|------|
| **Dashboard** (`server.py`) | HTTP server on `DASHBOARD_HOST`:`DASHBOARD_PORT` (default `0.0.0.0:8765`). Serves the React UI, REST APIs (`/api/snapshot`, settings, projects, …), and talks to Elasticsearch. |
| **Worker manager** (`manager.py`) | Polls ES for claimable work per agent role; updates worker heartbeat state. |
| **Worker handlers** (`worker_handlers.py`) | Runs agent pipelines (intake, PM, implementer, tester, reviewer, memory-updater) using configured LLMs. |
| **Elasticsearch 8.x** | Primary store: tasks, handoffs, failures, provenance, memory indices (see `memory/es/index_templates/`). |
| **OpenBao** (optional, recommended) | **KV secrets** — API keys, `ES_API_KEY`, tokens. Flume reads **`flume.config.json`** + token file, then `openbao kv get`. |
| **OpenBao CLI / `gh` / Codex** | Installed by the installer (best-effort): secrets, GitHub PRs, and **OpenAI Codex CLI** (`@openai/codex`) for OAuth / Plan New Work. |

### Configuration flow (startup)

1. **Shell** (`dashboard/run.sh`, `worker-manager/run.sh`) sets `PYTHONPATH` to the app root (`src` in git layout, package root in tarball), optionally **`source`s `.env`** if present.
2. **Python** imports **`flume_secrets`** (`src/flume_secrets.py`):
   - Parses **`.env`** lines into `os.environ` (dashboard only; legacy / mixed mode).
   - If **`flume.config.json`** exists and OpenBao **addr + token** resolve: runs **`openbao kv get`**, merges **all** KV fields into **`os.environ`** (same key names as `.env`).
3. **Dashboard / workers** read `ES_URL`, `ES_API_KEY`, `LLM_*`, etc. from the environment.

**Bootstrap rule:** you need **either** `flume.config.json` (OpenBao path) **or** `.env` at the Flume **repo / package root** (or `OPENBAO_ADDR` in the environment). Secrets should live in **OpenBao KV**, not in git.

### Data layout (conceptual)

```text
flume repo root (WORKSPACE_ROOT)
├── flume.config.json      ← OpenBao bootstrap (non-secret JSON)
├── .env                     ← Legacy / mixed; installer still writes defaults
├── install/.es-bootstrap.env ← ES API key from ES installer (local only)
├── projects.json
├── sequence_counters.json
├── plan-sessions/
└── worker-manager/state.json

src/   (git clone only)
├── flume_secrets.py       ← OpenBao + bootstrap loader
├── dashboard/
├── worker-manager/
├── agents/
├── memory/es/
└── frontend/dist/
```

---

## Installation pipeline

### Option A — `setup.sh` (recommended)

From the **Flume root** (git clone or extracted package):

```bash
bash setup.sh
```

This:

1. Runs **`install/install.sh`** (git) or **`install.sh`** (package).
2. On **git clones**, runs `npm install && npm run build` under `src/frontend/src` if `npm` exists (the installer usually adds **Node.js LTS** + **codex** first).
3. Loops until **`.env` has a valid `ES_API_KEY`** (or bootstrap applied), optionally invoking ES installers / bootstrap scripts.
4. Runs **`create-es-indices.sh`** with `ENV_FILE` set.
5. Installs **`flume-dashboard.service`** (systemd user) and runs **`./flume start`** when ES credentials are valid.

### Option B — `install.sh` only

**Git clone:**

```bash
cd /path/to/flume
bash install/install.sh
```

**Package tarball:**

```bash
cd flume-<VERSION>/
bash install.sh
```

### What `install.sh` does (steps 1–7)

| Step | Name | Scripts / actions |
|------|------|-------------------|
| **1** | Check dependencies | `setup/verify-deps.sh` — required: `python3`, `git`, `pgrep`, `curl`. Optional: `gh`, `openbao` CLI, `node`, `codex`, running Elasticsearch. |
| **2** | Elasticsearch | If ES is down or `install/.es-bootstrap.env` lacks a key, runs `setup/install-elasticsearch.sh` (often via `sudo`). May run `bootstrap-es-credentials.sh` on a TTY if needed. |
| **3** | OpenBao, GitHub CLI & Codex | `setup/install-openbao.sh`, `setup/install-gh.sh`, **`setup/install-codex-cli.sh`** (installs **Node.js LTS** into `/usr/local` if needed, then `npm i -g @openai/codex`). Best-effort; requires `sudo` and outbound HTTPS to `nodejs.org` + `registry.npmjs.org`. |
| **4** | Configure runtime | Creates **`.env`** from `install/.env.template` if missing; merges ES bootstrap into `.env`; writes **`flume.config.json`** from `install/flume.config.example.json` if missing; if `BAO_TOKEN`/`VAULT_TOKEN`/`OPENBAO_TOKEN` is set, runs **`setup/sync-bootstrap-to-openbao.sh`** to push `ES_*` into OpenBao KV. |
| **5** | Elasticsearch indices | `setup/create-es-indices.sh` — uses `.env` and/or **`setup/hydrate-openbao-env.py`** when `ES_API_KEY` is only in OpenBao. |
| **6** | Workspace | Creates state files, scrubs stray bundled repos, optional **`setup/install-flume-service.sh`** (needs `.env` **or** `flume.config.json`). |
| **7** | Done | Prints `./flume` and worker commands. |

---

## Repository layouts

### Git clone (`~/flume`)

```
flume/
├── setup.sh
├── install/
│   ├── install.sh
│   ├── README.md              ← this guide
│   ├── .env.template
│   ├── flume.config.example.json
│   └── setup/
│       ├── verify-deps.sh
│       ├── install-elasticsearch.sh
│       ├── install-openbao.sh
│       ├── install-gh.sh
│       ├── hydrate-openbao-env.py
│       ├── codex_oauth_login.py
│       ├── sync-bootstrap-to-openbao.sh
│       ├── bootstrap-es-credentials.sh
│       ├── create-es-indices.sh
│       ├── install-flume-service.sh
│       └── flume-dashboard.service.template
├── flume                      ← CLI (systemd user dashboard)
├── src/
│   ├── flume_secrets.py
│   ├── dashboard/
│   ├── worker-manager/
│   ├── agents/
│   ├── memory/es/
│   └── frontend/
└── (after install) .env, flume.config.json, projects.json, …
```

Run: `bash src/dashboard/run.sh`, `bash src/worker-manager/run.sh`.

### Extracted package (`flume-<VERSION>/`)

```
flume-<VERSION>/
├── setup.sh
├── install.sh
├── flume
├── flume.config.example.json
├── setup/                     ← same scripts as git’s install/setup/
├── dashboard/
├── worker-manager/
├── agents/
├── memory/es/
├── frontend/dist/
└── .env.template
```

Run: `bash dashboard/run.sh`, `bash worker-manager/run.sh`.

---

## Quick start (recap)

```bash
# Package:
tar -xzf flume-<VERSION>.tar.gz && cd flume-<VERSION>/ && bash setup.sh

# Git:
cd ~/flume && bash setup.sh
```

Then:

```bash
./flume start    # dashboard (background)
./flume logs     # optional
```

By default the **dashboard auto-starts** the worker manager and agent handlers when it launches (`FLUME_AUTO_START_WORKERS=1` in `.env`). To run workers only manually or on another host, set **`FLUME_AUTO_START_WORKERS=0`** and use `bash src/worker-manager/run.sh` (git) or `bash worker-manager/run.sh` (package).

---

## Configuration

### OpenBao-first (recommended)

1. **`flume.config.json`** at the **Flume root** (created from `install/flume.config.example.json` by the installer). Contains **`openbao.addr`**, **`mount`**, **`path`**, and **`tokenFile`** (path to a file with the token, `chmod 600`).
2. **KV** at e.g. **`secret/flume`** (configurable) holds key=value pairs matching **`.env` names**: `ES_URL`, `ES_API_KEY`, `ES_VERIFY_TLS`, `LLM_PROVIDER`, `LLM_API_KEY`, `GH_TOKEN`, `EXECUTION_HOST`, index names, etc.
3. **Push ES bootstrap into OpenBao** (after ES install, if you have a token):

   ```bash
   BAO_TOKEN=s.xxx bash install/setup/sync-bootstrap-to-openbao.sh
   # or from package root:
   BAO_TOKEN=s.xxx bash setup/sync-bootstrap-to-openbao.sh
   ```

4. **`.env` is optional** if OpenBao supplies everything; the installer may still create `.env` with non-secret defaults for convenience.

### Legacy `.env`

Single file at the Flume root, from **`install/.env.template`**. The dashboard still parses `.env` in Python for compatibility; OpenBao KV **overrides** overlapping keys when both are used.

**Missing `ES_API_KEY`:** run:

```bash
ELASTIC_PASSWORD=yourpassword bash install/setup/bootstrap-es-credentials.sh
```

(`elastic` user password from Elasticsearch; reset with `elasticsearch-reset-password` if needed.)

### LLM providers

Configure in the **Settings** UI or via `.env` / OpenBao KV:

| Provider | `LLM_PROVIDER` | Notes |
|----------|----------------|--------|
| Ollama | `ollama` | Local / network URL |
| OpenAI | `openai` | API key or OAuth |
| OpenAI-compatible | `openai_compatible` | Custom `LLM_BASE_URL` |
| Anthropic / Gemini / xAI / Mistral / Cohere | `anthropic`, `gemini`, … | API keys. **Gemini** uses a [Google AI Studio](https://aistudio.google.com/apikey) key; the OpenAI-compatible endpoint expects **`Authorization: Bearer <key>`** (per [Google’s OpenAI compat docs](https://ai.google.dev/gemini-api/docs/openai)). |

**Multiple providers / keys:** In Settings, pick a **provider** (OpenAI, Gemini, …); the **Saved API keys** list shows **only keys for that provider** (change the provider dropdown to manage another vendor’s keys). Store **many labeled keys per vendor** (labels must be **unique per provider**). **Set as default** picks which saved row fills the global **LLM_*** profile and backs agent roles that use **Settings (default)**; saving a new key from the main form can still set the default. Keys live in **`worker-manager/llm_credentials.json`** (`defaultCredentialId` + legacy `activeCredentialId`). **Agents → Configure agent models** lists **every** saved key and **Settings default** so you assign a **specific credential** per role (not just provider/model); providers in the dialog come from those rows. The worker manager reads **`worker-manager/agent_models.json`** each poll. **Agent Operations** cards show the key **label**.

ChatGPT/Codex OAuth: prefer **`./flume codex-oauth login-browser`** (see **OpenAI ChatGPT / Codex OAuth**); device **`login`** may lack `api.responses.write`.

After changes, **restart** dashboard and workers.

---

## OpenBao CLI install

```bash
sudo bash install/setup/install-openbao.sh    # git
sudo bash setup/install-openbao.sh            # package
openbao version
```

The **OpenBao server** is separate (you operate it); Flume only needs reachability + a valid token.

---

## GitHub CLI

```bash
sudo bash install/setup/install-gh.sh
gh auth login
# or GH_TOKEN in OpenBao KV / .env
```

---

## Elasticsearch

Install / repair:

```bash
sudo bash install/setup/install-elasticsearch.sh
```

Bootstrap credentials file: **`install/.es-bootstrap.env`** (git) — applied into `.env` by `install.sh`.

Create indices manually:

```bash
bash install/setup/create-es-indices.sh
# with ENV_FILE:
ENV_FILE=/path/to/flume/.env bash install/setup/create-es-indices.sh
```

---

## OpenAI ChatGPT / Codex OAuth

Flume can store a **ChatGPT (Codex) OAuth session** (same flows as the [Codex CLI](https://github.com/openai/codex)) or use an OpenAI **platform API key** (`sk-…`).

**Plan New Work (text planning) without a platform `sk-` key:** Codex **browser** OAuth tokens usually lack **`model.request`**, so posting that bearer to **`api.openai.com`** **`/v1/chat/completions`** returns **401**. Flume therefore **defaults** (`FLUME_OPENAI_USE_CODEX_APP_SERVER=auto`) to driving **text chat** (including the intake planner) through the **Codex CLI** **`codex app-server`** over **stdio**, using **`~/.codex/auth.json`** — the same session as **`codex login`**. That path is intended for **ChatGPT/Codex subscription** usage rather than pay-per-token **platform** billing. Install **Node.js**, then **`npm i -g @openai/codex`** (or rely on **`npx`**), run **`codex login`** on the Flume host, and **`./flume restart --all`**. Flume’s **`.openai-oauth.json`** is **not** read by the Codex subprocess; keep **`~/.codex/auth.json`** in sync (e.g. login with Codex, or copy `auth.json` from a machine where you already signed in).

**Worker agents (tool-calling loop)** still use **`llm_client.chat_with_tools`** → **`api.openai.com`** today. For those roles, use a **platform `sk-`**, **Ollama**, or another provider until a full Codex app-server tool bridge exists.

Set **`FLUME_OPENAI_USE_CODEX_APP_SERVER=0`** to force legacy HTTP-only OpenAI chat (will 401 on typical Codex OAuth). Set **`1`** to force the Codex path even for unusual tokens.

If **`LLM_PROVIDER=openai`** but **`LLM_BASE_URL`** still points at **Ollama** (e.g. `http://127.0.0.1:11434`), older builds could POST your OAuth bearer to the wrong host and get **401**. Current Flume ignores localhost / `:11434` bases for official OpenAI; you can also **clear `LLM_BASE_URL`** in `.env` when using hosted OpenAI.

### Headless servers, remote SSH, and CI (same ideas as Codex CLI)

The [Codex CLI](https://github.com/openai/codex) uses **browser OAuth** by default: an authorize URL, an HTML sign-in page, then a redirect to a **loopback callback** (e.g. `http://localhost:1455/auth/callback`) where a short-lived local server receives the `code` and exchanges it for tokens (stored in **`~/.codex/auth.json`**). That matches Flume’s **`login-browser`** / **`login-paste`** (Flume writes **`<flume-root>/.openai-oauth.json`** and can sync **`.env`**).

| Situation | Typical approach | In Flume |
|-----------|------------------|----------|
| **GUI on same machine** | `codex login` opens browser → localhost callback | `./flume codex-oauth login-browser` |
| **No browser on the server** | Open authorize URL on another device; still need the **localhost** redirect URL to complete PKCE | `./flume codex-oauth login-paste` (paste full `http://localhost:1455/auth/callback?...` back into SSH) |
| **SSH tunnel** | Forward the random/high port to the server | `ssh -L <port>:127.0.0.1:<port> user@host` then open the printed URL on your laptop |
| **Device code** | `codex login --device-auth` (code + URL on any browser) | `./flume codex-oauth login` — may have weaker scopes; prefer **`login-browser`** + **`codex login`** for **`~/.codex`** |
| **Login on a laptop, run Flume on a server** | Copy **`~/.codex/auth.json`** (e.g. `scp`) | Run **`codex login`** locally, then **`./flume codex-oauth import`** on the server (or copy `auth.json` and import) |
| **No ChatGPT OAuth** | Platform **API key** | **Settings → LLM → API Key** (`sk-…`); uses **`/v1/chat/completions`**, not the ChatGPT OAuth bearer path |

Account or org policies (e.g. device auth allowed in ChatGPT / Codex settings) apply the same way as for the official CLI.

### Error: `Missing scopes: api.responses.write`

**Browser / paste / import (Codex) tokens** usually list only **connector** scopes in the JWT (`api.connectors.read`, …) — OpenAI does **not** allow `model.request` / `api.responses.write` on `/oauth/authorize` for the public Codex client. **Current Flume** detects that and calls **`/v1/chat/completions`** for Plan New Work and agents instead of **`/v1/responses`**, so this error should stop after **`git pull`** and **`./flume restart --all`**.

If you still see **`/v1/responses`** in the error after restart, the running code is **not** the version with JWT routing. Verify upstream is merged, then check the file on the server:

```bash
grep -n "if scopes is None" ~/flume/src/dashboard/llm_client.py
```

You should see **`return False`** (OAuth without decodable `api.responses.write` uses chat/completions). If the line is missing, **`git remote -v`** / **`git fetch upstream && git merge upstream/main`** (or pull from **Fremen-Labs/flume**). Then **`./flume restart --all`** again.

If routing is correct but you then see **`Missing scopes: model.request`** on **`/v1/chat/completions`**, that is expected for Codex OAuth: the authorize URL cannot request `model.request`, but the API still requires it. **Use a platform `sk-` API key** for Flume’s planner and agents calling **`api.openai.com`**.

**Device-code** (`./flume codex-oauth login`) often yields **weaker** tokens than browser OAuth; **`Refresh OAuth` cannot add scopes** that were never granted.

**Fix (pick one):**

1. **Recommended — browser login built into Flume** (localhost callback + PKCE, same idea as `codex login`):

   ```bash
   ./flume codex-oauth login-browser
   ./flume restart --all
   ```

   **Note:** `auth.openai.com` does **not** accept a `resource` parameter on **token refresh** (it returns `unknown_parameter`). Flume only adds optional `resource=` to the **authorize** URL if you set **`OPENAI_OAUTH_RESOURCE`** in `.env`.

   **`login-browser`** listens on **`http://localhost:1455/auth/callback`** by default (same as the official Codex CLI). Do not use a random port — OpenAI often returns **`unknown_error`** on the sign-in page if the redirect URI is not allowlisted.

   Run this on a machine where your **browser can reach `http://127.0.0.1:1455`** (or use **`ssh -L 1455:127.0.0.1:1455`** from your laptop). The script prints the exact authorize URL.

   If the browser completes login on **another** computer (or you only have the redirect URL from chat), **paste the full `http://localhost:1455/auth/callback?...` line into the same terminal** where `login-browser` is running (Unix/macOS). Each run uses a new PKCE session — if you stopped the command, run **`login-browser`** again and use the **new** callback URL.

   **Headless server (no browser on the Flume host):** use **paste-back** (same API scopes as `login-browser`):

   ```bash
   ./flume codex-oauth login-paste --write-html /tmp/flume-oauth.html
   ```

   Copy the printed **authorize URL** or `scp` the HTML file to a laptop, open it in a browser, sign in. The browser redirects to `http://localhost:<port>/auth/callback?...` (often “connection refused” — that is OK). **Copy the full URL from the address bar** and paste it into the terminal where `login-paste` is waiting. Default port is **1455** (same as the official Codex CLI; OpenAI’s OAuth allowlist rejects most other ports — if you see `auth.openai.com/error` with `unknown_error`, fix the port before overriding). Override with `--port` or **`FLUME_OAUTH_PASTE_PORT`** only if you know what you’re doing.

2. **Official Codex CLI**: **`codex login`** (browser), then **`./flume codex-oauth import`**.

3. **Device code** (`./flume codex-oauth login`) may still work for some accounts; if you see this 401, prefer **`login-browser`**.

Optional **`OPENAI_OAUTH_SCOPES`**, **`OPENAI_OAUTH_AUTHORIZE_SCOPES`**, **`OPENAI_OAUTH_ORIGINATOR`** — see **Advanced** under OpenAI OAuth.

### Recommended: Flume CLI (from the Flume install directory)

```bash
./flume codex-oauth login-browser   # best when the Flume host can open localhost in a browser
# or (headless / OpenClaw-style paste-back):
./flume codex-oauth login-paste --write-html /tmp/flume-oauth.html
# or: ./flume codex-oauth login    # device code; may lack api.responses.write
./flume restart --all
```

**`login-browser`:** opens (or prints) an **authorize** URL; after you sign in, the browser redirects to **localhost** and Flume writes **`<flume-root>/.openai-oauth.json`** and updates **`.env`** (unless `--no-sync-env`).

**`login-paste`:** for **headless** hosts: prints the authorize URL, optionally writes an HTML file with a clickable link; you complete login on another machine and **paste the redirect URL** from the address bar back into the terminal.

**`login`:** follow the **Codex device** URL and enter the one-time code.

Then set **Settings → LLM → OpenAI → Auth mode → OAuth** (or rely on the updated `.env`).

**`./flume restart` only restarts the dashboard (systemd).** Worker manager + worker handlers keep running until you run **`./flume restart --all`** (or stop/start them manually). Use **`--all`** after LLM, OAuth, or worker code changes so **Agent Operations** picks up the new model.

**Dashboard UI (React) is not rebuilt by `git pull` or `./flume restart`.** The server serves **`src/frontend/dist`** (bundled JS). After pulling changes under **`src/frontend/src/`** (pages, components), run:

```bash
./flume build-ui              # npm install + vite build → src/frontend/dist
./flume restart               # or: ./flume restart --build-ui
```

Then **hard-refresh** the browser (**Ctrl+Shift+R** / **Cmd+Shift+R**) so cached assets are not used.

**Troubleshooting:** If Agents / Settings look unchanged after `git pull` + `./flume restart`, you did not rebuild the bundle. Run **`./flume build-ui`** (or **`./flume restart --build-ui`**). After **`./flume restart`**, Flume prints a **boxed warning** when any `src/frontend/src/**/*.tsx` is newer than `src/frontend/dist/index.html`.

**Package tarball** (extracted root): same commands — `./flume` lives next to `setup/`.

### Already use the official Codex CLI?

```bash
codex login            # or: codex login --device-auth
./flume codex-oauth import
./flume codex-oauth refresh
./flume restart --all
```

Imports **`~/.codex/auth.json`** (or **`$CODEX_HOME/auth.json`**) into Flume’s OAuth state file.

### Codex app-server (OAuth-backed coding & code review)

OpenAI’s **[Codex app-server](https://developers.openai.com/codex/app-server)** is the JSON-RPC surface the Codex VS Code extension uses: threads, turns, streamed agent events, approvals, and **`review/start`**. It is **not** the same path as Flume’s HTTP **`llm_client`** (`/v1/chat/completions`). Use it when you want **Codex-native** behavior with **ChatGPT/Codex OAuth** (via **`~/.codex/auth.json`** / **`codex login`** / **`./flume codex-oauth import`**).

1. Install **Node.js** (includes `npx`). Optionally install the [Codex CLI](https://github.com/openai/codex) globally: **`npm i -g @openai/codex`** (adds `codex` to your `PATH`).
2. Ensure OAuth: **`./flume codex-oauth login-browser`** (or **`codex login`**, then **`./flume codex-oauth import`**).
3. Start the server (WebSocket is convenient for local tools):

   ```bash
   # Optional: override listen URL (default ws://127.0.0.1:4500)
   export FLUME_CODEX_APP_SERVER_LISTEN=ws://127.0.0.1:4500
   ./flume codex-app-server
   ```

   If **`codex`** is not on `PATH`, **`./flume codex-app-server`** falls back to **`npx --yes @openai/codex`** (first run may download the package). To force that path: **`FLUME_CODEX_APP_SERVER_VIA_NPX=1`**. To use a specific binary: **`CODEX_BIN=/path/to/codex`**.

4. **Settings → Codex app-server** shows whether the configured port is accepting TCP connections and whether **`codex`** is on `PATH` (or use **`npx`** as above).

**Next steps (integration):** connect your client or IDE to the same WebSocket URL, send **`initialize`** / **`initialized`**, then **`thread/start`**, **`turn/start`**, etc., per the [protocol docs](https://developers.openai.com/codex/app-server). Future Flume work may add a dashboard proxy or dedicated UI on top of this transport.

### Bootstrap (Codex session cache + fallbacks)

```bash
./flume codex-oauth bootstrap
```

Uses **`~/.codex/auth.json`** when present; otherwise tries optional legacy profile import via **`install/setup/openai-oauth.sh`** / **`setup/openai-oauth.sh`**. Then refreshes and syncs **`.env`**.

### Refresh / status

```bash
./flume codex-oauth refresh
./flume codex-oauth status
```

### Low-level scripts (optional)

Same behavior without the Flume CLI:

```bash
python3 install/setup/codex_oauth_login.py login --flume-root /path/to/flume
python3 install/setup/codex_oauth_login.py login-browser --flume-root /path/to/flume
python3 install/setup/codex_oauth_login.py login-paste --flume-root /path/to/flume
bash install/setup/openai-oauth.sh refresh
```

### Advanced

- **`OPENAI_OAUTH_CLIENT_ID`** — override the public OAuth client id (default matches openai/codex).
- **`OPENAI_OAUTH_SCOPES`** — space-separated scopes for **device login** (`login`) and **token refresh** only. Default includes `model.request`, `api.model.read`, `api.responses.write`. Empty string omits `scope` from those requests. **Not** used for **`login-browser`** / **`login-paste`** (see below).
- **`OPENAI_OAUTH_AUTHORIZE_SCOPES`** — scopes for **`login-browser`** and **`login-paste`** GET `/oauth/authorize` only. Default matches **official Codex** (`openid profile email offline_access api.connectors.read api.connectors.invoke`). OpenAI returns **`invalid_scope`** if you request e.g. `model.request` on authorize — do not copy `OPENAI_OAUTH_SCOPES` here.
- **`OPENAI_OAUTH_RESOURCE`** — Optional. If set (non-empty), appended as **`resource`** on browser **`/oauth/authorize`** only. **Not** sent to `/oauth/token` — OpenAI’s token endpoint returns **`unknown_parameter`** for `resource` on refresh and code exchange.
- **`OPENAI_OAUTH_ORIGINATOR`** — `originator` query param for browser authorize (default `codex_cli_rs`, matches Codex CLI).
- State file path defaults to **repo/package root** so it works with **`LOOM_WORKSPACE`** = `src/` (dashboard and workers resolve relative paths against the repo root first).

Updates `.env` (and can sync sensitive fields to OpenBao via Settings when OpenBao is enabled).

---

## Multi-machine workers

Each host runs **`worker-manager`**. Use **`EXECUTION_HOST`** in `.env` or OpenBao KV so roles dispatch to the right machine. All hosts share the **same Elasticsearch** (and ideally the same secret source).

---

## Runtime files (after install)

```
flume/
├── .env                    ← optional legacy; do not commit real secrets
├── flume.config.json       ← OpenBao bootstrap; safe to commit if no secrets inside
├── projects.json
├── sequence_counters.json
├── plan-sessions/
└── worker-manager/
    ├── state.json
    └── *.log
```

---

## Troubleshooting

| Issue | What to check |
|-------|----------------|
| Dashboard won’t start / **`ss` shows no `:8765`** | **`./flume start`** now errors if the service exits (prints a `journalctl` tail). Check `journalctl --user -u flume-dashboard -n 50`; run **`bash src/dashboard/run.sh`** in foreground. **Python ≥ 3.9**; modules using PEP 604 unions must include **`from __future__ import annotations`** or Python 3.9 crashes on import. |
| **`/api/snapshot` 502 / ES not configured** | `ES_API_KEY` in OpenBao KV or repo-root **`.env`** (`~/flume/.env`); not `AUTO_GENERATED_BY_INSTALLER`. If you have a stray **`src/.env`**, remove it or ensure **`flume/.env`** has the real key (repo root wins). Token file readable for OpenBao mode. **`./flume restart`** after changing secrets. |
| OpenBao not loading | `openbao` on `PATH`; `OPENBAO_ADDR` + token; `flume.config.json` paths correct; `openbao kv get secret/flume` works manually. |
| Indices missing | `bash install/setup/create-es-indices.sh`; template `agent-review-records.json` path in script. |
| Workers idle / no tasks | `EXECUTION_HOST`, `worker-manager/manager.log`, ES connectivity from that host. |
| Elasticsearch down | `systemctl status elasticsearch`; `journalctl -u elasticsearch -f`. |
| PR creation | Install `gh`, authenticate, or set **`GH_TOKEN`** in KV / `.env`. |

---

## Running without `setup.sh`

```bash
# Dashboard (foreground)
bash src/dashboard/run.sh      # git
bash dashboard/run.sh          # package

# Workers
bash src/worker-manager/run.sh
bash worker-manager/run.sh
```

---

## Create your first project

1. Open the dashboard → **Projects → New Project**
2. Name + optional git URL
3. Use **Intake** to describe work; planning decomposes into epics → tasks

---

## What’s included (features)

- Real-time dashboard and **snapshot** API
- Multi-role agents (intake, PM, implementer, tester, reviewer, memory-updater)
- Elasticsearch-backed task/memory indices
- Settings UI for LLMs and repo integration
- **systemd user** service via `./flume` CLI
- **OpenBao-first** secrets with **legacy `.env`** support
