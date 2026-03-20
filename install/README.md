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
| **OpenBao CLI / `gh`** | Installed by the installer (best-effort) for local secret management and GitHub PR creation. |

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
2. On **git clones**, runs `npm install && npm run build` under `src/frontend/src` if `npm` exists.
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
| **1** | Check dependencies | `setup/verify-deps.sh` — required: `python3`, `git`, `pgrep`, `curl`. Optional: `gh`, `openbao` CLI, `node`, running Elasticsearch. |
| **2** | Elasticsearch | If ES is down or `install/.es-bootstrap.env` lacks a key, runs `setup/install-elasticsearch.sh` (often via `sudo`). May run `bootstrap-es-credentials.sh` on a TTY if needed. |
| **3** | OpenBao & GitHub CLI | `setup/install-openbao.sh`, `setup/install-gh.sh` (skip if already on `PATH`). |
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

Workers: `bash src/worker-manager/run.sh` (git) or `bash worker-manager/run.sh` (package).

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
| Anthropic / Gemini / xAI / Mistral / Cohere | `anthropic`, `gemini`, … | API keys |

OAuth helper: `bash setup/openai-oauth.sh bootstrap` (see section below).

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

## OpenAI OAuth

```bash
# Git clone:
bash install/setup/openai-oauth.sh bootstrap
bash install/setup/openai-oauth.sh refresh

# Package root:
bash setup/openai-oauth.sh bootstrap
bash setup/openai-oauth.sh refresh
```

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
| Dashboard won’t start | `python3 --version` ≥ 3.9; **`.env` or `flume.config.json`** exists; `journalctl --user -u flume-dashboard`; run `bash src/dashboard/run.sh` (git) in foreground for errors. |
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
