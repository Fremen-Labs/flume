<div align="center">

# FLUME

[![License: MIT](https://img.shields.io/badge/License-MIT-purple.svg)](https://opensource.org/licenses/MIT)
[![Elasticsearch: 8.x](https://img.shields.io/badge/Elasticsearch-8.x-blue.svg)](https://www.elastic.co/)
[![Backend: Python 3.9+](https://img.shields.io/badge/Backend-Python_3.9+-yellow.svg)](https://www.python.org/)
[![Frontend: React & Vite](https://img.shields.io/badge/Frontend-React_18-cyan.svg)](https://reactjs.org/)
[![CLI: Go 1.21+](https://img.shields.io/badge/CLI-Go_1.21+-green.svg)](https://golang.org/)

*An AI-powered agent workflow platform for planning, implementing, testing, and reviewing codebase changes natively using local hardware clusters.*

</div>

---

## ⚡ The V3 Edge Orchestrator

Flume is managed via a singular high-performance, totally zero-dependency Go CLI. The days of fighting virtual environments, orphaned Docker containers, and scattered Bash scripts are over. Welcome to the precise, asynchronous execution of the **Flume Autonomous Engineering Frontier**.

### Quick Start (Native Execution)

```bash
# 1. Download the ecosystem
git clone https://github.com/Fremen-Labs/flume.git
cd flume

# 2. Compile and Install the CLI Native Engine 
go build -o flume cmd/flume/main.go
sudo cp ./flume /usr/local/bin/flume

# 3. Boot the Matrix
flume start 
```

*The Flume orchestrator dashboard will initialize concurrently at [http://localhost:8765](http://localhost:8765).*

---

## 💻 Elite Architecture Diagnostics

The ecosystem relies on an intricate web of Elasticsearch indexers, OpenBao Cryptographic KMS nodes, and FastAPI Python Workers. The Go CLI manages the health bounds natively.

| Command | Objective |
| :--- | :--- |
| `flume start` | Boots the ecosystem natively. Automatically hydrates secrets via OpenBao and proxies Elasticsearch safely into your `.env`. |
| `flume destroy` | Executes the Annihilation Protocol. Completely obliterates all Docker bounds and violently cleanses cached volumes. |
| `flume doctor` | Instantiates the `lipgloss` execution diagnostics telemetry array to assert backend component health directly inside your terminal. |
| `flume help` | Evaluates absolute CLI parameter geometries dynamically. |

> **State Persistence Rule:** The `flume destroy` command strictly preserves your `./.env`, `projects.json`, and UI layout parameters while tearing down the local Docker daemons accurately. 

---

## 🏗️ Execution Topology

Flume utilizes an asynchronous, multi-agent topology driven by Python multiprocessing that isolates explicit task nodes safely while polling the Elasticsearch graph securely.

```text
┌─────────────────────────────────────────────────────────────────┐
│        [ Mission Control Dashboard ]  — Live Telemetry Radar    │
└────────────────────────────┬────────────────────────────────────┘
                             │ (REST JSON State)
┌────────────────────────────▼────────────────────────────────────┐
│      [ Flume Go CLI Gateway ] (Global Binary Object)            │
│  • Maps `docker-compose.yml` + `OpenBao` Vault Cryptography     │
└────────────┬───────────────────────────────┬────────────────────┘
             │                               │
       READ / WRITE                    SPAWN / ISOLATE
             ▼                               ▼
┌────────────────────────┐      ┌─────────────────────────────────┐
│  [ The Knowledge Graph]│      │   [ The Agent Swarm ]           │
│   Elasticsearch 8.x    │◄─────┤   Multiprocessing Agent Daemons │
│   (System of Record)   │      │   (Isolated Python Workers)     │
└────────────────────────┘      └─────────────────────────────────┘
```
