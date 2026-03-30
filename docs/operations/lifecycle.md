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

# 3. Define the Environment Matrix
cp .env.example .env

# 4. Boot the Ecosystem
flume start 
```

> [!NOTE]
> During a cold boot, Flume dynamically spawns highly secure Unseal Keys and Root Tokens natively for the OpenBao KMS instance. These keys are violently and automatically injected into your local `.env`. Ensure your `.env` is never committed to Git.

## 2. Update & Synchronization (Day 2)

Because the Python AI Workers and the Dashboard execute statelessly inside the Docker bridge, updating the ecosystem is completely non-destructive to your tasks or memory blocks.

### The Sync Process
1. Pull the latest commits from the repository: `git pull origin main`
2. If `cmd/flume/main.go` or other orchestrator binaries have changed, recompile:
   `go build -o flume cmd/flume/main.go && sudo cp ./flume /usr/local/bin/flume`
3. Execute `flume start` (or gracefully restart the containers `docker compose restart`).

> [!TIP]
> Executing `flume start` over an existing active installation behaves as an **idempotent sync**. It applies new environment variables or container configurations without tearing down your persistent volumes.

## 3. Destruction (The Annihilation Protocol)

When rotating deployments or debugging cluster corruption, you may need to purge the ecosystem.

```bash
flume destroy
```

> [!CAUTION]
> The `flume destroy` command completely obliterates all Docker bounds (`docker compose down -v`), violently cleansing cached OpenBao and Elasticsearch volumes. Any state not backed up will be permanently lost.

**Persistence Guarantee**: The `flume destroy` execution strictly preserves your local `./.env` mapping, your `projects.json` arrays, and your Dashboard UI layout parameters locally on your hard drive. Future cold boots will perfectly adopt these configurations.
