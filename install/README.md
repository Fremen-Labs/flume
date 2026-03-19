# Flume

An AI-powered agent workflow platform — plan, implement, test, and review code changes
using a coordinated team of LLM agents, with a real-time dashboard to monitor everything.

---

## Quick Start

```bash
tar -xzf flume-<VERSION>.tar.gz
cd flume-<VERSION>/
bash install.sh
```

The installer is designed to work out-of-the-box with defaults and does not require manual `.env` editing.

Path note:
- This document assumes you are inside an extracted package directory (`flume-<VERSION>/`), so commands use `dashboard/run.sh`.
- If you are running from a git clone (`~/flume`), use `src/dashboard/run.sh` and `src/worker-manager/run.sh` instead.

### Running from a git clone (developer mode)

If you are running from a source checkout (not an extracted package), run the installer first — it will install Elasticsearch, create `.env` with valid credentials, and set up indices:

```bash
cd ~/flume
bash install/install.sh
```

Then build the frontend (if needed) and start:

```bash
cd src/frontend/src && npm install && npm run build && cd ~/flume
bash src/dashboard/run.sh
```

Open `http://<your-host>:8765`, then in another terminal run:

```bash
bash src/worker-manager/run.sh
```

---

## What's Included

```
flume/
├── install.sh              ← Run this first
├── .env.template           ← Configuration template
├── README.md               ← This file
├── dashboard/
│   ├── server.py           ← Dashboard HTTP server (Python stdlib only)
│   └── run.sh              ← Start the dashboard
├── frontend/
│   └── dist/               ← Pre-built React UI
├── worker-manager/
│   ├── manager.py          ← Agent task dispatcher
│   ├── worker_handlers.py  ← Agent execution engine
│   ├── agent_runner.py     ← Per-role LLM agent logic
│   └── run.sh              ← Start the worker manager
├── agents/                 ← System prompts for all 9 agent roles
├── memory/
│   └── es/                 ← Elasticsearch scripts and index templates
└── setup/
    ├── verify-deps.sh
    ├── install-elasticsearch.sh
    ├── install-openbao.sh
    └── create-es-indices.sh
```

---

## Requirements

| Dependency | Version | Required for |
|---|---|---|
| Python | 3.9+ | Dashboard + agents (stdlib only, no pip) |
| Elasticsearch | 8.x | Primary data store |
| git | any | Agent code operations |
| pgrep | any | Worker process detection |
| curl | any | ES health checks |
| OpenBao CLI | latest | Secrets CLI (optional, installed by installer if selected) |
| gh (GitHub CLI) | any | PR creation (optional) |
| Node.js | 18+ | Rebuilding frontend from source (optional) |

---

## Configuration

All configuration lives in a single `.env` file at the root of the Flume directory.
The installer creates and manages it from `.env.template` automatically.

To reconfigure after installation (optional):
```bash
nano .env          # edit values
bash dashboard/run.sh   # restart dashboard to pick up changes
```

### LLM Provider Options

Flume supports multiple LLM providers. Configure via the **Settings** page in the dashboard or by editing `.env`:

| Provider | `LLM_PROVIDER` | Notes |
|---|---|---|
| Local Ollama | `ollama` | No API key needed. Local or network host:port. |
| OpenAI | `openai` | API key or OAuth (Codex-style refresh token). |
| OpenAI OAuth | `openai` + `OPENAI_OAUTH_STATE_FILE` | Uses refresh-token flow; run `bash setup/openai-oauth.sh bootstrap` |
| OpenAI-compatible | `openai_compatible` | Groq, Together, Mistral, Azure, LM Studio, vLLM. Set `LLM_BASE_URL` |
| Anthropic | `anthropic` | Requires `LLM_API_KEY`. |
| Google Gemini | `gemini` | Requires `LLM_API_KEY`. |
| xAI, Mistral AI, Cohere | `xai`, `mistral`, `cohere` | Set corresponding API key in Settings. |

### Settings UI

The dashboard **Settings** page lets you:

- **Add and switch LLMs**: Choose from OpenAI, Anthropic, Gemini, xAI, Mistral, Cohere, and Ollama.
- **Local route**: Use a model hosted on the same machine (e.g. `127.0.0.1:11434` for Ollama).
- **Network route**: Point to a remote endpoint with host and optional port.
- **OAuth (Codex-style)**: Use OpenAI OAuth instead of an API key; refresh tokens from the Settings page or `bash setup/openai-oauth.sh refresh`.

