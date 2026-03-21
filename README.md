<div align="center">

# FLUME

[![License: MIT](https://img.shields.io/badge/License-MIT-purple.svg)](https://opensource.org/licenses/MIT)
[![Elasticsearch: 8.x](https://img.shields.io/badge/Elasticsearch-8.x-blue.svg)](https://www.elastic.co/)
[![Backend: Python 3.9+](https://img.shields.io/badge/Backend-Python_3.9+-yellow.svg)](https://www.python.org/)
[![Frontend: React & Vite](https://img.shields.io/badge/Frontend-React_18-cyan.svg)](https://reactjs.org/)

*An AI-powered agent workflow platform for planning, implementing, testing, and reviewing codebase changes natively using local hardware clusters.*

</div>

---

## Quick Start

Flume provides a unified command-line interface (`flume`) to manage dependencies, configurations, and background services easily.

```bash
# 1. Enter the Repository
cd ~/flume

# 2. Install dependencies (Node, Python, Elasticsearch)
./flume install

# 3. Configure your environment (LLM Provider, Git Identity)
./flume onboard

# 4. Start the dashboard and background agent workers
./flume start
```

*The dashboard will be available at [http://localhost:8765](http://localhost:8765).*

---

## Command Line Interface (`./flume`)

You can manage all lifecycle and daemon operations cleanly through the `./flume` executable:

| Command | Description |
| :--- | :--- |
| `./flume install` | Runs the automated installer for dependencies, virtual environments, and the Elasticsearch bootstrap. |
| `./flume onboard` | Interactive configuration wizard for your `.env` (LLM Provider, Git Identity, Execution Host). |
| `./flume start` | Starts the background React Interface and Python agent worker daemons concurrently. |
| `./flume update` | Pulls the latest `main` branch, rebuilds UI artifacts, and gracefully restarts processes. | 
| `./flume status` | Display the explicit status of asynchronous background services. |
| `./flume logs` | Tail system journal logs mapping dashboard and agent worker pools. |

---

## Architecture Overview

Flume utilizes an asynchronous, multi-agent topology driven by Python multiprocessing. Agents (Planners, Implementers, Reviewers, Testers) function independently by polling a unified Elasticsearch graph, heavily leveraging dedicated git worktrees to isolate tasks.

```text
┌─────────────────────────────────────────────────────────────────┐
│        [ Mission Control Dashboard ]  — Live Telemetry Radar    │
└────────────────────────────┬────────────────────────────────────┘
                             │ (WebSocket / REST JSON State)
┌────────────────────────────▼────────────────────────────────────┐
│      [ Flume CLI Gateway ] (`./flume` daemon manager)           │
│  • Reads `flume.config.json` + `OpenBao` Vault Secrets          │
└────────────┬───────────────────────────────┬────────────────────┘
             │                               │
       READ / WRITE                    SPAWN / ISOLATE
             ▼                               ▼
┌────────────────────────┐      ┌─────────────────────────────────┐
│  [ The Knowledge Graph]│      │   [ The Agent Swarm ]           │
│   Elasticsearch 8.x    │◄─────┤   Multiprocessing Agent Daemons │
│   (System of Record)   │      │   (Isolated Python Workers)     │
└────────────────────────┘      └────────────────┬────────────────┘
                                                 │
                                           OFFLOAD EXECUTION
                                                 ▼
                                ┌─────────────────────────────────┐
                                │ [ Execution Engine ]            │
                                │ • Local Exo Cluster (Qwen)      │
                                │ • Local Ollama Binary (Llama3)  │
                                │ • OpenAI / Anthropic APIs       │
                                └─────────────────────────────────┘
```

> **Security Note**: Secrets such as API keys are not strictly required on disk. The system fully supports secure `OpenBao` Vault endpoints for injecting credentials via HTTP at runtime.

---

## Live Mission Control

<<<<<<< HEAD
```bash
cd ~/flume          # git clone root, or extracted package directory
./flume setup
```

`./flume setup` installs dependencies when possible, runs the installer, builds the frontend, starts the dashboard in the background, starts Codex app-server in the background, and prints the next OAuth step.

Control the dashboard:

```bash
./flume setup
./flume start | stop | restart | status | logs | enable | disable
./flume codex-oauth login-browser   # recommended: ChatGPT / Codex OAuth for OpenAI
./flume codex-app-server status     # background Codex app-server status
# Worker roles can use either OpenAI OAuth/Codex app-server or any saved API-key provider.
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
| 3 | **OpenBao & GitHub CLI** | Best-effort install of `openbao` and `gh` to `/usr/local/bin` |
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

**Optional:** OpenBao **server** (you run it; installer installs **CLI**), `gh`, Node (frontend rebuild).

See [`install/README.md`](install/README.md) for the full table and scripts.
Node (frontend rebuild).

See [`install/README.md`](install/README.md) for the full table and scripts.
=======
The React UI provides real-time visibility into the agent swarm operations natively:

1. **Live Mission Radar**: Actively parses `state.json` abstract syntax trees to show exactly which agents are resolving tasks, mapping modified files, and processing delivery workflows.
2. **OpenBao Security Monitor**: Tracks agent vault checkouts and secret access rates continuously natively inside the `/security` layout.
>>>>>>> main
