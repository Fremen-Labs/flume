<div align="center">

# FLUME
## V3 Autonomous Edge Orchestrator

[![License: MIT](https://img.shields.io/badge/License-MIT-purple.svg)](https://opensource.org/licenses/MIT)
[![Elasticsearch: 8.x](https://img.shields.io/badge/Elasticsearch-8.x-blue.svg)](https://www.elastic.co/)
[![Backend: Python 3.9+](https://img.shields.io/badge/Backend-Python_3.9+-yellow.svg)](https://www.python.org/)
[![GUI: React & Vite](https://img.shields.io/badge/GUI-React_18-cyan.svg)](https://reactjs.org/)
[![CLI: Go 1.21+](https://img.shields.io/badge/CLI-Go_1.21+-green.svg)](https://golang.org/)
[![Docker: Required](https://img.shields.io/badge/Docker-Required-blue.svg)](https://www.docker.com/)

*An AI-powered agent workflow platform for planning, implementing, testing, and reviewing codebase changes natively using local hardware clusters.*

</div>

---

The Flume V3 ecosystem is managed entirely by a singular, high-performance Go CLI (`flume`). This executable provides a pristine, zero-dependency interface to your terminal while cleanly orchestrating a containerized matrix (Elasticsearch, OpenBao, Python Workers) protecting your absolute host OS entirely.

### ✨ V3.2 Feature Highlights
- **10+ Native CLI Mappings:** Deeply configure, execute RAG tasks, monitor statuses, and pull container logs directly through the `flume` terminal executable securely.
- **Remote Git Integration:** The Dashboard natively clones any remote `https://` or `ssh://` repository asynchronously without breaking boundaries.
- **Mission Control Kill Switch:** Immediate Docker Swarm termination routes explicitly block LLM budget-bleed spirals securely.
- **Exo Auto-Discovery:** Instant localized integration bridging into decentralized Macbook GPU grids locally.
- **Kubernetes-Grade State Storage:** Elastic & OpenBao mounts natively mapped avoiding brittle orchestration faults. 
- **The ReleaseFlow Matrix:** Full CI/CD pipelines deploying multi-architecture binaries and analyzing CVEs natively on merge.

## ⚡ Quick Start (Time-to-Value)

> **Docker Desktop or OrbStack is mandatory.** The Go CLI only orchestrates containers—zero host pollution.

```bash
# 1. Download the ecosystem
git clone https://github.com/Fremen-Labs/flume.git
cd flume

# 2. Compile and Install the CLI Native Engine 
go build -o flume cmd/flume/main.go
sudo cp ./flume /usr/local/bin/flume

# 3. Boot the Matrix
# Start the Hub-and-Spoke interactive configuration wizard
flume start 

# OR - Boot programmatically using Infrastructure-as-Code (IaC) YAML
flume start --config ./flume-mesh.yml
```

> **Note on Configuration:** The interactive wizard loops infinitely, allowing you to seamlessly integrate both public Frontier API endpoints (Grok, Anthropic, Gemini) and Local hardware nodes (Ollama/Exo) on the exact same topological plane securely.

*The Flume orchestrator dashboard will initialize concurrently at [http://localhost:8765](http://localhost:8765).*

---

## 📚 Documentation Architecture

We follow the elite **Diátaxis Documentation Framework**, segmenting the Flume manuals securely into specific user intents. Please select the guide you need below:

### ⚙️ Operations Guides
*How to handle the explicit lifecycle of the deployment safely.*
- **[System Lifecycle (`Install / Update / Destroy`)](./docs/operations/lifecycle.md)**: Master the deterministic states of Flume natively without breaking bounds.
- **[Troubleshooting Matrices](./docs/operations/troubleshooting.md)**: Pinpoint solutions for memory mapping faults, orphaned Docker network bindings, and container delays natively.

### 🔮 Walkthroughs & Capabilities
*How to unlock the maximum potential of Flume's native AI edge capabilities.*
- **[GUI Onboarding & Dispatch](./docs/walkthroughs/gui-onboarding.md)**: Zero-friction visual dashboard setup connecting active React 18 interfaces to your absolute repository safely.
- **[API Headless Onboarding](./docs/walkthroughs/api-onboarding.md)**: Explicit `curl` API mappings to deterministically invoke Agent planning chains outside a UI context safely.
- **[Zero-Cost Local Inference Setup](./docs/walkthroughs/local-inference.md)**: Bypass OpenAI completely and run open-weight models (via Exo / Ollama) through the `host.docker.internal` bridge smoothly.
- **[Zero-LLM Meta-Critic CI/CD Pipeline](./docs/walkthroughs/meta-critic-pipeline.md)**: Secure your repository with our mathematically exact automated garbage-collected code linter securely.
- **[Elastro Code Graph Indexing](./docs/walkthroughs/elastro-code-graph.md)**: Replace flawed token-RAG with AST semantic maps natively parsing variable dependencies locally.
- **[Inception Skills Integration](./docs/walkthroughs/inception-skills.md)**: Construct deterministic, natively compiled Golang skill handlers leveraging localized Elastic Context RAG nodes without parsing latency.

### 📊 Observability & Metrics
*How to monitor real-time gateway performance, VRAM constraints, and fallback frequency.*
- **[Advanced Telemetry Uplink](./docs/operations/observability-telemetry.md)**: Guide on configuring the zero-dependency Prometheus `GET /metrics` backend and parsing the React-based visual dashboard arrays.
- The Flume gateway exposes a zero-dependency **Prometheus Text Exposition Endpoint natively at `GET /metrics`** (port `8766` by default or internal via Docker).
- **Core Insights:** Tracks `flume_ensemble_requests_total`, `flume_escalation_total`, `flume_ensemble_score_histogram`, and internal Go heuristics (`go_goroutines`, `go_memstats_alloc_bytes`).
- Easily ingested natively by `Metricbeat` or raw Prometheus instances. Example Grafana queries:
  - Base Local Fallback Rate: `rate(flume_escalation_total[5m])`
  - Active Evaluator Models: `flume_active_models`
  - High VRAM Constraint Drops: `increase(flume_vram_pressure_events_total[1h])`

### 📘 Reference Materials
*Deep architectural explanations of Flume's matrix integrations.*
- **[CLI Parameter Maps](./docs/reference/cli.md)**: Safely inspect exactly how `start`, `destroy`, and `doctor` flags behave cleanly.
- **[The Container Matrix Architecture](./docs/reference/architecture.md)**: Understand the strict isolation paradigm splitting the execution logic from your raw SSD safely.
- **[Settings & System Configuration](./docs/reference/settings-management.md)**: Deep dive into the Flume configuration GUI managing strict LLM limits, Vault keys, and GitHub/ADO tokens securely.
- **[Provider Architecture Interface](./docs/reference/providers.md)**: Detailed mapping of internal gateway translation protocols parsing Anthropic nodes and Gemini execution invariants.
- **[Elasticsearch Topology Map](./docs/reference/architecture.md#elasticsearch-architecture-map)**: Persistent index topology representing Flume's distributed schema patterns.

