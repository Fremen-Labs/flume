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

The React UI provides real-time visibility into the agent swarm operations natively:

1. **Live Mission Radar**: Actively parses `state.json` abstract syntax trees to show exactly which agents are resolving tasks, mapping modified files, and processing delivery workflows.
2. **OpenBao Security Monitor**: Tracks agent vault checkouts and secret access rates continuously natively inside the `/security` layout.
