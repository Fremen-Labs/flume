# Local AI Inference

Flume is architected to be completely model-agnostic. While commercial models (OpenAI/Anthropic) provide excellent zero-configuration capabilities, Flume natively executes pipelines using zero-cost, privacy-preserving **Local LLMs** without ever sending a packet to the internet.

We highly recommend [Exo](https://github.com/exo-explore/exo) for Apple Unified Memory MLX clustering across multiple Mac Minis, or **Ollama** for standard local CPU/GPU bounds.

## 1. Configure Open-Weight Inference (The Bridge Caveat)

Because the Flume ecosystem strictly containerizes its Python AI Workers inside a Docker network isolated from your host, the workers **cannot** simply ping `http://localhost:11434`. Inside the worker container, `localhost` means *the container itself*. 

To route out to your Mac/Linux bare-metal machine where Exo or Ollama is running, you must configure the LLM endpoints using the `host.docker.internal` DNS bridge.

### Configuring via the Settings UI

All LLM configuration is managed through the **Settings → LLM** panel in the Flume dashboard. No manual file editing is required.

1. Open the dashboard and navigate to **Settings → LLM**.
2. Select **Provider**: `Ollama` for local inference or `OpenAI-compatible (custom)` for Exo.
3. Set the **Route Type** to **Network** and enter `host.docker.internal` as the host with the appropriate port:
   - **Exo (MLX cluster):** port `52415`
   - **Ollama:** port `11434`
4. Set your **Model** (e.g. `qwen2.5-coder`, `llama3.2`, `mixtral`).
5. Click **Save Settings**.

Flume persists all settings to OpenBao (Vault) and syncs them to worker processes automatically on save — no restart required.

Since both Exo and Ollama implement OpenAI-compatible REST endpoints, selecting `OpenAI-compatible (custom)` and pointing the base URL to `http://host.docker.internal:<port>/v1` works for either runtime.

## 2. Booting the Orchestration

Once configured, boot the ecosystem identically:

```bash
flume start
```

Any task dispatched to the `implementer` or `pm` matrix will now route strictly through `host.docker.internal`, offloading all cognitive execution to your bare-metal inference daemon securely.
