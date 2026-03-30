# Local AI Inference

Flume is architected to be completely model-agnostic. While commercial models (OpenAI/Anthropic) provide excellent zero-configuration capabilities, Flume natively executes pipelines using zero-cost, privacy-preserving **Local LLMs** without ever sending a packet to the internet.

We highly recommend [Exo](https://github.com/exo-explore/exo) for Apple Unified Memory MLX clustering across multiple Mac Minis, or **Ollama** for standard local CPU/GPU bounds.

## 1. Configure Open-Weight Inference (The Bridge Caveat)

Because the Flume ecosystem strictly containerizes its Python AI Workers inside a Docker network isolated from your host, the workers **cannot** simply ping `http://localhost:11434`. Inside the worker container, `localhost` means *the container itself*. 

To route out to your Mac/Linux bare-metal machine where Exo or Ollama is running, you must configure the LLM endpoints using the `host.docker.internal` DNS bridge.

### Editing `.env`

Locate your `./.env` (copied from `.env.example` during installation). 

Since both Exo and Ollama conveniently utilize OpenAI-compatible REST shapes, you will configure Flume to think it's talking to OpenAI natively while intercepting the base URLs.

```bash
# Set provider exactly to 'openai' so the client shapes the requests correctly
LLM_PROVIDER=openai

# Optional: Set the local model name (e.g. 'llama3.1', 'mixtral', 'qwen2.5-coder')
LLM_MODEL=qwen2.5-coder

# Exo Configuration (Host Network Bridge mapping to MLX API)
LOCAL_EXO_BASE_URL=http://host.docker.internal:52415/v1

# OR: Ollama Configuration (Host mapping to standard API)
# LOCAL_EXO_BASE_URL=http://host.docker.internal:11434/v1
```

> [!WARNING]
> Do NOT set `LOCAL_EXO_BASE_URL=http://127.0.0.1:11434`. Your workers will violently crash with `ConnectionRefused` because they will be probing their own isolated Docker container for an LLM that isn't there!

## 2. Booting the Orchestration

Once configured, boot the ecosystem identically:

```bash
flume start
```

Any task dispatched to the `implementer` or `pm` matrix will now route strictly through `host.docker.internal`, offloading all cognitive execution to your bare-metal inference daemon securely.
