# Operational Lifecycle

The Flume V3 execution engine strictly orchestrates a native Go CLI bridging to a sandboxed Docker matrix. The operations documented here dictate how to hydrate, synchronize, and obliterate the ecosystem deterministically.

## 1. Install & Cold Boot (Day 0)

The Flume installation deliberately isolates execution logic away from your host OS by delegating python backends to containerized workers. 

**Prerequisites:**
- **Go 1.21+** (for orchestrator compilation)
- **Docker Desktop / OrbStack** (mandatory execution engine)

### Initialization Sequence
```bash
# 1. Download the ecosystem
git clone https://github.com/Fremen-Labs/flume.git
cd flume

# 2. Compile and Install the CLI Native Engine 
go build -o flume cmd/flume/main.go
sudo cp ./flume /usr/local/bin/flume

# 3. Boot Flume Core
flume up
# or: docker compose up -d --build --wait
```

> [!NOTE]
> Core is OpenBao, Elasticsearch, Gateway, and the console at http://127.0.0.1:8765. The agent mesh is not started by default. `flume start` without `--full` now refuses so it cannot boot the old stack by accident.

## 2. Update & Synchronization (Day 2)

Because the Python AI Workers and the Dashboard execute statelessly inside the Docker bridge, updating the ecosystem is completely non-destructive to your tasks or memory blocks.

### The Sync Process
1. Pull the latest commits from the repository: `git pull origin main`
2. If `cmd/flume/main.go` or other orchestrator binaries have changed, recompile:
   `go build -o flume cmd/flume/main.go && sudo cp ./flume /usr/local/bin/flume`
3. Execute `flume up` (or `docker compose up -d --build --wait`). The legacy agent mesh is `flume start --full`, not the default.

> [!TIP]
> Executing `flume up` over an existing installation is idempotent: compose reapplies container configuration without deleting OpenBao or Elasticsearch volumes.

## 3. Destruction (The Annihilation Protocol)

When rotating deployments or debugging cluster corruption, you may need to purge the ecosystem.

```bash
flume destroy
```

> [!CAUTION]
> The `flume destroy` command completely obliterates all Docker bounds (`docker compose down -v`), violently cleansing cached OpenBao and Elasticsearch volumes. Any state not backed up will be permanently lost.

**Persistence Guarantee**: The `flume destroy` command preserves the `.env` bootstrap file (infrastructure connection strings such as `ES_API_KEY` and `OPENBAO_ADDR`). All LLM configuration, API keys, and agent settings are stored in Elasticsearch and OpenBao — they survive `flume destroy` only if you have snapshot/backup policies configured.
