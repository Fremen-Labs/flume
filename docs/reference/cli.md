# Command Line Reference

The `flume` Native Engine manages all orchestration natively in Go without bleeding Docker abstractions or bash dependencies onto your host.

> [!IMPORTAT]
> All Flume execution pipelines require `sudo` isolation access securely since they map critical network sockets inside Docker Compose natively.

## The Flume Core Commands

| Command | Definition | Network & Data Matrix | Context |
| :--- | :--- | :--- | :--- |
| `flume start` | Safely evaluates standard host mappings, explicitly starting your detached Elasticsearch databases, the OpenBao KMS interface, and finally your Python UI backends. | Hydrates the internal Docker bridge cleanly. Boots Unseal Keys automatically on local port 8200 dynamically inserting them safely into `.env`. | Run this mapping to bring up your environment efficiently. **Idempotent.** |
| `flume destroy` | Triggers the Annihilation Protocol unconditionally. Destroys all containers, wipes OpenBao entirely, eradicates Docker volumes, and leaves only external state securely bound. | `docker compose down -v` across strictly the Flume target namespaces. | When fixing corrupted memory maps or attempting a fresh deploy without manual file manipulation safely. |
| `flume doctor` | Fires an immediate matrix telemetry check structurally examining running instances. Evaluates OpenBao tokens and Elastic mapping statuses using formatted terminal output. | Assesses port `lsof` conflicts securely. | When `localhost:8765` is failing to route correctly safely. |
| `flume stop` | Cleanly suspends execution pipelines without violently destroying volumes natively. | `docker compose stop`. | Conserving Macbook battery securely when you want to resume exactly the same graph safely tomorrow. |
| `flume config` | Modifies deep ecosystem settings (Docker variables, Vault keys). | Dynamically updates and restarts relevant worker topologies applying zero-downtime bounds mapping. | Injecting explicit host URLs or setting structural environment policies. |
| `flume projects` | Headless API connection triggering dynamic project scaffolding and cloning over the isolated CLI without a UI. | Bridges standard `sqlite/registry` maps securely loading HTTPS/SSH remote workspaces locally. | For automated ingestion workflows mapping Git directly into Elasticsearch ASTs without browser intervention. |
| `flume status` | Real-time swarm monitoring showing specific pipeline stages. | Pings `/health` and OpenBao nodes continuously reporting bounded network delays natively. | Evaluating worker death spirals safely without polling logs. |
| `flume logs` | Streamlined access to centralized worker logic explicitly bypassing raw Docker verbose maps. | Subscribes cleanly capturing specifically designated payload outputs natively natively truncating buffer overflows. | Deeply analyzing trace events without relying natively on Kibana interfaces. |
| `flume tasks` | Dispatch explicitly mapped job bounds natively pushing work out to running pipelines isolated perfectly. | Emits exact zero-LLM metadata payloads checking for bounds before executing LLMs. | Invoking automated RAG pipelines via CLI scripts instantly. |
| `flume workers` | Manage individual daemon threads tracking specific active LLM inference payloads isolated smoothly. | Inspects granular thread states and VRAM block commitments perfectly natively. | Profiling hardware bottlenecks running parallel task distributions across local Ollama instances securely. |

> [!NOTE]
> All interactive portions of the `flume` CLI now guarantee explicit input validation and strict injection protection explicitly terminating out-of-bounds characters securely protecting the backend APIs natively.
