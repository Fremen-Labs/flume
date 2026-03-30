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
