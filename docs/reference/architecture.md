# Flume Orchestration Architecture

Flume strictly defines a separation of concerns between its **Execution Boundary** and its **Orchestration Brain**. 

The goal is absolute system parity: the exact same isolated execution environment that processes LLM payloads on a complex Linux kubernetes cloud deployment should flawlessly run on your M1 Macbook natively via OrbStack or Docker Desktop.

## The Two-Tier Architecture

### Tier 1: The Native Go Orchestrator

The only executable you run directly on your host macOS/Linux system is `flume`. It is a compiled Go binary cleanly mapped without dependency pollution.

The Go orchestrator effectively eliminates bash script brittleness. It is mathematically precise:
1. It validates Elasticsearch's port availability `9200`.
2. It blocks execution if Docker Desktop/OrbStack isn't explicitly alive.
3. It intercepts and securely injects your `OPENAI_API_KEY` into local container states.
4. It initializes the `OpenBao` infrastructure safely.

### Tier 2: The Container Matrix

Inside the Docker Network Bridge operates the execution backend natively communicating across internal IPs cleanly isolated from your browser.

## Persistence Paradigm & Kubernetes-Grade Storage

The architecture guarantees resilient, cloud-native storage patterns even when running natively on a local host.

- **Elasticsearch (State DB & AST RAG)**: Flume does not use PostgreSQL. Instead, all projects, prompts, user configuration hashes, worker memory nodes, and Elastro RAG graphs natively map deeply into scalable Elastic indexes locally. 
- **OpenBao (KMS Layer)**: The orchestration matrix never stores API keys in plaintext anywhere but your absolute execution bounds.
- **Kubernetes-Grade State Storage**: Resolving the brittle orchestration of the past, both Elasticsearch and OpenBao are now natively backed by robust, idempotent local persistent volumes. The `OPENBAO_TOKEN` and root initialization maps securely mount directly into local nodes, ensuring that Unseal Keys naturally persist and auto-recover organically across hard system `reboot` and `docker compose down` teardowns.
### Elasticsearch Architecture Map

*Persistent index topology representing Flume's distributed data structures.*

#### 1. Code & Task Engine
*The definitive source-of-truth ensuring execution consistency across disjoint local workers.*
- **`flume-projects`** 
  - **Who**: Worker-Manager, Gateway, UI.
  - **What & Where**: Tracks cloned states and bounds of git repositories mapped within ES.
  - **When**: Updated during initial onboarding and dynamic branch generation.
  - **Why**: Acts as the top-level context boundary, securely trapping agent operations inside explicit local directories.
- **`agent-task-records`** 
  - **Who**: Core Agent Swarms, Dashboard, Orchestrators.
  - **What & Where**: The canonical tracking layer containing sub-objects for agent reasoning (`agent_log`, `execution_thoughts`) and explicit worktree states.
  - **When**: Mutated iteratively during every agent action loop.
  - **Why**: It is the central nervous system resolving LLM drift; its absolute state persists reasoning across soft reboots and node migrations safely.
- **`flume-counters`**
  - **Who / What**: Monotonic sequence generators for generating collision-resistant IDs natively in ES.
  - **Why**: Circumvents the necessity for heavy relational locking mechanisms (e.g., PostgreSQL).

#### 2. Work Pipeline Telemetry
*Event-driven observability logging the critical path of the Autonomous Agents.*
- **Indices**: `agent-review-records`, `agent-handoff-records`, `flume-task-events`, `agent-failure-records`, `agent-provenance-records`
- **Who**: Orchestrators, Testing Suites, Human Checkpoints.
- **What & When**: They construct immutable trails isolating success operations, evaluation rebukes, and manual interventions immediately upon transition.
- **Why**: Enforces deterministic accountability. If an agent loops maliciously or fails testing 3x, these tables provide the forensic artifacts without polluting the main Task record.

#### 3. Mesh & Orchestration
*Real-time decentralized node clustering metrics protecting against budget bleeds and overload.*
- **`flume-node-registry` & `flume-routing-policy`**
  - **Who**: The Go Gateway's Multi-Processor.
  - **What & Why**: Registers connected hardware limits (Ollama Mac Minis vs Windows GPU boxes) and rules for falling back to Cloud Frontier providers securely.
- **`agent-system-workers` & `agent-system-cluster`**
  - **Who / When**: Written continuously by worker heartbeats.
  - **Why**: Allows execution pipelines to respect the 'Mission Control Kill Switch' efficiently, halting Docker containers mid-generation if the swarm enters a gridlocked state.
- **`agent-plan-sessions`**
  - **Who / Why**: Preserves raw human-agent conversational contexts (the 'Intake' loop) prior to compiling them into structurally sound execution Tasks natively.

#### 4. OpenBao Hardware Vault
*Kubernetes-grade secret decryption mappings avoiding plain-text drift.*
- **Indices**: `flume-llm-credentials`, `flume-ado-tokens`, `flume-github-tokens`
- **Who**: The Gateway KMS Module.
- **What**: Safely matches non-sensitive active UI labels (e.g., `Default Entra ID`) tightly to their encrypted OpenBao counterparts natively.
- **Where & Why**: Kept entirely within explicit system bounds to structurally decouple dynamic API payload routing from raw credential decryption.

#### 5. Config & Observability
*Variables guiding logic execution seamlessly across UI and CLI environments.*
- **Indices**: `flume-llm-config`, `flume-settings`, `flume-telemetry`, `agent-token-telemetry`, `flume-config`, `flume-agent-models`, `agent-security-audits`
- **Why**: Completely eliminates brittle `.env` dependencies or scattered local JSON files. By pushing constraints (e.g., overriding specific role endpoints) natively into Elasticsearch, multi-node setups behave uniformly globally.

#### 6. Memory & AST Core
*Defeating naive 'vector-stuffing' AI limits through deterministic structure.*
- **`flume-elastro-graph`**
  - **Who / What**: Generated heavily by `elastro_sync`. Stores exact semantic Deterministic Abstract Syntax Trees mapping your codebase dependencies strictly.
  - **Why**: Replaces vector noise. Agents natively query exact function callers and variable bindings precisely natively.
- **Indices**: `agent_semantic_memory`, `flow_tools`, `agent_knowledge`, `agent-memory-entries`
  - **Who / Why**: Utilized directly by Agent Toolchains. They persist highly constrained semantic heuristics, specific manual knowledge overrides, and strict TTL-decayed vectors avoiding long-term context drift globally.
