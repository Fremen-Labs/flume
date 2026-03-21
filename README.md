<div align="center">

# 🌊 FLUME
### The Autonomous Engineering Frontier

[![License: MIT](https://img.shields.io/badge/License-MIT-purple.svg)](https://opensource.org/licenses/MIT)
[![Elasticsearch: 8.x](https://img.shields.io/badge/Elasticsearch-8.x-blue.svg)](https://www.elastic.co/)
[![Backend: Python 3.9+](https://img.shields.io/badge/Backend-Python_3.9+-yellow.svg)](https://www.python.org/)
[![Frontend: React & Vite](https://img.shields.io/badge/Frontend-React_18-cyan.svg)](https://reactjs.org/)

*Plan, implement, test, and review production-grade codebase mutations using a relentlessly coordinated swarm of LLM agents running natively on your hardware.*

</div>

---

## ⚡ The Swarm Deployment (Quick Start)

We have engineered Flume to deploy its complex, multi-agent architecture **effortlessly**. Forget hunting for dependencies or wrestling with scripts; your entire local ecosystem is orchestrated by our unified Frontier CLI gateway.

```bash
# 1. Enter the Repository
cd ~/flume

# 2. Bootstrap the Frontier
./flume install

# 3. Configure your Telemetry
./flume onboard

# 4. Awaken the Swarm
./flume start
```

*Your dashboard will immediately initialize at [http://localhost:8765](http://localhost:8765).*

---

## 🛸 The Frontier Command Line

Whether you need to cleanly pull updates from the central repository, review background daemon logs natively, or forcefully bounce your LLM pipelines, everything routes securely through the **Flume CLI**:

| Command | Execution Logic |
| :--- | :--- |
| `./flume install` | Invokes the visual installer, aggressively auto-heals missing Node/Python dependencies, and synchronizes the Elasticsearch 8.x bootstrap. |
| `./flume onboard` | Launches the interactive wizard. Securely maps your `Execution Host`, local Git identities, and preferred Inference Engine (Exo, Ollama, OpenAI). |
| `./flume start` | Spins up the background React Interface and asynchronously triggers your Python agent pools natively. |
| `./flume update` | Natively stashes your config, pulls `main`, re-bundles the Vite React telemetry loops, and elegantly restarts your daemon tracking in seconds. | 

---

## 🏛 Ecosystem Architecture

Flume abandons simplistic scripting in favor of an **Asynchronous Swarm Topology**. The agents (Planners, Implementers, Reviewers, Testers) function entirely independently, dynamically polling the unified Elasticsearch memory graph and offloading massive LLM generations gracefully to local hardware via Exo.

```text
┌─────────────────────────────────────────────────────────────────┐
│        [ Mission Control Dashboard ]  — Live Telemetry Radar    │
└────────────────────────────┬────────────────────────────────────┘
                             │ (WebSocket / REST JSON State)
┌────────────────────────────▼────────────────────────────────────┐
│      [ Frontier CLI Gateway ] (`./flume` daemon manager)        │
│  • Reads `flume.config.json` + `OpenBao` Vault Injections       │
└────────────┬───────────────────────────────┬────────────────────┘
             │                               │
       READ / WRITE                    SPAWN / ISOLATE
             ▼                               ▼
┌────────────────────────┐      ┌─────────────────────────────────┐
│  [ The Knowledge Graph]│      │   [ The Autonomous Swarm ]      │
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
                                └─────────────────────────────────┘
```

> **Security First Context**: Secrets (like API keys) are **never** committed. The `flume.config.json` explicitly points all background workers toward secured OpenBao Vault endpoints memory-mounted over HTTP.

---

## 🧬 Live Mission Control & Telemetry

Flume natively ships with a breathtaking **React 18 User Interface**, engineered explicitly to track your multi-agent interactions beautifully.

1. **Live Mission Radar**: Replaces obsolete mock-data UI grids with active `state.json` AST parsing, showing exactly which agents are idle, which files they are mutating, and dynamically pulsing as your codebases evolves.
2. **OpenBao Hive Monitor**: Explicitly track LLM agent vault usage in real-time natively inside your `/security` layout.

---

## 🛠 Advanced Features

### Seamless Remote Updates
We push new architectural frontiers constantly. Keeping your Swarm in sync with our `main` repository takes literally zero effort:
```bash
./flume update
```

### Headless Git Isolation
When you assign a task to Flume, the internal `run_swarm.py` orchestrator checks out dedicated `git worktree` directories natively. Multiple agents can hack, validate via `meta-critic`, and dispatch GitHub Pull Requests completely parallel to one-another—all while leaving your IDE environment completely untouched!

<div align="center">
<br/>

**Flume** 
*Built for the Autonomous Age.*
</div>