After saving, **restart** the dashboard and worker-manager for changes to take effect.

---

## Running Flume

### 1. Start the Dashboard

```bash
bash dashboard/run.sh
```

Open your browser at `http://<your-host>:8765`

### 2. Start the Agent Workers

In a separate terminal:

```bash
bash worker-manager/run.sh
```

Or use the **Start Workers** button inside the dashboard.

### 3. Create Your First Project

1. Open the dashboard
2. Go to **Projects → New Project**
3. Enter a name and a git repository URL to clone (or leave blank to start fresh)
4. Click **Create**

---

## OpenAI OAuth (No OpenClaw runtime dependency)

If you want Flume to use OpenAI OAuth instead of a static API key:

```bash
bash setup/openai-oauth.sh bootstrap
```

What this does:
- Imports token data from OpenClaw profile **if available** on the same machine
- Refreshes access token via OpenAI OAuth token endpoint
- Updates `.env` (`LLM_PROVIDER=openai`, `LLM_API_KEY`, `OPENAI_OAUTH_*`)

Then restart services:

```bash
bash dashboard/run.sh
bash worker-manager/run.sh
```

You can later refresh again with:

```bash
bash setup/openai-oauth.sh refresh
```

### 4. Add Work

Use **Intake** to describe what you want built. The AI planning assistant will
decompose your request into epics → features → stories → tasks and queue them
for the agents.

---

## Elasticsearch Setup

If you need to install Elasticsearch from scratch:

```bash
sudo bash setup/install-elasticsearch.sh
```

After installation, Flume writes bootstrap ES credentials to `.es-bootstrap.env`,
and `install.sh` auto-applies them to your `.env`.

To create the required indices:

```bash
bash setup/create-es-indices.sh
```

---

## OpenBao Setup (Optional)

The interactive installer can install the OpenBao CLI automatically. You can also run it manually:

```bash
sudo bash setup/install-openbao.sh
```

After installation, verify:

```bash
openbao version
```

Recommended flow:
- Keep long-lived API tokens in OpenBao.
- Complete Flume install with defaults.
- Add/update provider keys after install from the Settings page and/or your OpenBao sync process.

---

## Multi-Machine Setup

Flume supports distributing agent roles across multiple machines.

Each machine runs the worker-manager independently. Agents are filtered to their
designated machine using the `EXECUTION_HOST` value in `.env`.

**Machine A** (e.g., intake/review/memory roles):
```bash
# .env
EXECUTION_HOST=machine-a
```

**Machine B** (e.g., implementer/tester roles, more compute):
```bash
# .env
EXECUTION_HOST=machine-b
```

All machines must point at the same Elasticsearch instance.

---

## Directory Layout at Runtime

After `install.sh`, the workspace root will contain:

```
flume/
├── .env                    ← Your configuration (never share this)
├── projects.json           ← Project registry
├── sequence_counters.json  ← ID counters
├── plan-sessions/          ← AI planning session state
└── worker-manager/
    ├── state.json          ← Worker heartbeat/status
    └── *.log               ← Worker logs
```

---

## Troubleshooting

**Dashboard won't start:**
- Check `python3 --version` is 3.9+
- Verify `.env` exists and `ES_API_KEY` is set
- Check Elasticsearch is running: `curl -sk https://localhost:9200/`

**Agents not picking up tasks:**
- Check `worker-manager/manager.log` for errors
- Verify `EXECUTION_HOST` in `.env` matches the host names in your worker config
- Confirm `LLM_PROVIDER` and `LLM_API_KEY` are correct

**Can't connect to Elasticsearch:**
- `systemctl status elasticsearch`
- `journalctl -u elasticsearch -f`
- Try regenerating the API key:
  ```bash
  curl -sk -u 'elastic:<password>' \
    -X POST 'https://localhost:9200/_security/api_key' \
    -H 'Content-Type: application/json' \
    -d '{"name":"flume","role_descriptors":{}}'
  ```

**PR creation fails:**
- Install GitHub CLI: https://cli.github.com/
- Authenticate: `gh auth login` or set `GH_TOKEN` in `.env`
