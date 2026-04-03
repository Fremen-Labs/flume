# Flume Settings & System Configuration

The Flume Settings Dashboard natively controls the secure operational boundaries of your ecosystem. Because Flume strictly isolates your execution via the Go + Docker bridge, the Settings UI dictates how environmental variables are safely mapped, hot-reloaded, and stored entirely offline securely.

---

## 1. LLM Provider Configuration
The most explicit requirement for Flume agent intelligence is your connection block.
- **Provider Arrays**: Flume supports dynamic scaling natively. Switch between **OpenAI**, **Anthropic**, **Gemini**, or local inference endpoints like **Ollama** and **Exo**.
- **Exo Auto-Discovery**: Flume now includes native **Exo Cluster Auto-Discovery UI components**. Selecting the Exo provider dynamically interrogates the network topology to find distributed GPU clusters without manual host IP mapping.
- **The "Local Network" Bridge**: When assigning custom OpenWeights parameters, toggle the Route Type to **Network** and manually define your bridge (e.g. `host.docker.internal`). 
- **OAuth Codex Support**: If you subscribe to ChatGPT endpoints securely, Flume natively detects local `codex app-server` tunnels on port `64132`. This allows zero-API-key execution via `npx @openai/codex app-server`.

> [!WARNING]
> Storing API Keys safely natively writes payloads to `worker-manager/llm_credentials.json` internally protected by OpenBao encryption blocks. Ensure the Vault is booted!

## 2. Repo Credentials (Pats)
For Flume's autonomous Git Workers to clone, diff, and PR natively, you must inject tokens cleanly. 

- **GitHub Integrations**: Add standard Classic or Fine-Grained `ghp_` PATs. Set exactly one token to **Active**. Under the hood, Flume injects this securely into the `GH_TOKEN` environment dynamically per agent.
- **Azure DevOps (ADO)**: Assign an Organization URL map and explicitly define the `ADO_TOKEN` bounds natively.

## 3. Mission Control: Kill Switch & Telemetry
In compliance with CTO-grade disaster mitigation guidelines, the settings panel now encapsulates a true **Mission Control Kill Switch**.

- **Netflix-Scale Telemetry**: Dynamically surfaces active Docker worker VRAM ingestion rates, total failure blocks incurred, and explicit partial-failure states.
- **The Kill Switch**: If an LLM bleed spiral is detected, engaging the Kill Switch instantly invokes `docker-compose stop` organically severing all Swarm worker network routes, mathematically preventing out-of-control API expenditures, then gracefully restarts dependent daemons safely on `resume`.

## 4. System Infrastructure Configuration
If you run Flume outside of the bundled docker topology, the Dashboard forces you to align telemetry natively.

- **Elasticsearch Overrides**: Define custom Node host ports and strict `es_api_key` payload configurations safely.
- **OpenBao Overrides**: Overwrite the native `http://127.0.0.1:8200` boot paths with remote clustered parameters cleanly.

## 5. The `Restart Services` Command & Dynamic LLM Sync
Flume strictly caches operational dependencies natively in its fast-memory execution. When you save an LLM Configuration, alter a PAT token, or bind a new Elasticsearch endpoint, you will be prompted to **Restart Services**.

This executes the new **Dynamic LLM Model Sync architecture**. It does not trigger a violent `flume destroy`. It securely signals the Worker-Manager to hydrate the newest OpenBao/Vault secrets into environmental blocks natively and performs safe soft-reloads without orphaning active executions!
