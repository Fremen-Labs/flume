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

# 2. Compile the High-Performance Native Client
go build -buildvcs=false -o flume cmd/flume/main.go

# 3. Wipe any legacy Docker volumes cleanly
./flume destroy

# 4. Boot the High-Performance native ecosystem (Elastic in Docker, Python Dashboard natively)
./flume start --native
```

*The dashboard will be available at [http://localhost:8765](http://localhost:8765).*

*The dashboard will be available at [http://localhost:8765](http://localhost:8765).*

---

## Docker (Recommended for Development & Testing)

For isolated orchestration without host pollution, Netflix-grade deployment naturally relies on the containerized matrix natively via Docker Compose.

1. **Clone the Hub**
   ```bash
   cd ~/flume
   ```

2. **Establish Environment Configurations**
   Create the `.env` payload from the included template. Note the crucial overrides for local LLM reachability bridging across isolated proxy barriers:
   ```bash
   cp .env.example .env
   # Ensure LOCAL_EXO_BASE_URL=http://host.docker.internal:52415/v1 is set accurately for host-based inference routing.
   ```

3. **Deploy the Architecture**
   ```bash
   ./flume start
   ```

4. **Boot Validation (Cold-Start Integrity)**
   To rigorously test Cold-Start secret generation arrays flawlessly:
   ```bash
   docker compose down -v && docker compose up
   ```

5. **Diagnostic Traces**
   Monitor real-time asynchronous background topologies:
   ```bash
   docker compose logs -f
   ```

> **State Persistence**: By default, Elasticsearch mappings (`es-data`), Vault token encryptions (`openbao-data`), and dynamic workspace nodes (`flume-config`) are mounted via rigid volume persistence bounds natively preserving index memory across restarts permanently.

You can safely interact with the visual interface directly via **[http://localhost:8765](http://localhost:8765)**.

---

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

> **Netflix Reliability**: Once deployed, simply run `./flume logs` to attach standard logging filters natively diagnosing any daemon drops.

---

## Local Architecture Install & Start

```bash
cd ~/flume
cp .env.example .env
./flume install
./flume start
```

### Docker Native Path (Containers)
For Netflix-scale orchestration completely isolated within Docker, the entire cluster maps autonomously via standard compose directives relying exclusively on the provided environment templates:
```bash
cd ~/flume
cp .env.example .env
docker-compose up -d
```

The `./flume install` command autonomously bypasses systemd bloat and validates Node bindings mapping directly onto isolated Python memory execution routes automatically finding Elastic and Vault dependencies perfectly.

Control the Local Daemon clusters natively alongside the Codex OAuth endpoints:

```bash
./flume install
./flume start | stop | restart | status | logs
./flume build-ui                    # Force compile React layouts
./flume codex-oauth login-browser   # recommended: ChatGPT / Codex OAuth for OpenAI
./flume codex-app-server status     # background Codex app-server status
```

Open `http://<your-host>:8765`. The dashboard **starts workers automatically** by default (`FLUME_AUTO_START_WORKERS=1`). Target `FLUME_AUTO_START_WORKERS=0` inside your `.env` to execute manually.

---

## Advanced Dependencies
