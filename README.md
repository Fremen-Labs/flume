# Flume

An AI-powered agent workflow platform: plan, implement, test, and review code changes using a coordinated team of LLM agents, with a real-time dashboard to monitor everything.

**Full install & operations guide:** [`install/README.md`](install/README.md)

---

## Architecture (overview)

```text
┌─────────────────────────────────────────────────────────────────┐
│                        Browser (8765)                           │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP
┌────────────────────────────▼────────────────────────────────────┐
│  Dashboard (`server.py`) — API, static UI, Settings               │
│  • Loads `flume.config.json` + optional `.env`                    │
│  • Hydrates secrets from OpenBao KV → process env                 │
└────────────┬───────────────────────────────┬────────────────────┘
             │                               │
             │ ES API                          │ spawns / monitors
             ▼                                 ▼
┌────────────────────────┐      ┌─────────────────────────────────┐
│  Elasticsearch 8.x     │      │  Worker manager + worker_handlers │
│  (tasks, memory, …)    │◄─────┤  (same config / OpenBao as above) │
└────────────────────────┘      └────────────────┬────────────────┘
                                                 │
                                                 ▼
                                        LLM providers (Ollama, …)

┌────────────────────────┐
│  OpenBao (optional)    │  KV path e.g. `secret/flume` — ES_API_KEY,
│  Secrets not on disk   │  LLM_API_KEY, GH_TOKEN, EXECUTION_HOST, …
└────────────────────────┘
```

- **Elasticsearch** is the system of record for tasks, handoffs, failures, memory, etc.
- **Dashboard** and **workers** read the same configuration: **OpenBao-first** (recommended) or **legacy `.env`** at the Flume repo root.
- **Bootstrap file** `flume.config.json` (non-secret) points at OpenBao; **tokens and API keys** live in **OpenBao KV** (or only in `.env` if you use legacy mode).

---

## Quick start (one command)

```bash
cd ~/flume          # git clone root, or extracted package directory
bash setup.sh
```

`setup.sh` runs `install/install.sh` (git) or `install.sh` (package), then (on git clones) builds the frontend, ensures ES credentials, creates indices, installs the **systemd user** dashboard service, and starts it.

Control the dashboard:

```bash
./flume start | stop | restart | status | logs | enable | disable
./flume codex-oauth login   # optional: ChatGPT / Codex OAuth for OpenAI
```

Open `http://<your-host>:8765`. The dashboard **starts workers automatically** by default (`FLUME_AUTO_START_WORKERS=1`). To run workers separately (or disable auto-start), set `FLUME_AUTO_START_WORKERS=0` and use:

```bash
# Git clone                         # Extracted package
bash src/worker-manager/run.sh      bash worker-manager/run.sh
```

---

## Installation steps (`install.sh`)

The installer is **non-interactive** by default. It performs:

| Step | Name | What it does |
|------|------|----------------|
| 1 | **Check dependencies** | `verify-deps.sh` — Python 3.9+, git, pgrep, curl; optional `gh`, `openbao`, node |
| 2 | **Elasticsearch** | If ES is not running or API key is missing, runs `install-elasticsearch.sh` (may use `sudo`); may write `install/.es-bootstrap.env` |
| 3 | **OpenBao, GitHub CLI & Codex** | Best-effort: `openbao`, `gh`, **Node.js LTS** + **`codex`** (`npm i -g @openai/codex`) |
| 4 | **Configure runtime** | Creates/updates **`.env`** from template; applies ES bootstrap; creates **`flume.config.json`** from example (OpenBao bootstrap); optionally **syncs ES credentials to OpenBao** if `BAO_TOKEN` / `VAULT_TOKEN` / `OPENBAO_TOKEN` is set |
| 5 | **Elasticsearch indices** | `create-es-indices.sh` (can hydrate `ES_*` from OpenBao via `hydrate-openbao-env.py` if no key in `.env`) |
| 6 | **Workspace** | `projects.json`, `sequence_counters.json`, worker state, optional `flume` systemd service install |
| 7 | **Done** | Prints URLs and `./flume` usage |

**Layouts**

- **Git clone:** repo root contains `install/install.sh`, application under **`src/`** (`src/dashboard`, `src/worker-manager`, …).
- **Package tarball:** flattened tree — `install.sh` at root, `dashboard/`, `worker-manager/` next to it (no `src/`).

---

## Configuration (summary)

| Layer | Purpose |
|--------|---------|
| **`flume.config.json`** (repo root) | OpenBao address, KV mount/path, **`tokenFile`** path (chmod 600). No API keys in this file. |
| **OpenBao KV** e.g. `secret/flume` | `ES_URL`, `ES_API_KEY`, LLM keys, `GH_TOKEN`, `EXECUTION_HOST`, … (same names as `.env` keys). |
| **`.env`** (optional, legacy) | Full flat config; installer still creates it with defaults. Merged then overridden by OpenBao when both are used. |

Details, LLM providers, OAuth, and troubleshooting: **[`install/README.md`](install/README.md)**.

---

## Build a release package

```bash
bash build-package.sh [VERSION]
# Artifact: dist/flume-<VERSION>.tar.gz (+ .sha256)
```

Extract, then `bash install.sh` or `bash setup.sh` inside the extracted directory.

---

## Development / UI rebuild

The package ships pre-built `frontend/dist`. To rebuild from React sources (git clone):

```bash
cd src/frontend/src && npm install && npm run build
```

---

## Dependencies (short list)

**Required:** Python 3.9+, git, pgrep, curl, Elasticsearch 8.x (installed by installer or pre-provisioned).

**Optional:** OpenBao **server** (you run it; installer installs **CLI**), `gh`. **Node + Codex CLI** are installed by the installer when `sudo` is available (frontend rebuild + OAuth planning).

See [`install/README.md`](install/README.md) for the full table and scripts.
