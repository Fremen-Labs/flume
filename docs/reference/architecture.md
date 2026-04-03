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
- **Python Workers & Dashboard**: The FastAPI server manages the UI `localhost:8765`, utilizing strictly containerized Python instances targeting the host workspace strictly securely. 
