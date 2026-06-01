# ─────────────────────────────────────────────────────────────────────────────
# flume — unified multi-stage build for the single Go binary.
#
# Produces a ~20MB binary on golang:alpine with git for worker clone/push ops.
# Dashboard, gateway, and worker-manager all compile into one binary.
# Each Docker Compose service runs a different entrypoint command:
#   dashboard → /flume start --native (serves Vue SPA + API)
#   worker-N  → /flume worker          (claims and executes tasks)
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: Build the Vue dashboard SPA ────────────────────────────────────
FROM node:22-alpine AS frontend

WORKDIR /build
COPY src/frontend/src/package*.json ./
RUN npm ci --prefer-offline
COPY src/frontend/src/ ./
RUN npm run build

# ── Stage 2: Build the Go binary ────────────────────────────────────────────
FROM golang:1.24-alpine AS builder

# hadolint ignore=DL3018
RUN apk add --no-cache git ca-certificates

WORKDIR /src
COPY go.mod go.sum ./
COPY vendor-local/ vendor-local/
RUN go mod download

COPY . .

# Embed the pre-built SPA into the static assets directory
COPY --from=frontend /dist/ ./src/frontend/dist/

# Build a fully static binary (no cgo, no external deps)
RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags="-s -w -X main.version=$(git describe --tags --always 2>/dev/null || echo dev)" \
    -o /flume \
    ./cmd/flume

# ── Stage 3: Runtime (python-slim for reliable Elastro native extensions) ───
# Switched from Alpine to python:3.12-slim because elastro-client (v1.3.59+)
# vendors tree-sitter parsers (python/go/js/ts) with native extensions that
# fail to compile cleanly under Alpine/musl in many CI environments.
#
# python:3.12-slim (Debian) + temporary build-essential provides a robust,
# reproducible path while still allowing us to purge compilers from the final
# layer for a reasonably small worker image.
#
# Workers still get: git, sh, python toolchain for elastro + our Go binary.
#
# This unblocks the entire Phase 3.1 code-intelligence contract:
#   elastro_query_ast (preferred first tool for implementer/reviewer)
#   Project lifecycle: rag ingest at creation, rag update after edits.
FROM python:3.12-slim

# Minimal runtime OS packages (no compilers here — added temporarily only for elastro).
# wget + curl are required for the docker-compose healthchecks on gateway + dashboard
# (see docker-compose.yml: wget --spider on /health and /api/health).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates tzdata wget curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /bin/sh flume

# Create /app directory and set ownership early
RUN mkdir -p /app && chown -R flume:flume /app

# ── Install Elastro for worker code-intelligence tools (elastro_query_ast) ──
# This is **critical production infrastructure** for Grok-class agents.
#
# PyPI distribution name: elastro-client
#   pip install elastro-client     # provides the `elastro` CLI entrypoint
#
# GitHub release (preferred for exact reproducibility):
#   https://github.com/Fremen-Labs/elastro/releases/tag/v1.3.59
#   Direct wheel: https://github.com/Fremen-Labs/elastro/releases/download/v1.3.59/elastro_client-1.3.59-py3-none-any.whl
#
# Agents (implementer/reviewer) depend on `elastro_query_ast` for high-quality
# structural/semantic RAG. The binary MUST be present or code intelligence is
# completely broken (the exact failure mode observed post Python→Go migration).
#
# === RECOMMENDED (reproducible, no compile in your build) ===
#   # Download once:
#   curl -L -o elastro_client-1.3.59-py3-none-any.whl \
#     https://github.com/Fremen-Labs/elastro/releases/download/v1.3.59/elastro_client-1.3.59-py3-none-any.whl
#   docker build --build-arg ELASTR0_INSTALL=wheel -t flume-worker .
#
# Or let the build compile it (robust on debian-slim):
#   docker build --build-arg ELASTR0_INSTALL=public -t flume-worker .
#
# Private index or wheel also supported. The build HARD FAILS if requested
# but the resulting /opt/venv/bin/elastro is missing or non-functional.
ARG ELASTR0_INSTALL=public
ARG ELASTR0_PIP_INDEX_URL=""

# Create isolated venv (even on python-slim base) so we control exact packages
# and can keep the final layer free of build tools.
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip

# ── LogLoom CLI (for dashboard-side project AST ingestion) ────────────────────
# The `logloom` binary (used by runLogloomGraphIngest) is **not** reliably
# available via public `pip install logloom` from a clean python:3.12-slim
# environment (it appears to be a Fremen-Labs internal / private distribution
# at the moment, similar to the original "elastro" vs "elastro-client" issue).
#
# We therefore do **not** attempt to pip-install it here. This keeps the
# unified image buildable for everyone.
#
# Current behavior:
#   - Native mode (`flume start --native`): Uses $HOME/.local/bin/logloom (or LOGLOOM_BIN)
#   - Container mode: LogLoom AST ingest is gracefully skipped (best-effort, non-fatal)
#
# Discovery logic in api_projects.go + doctor.go already checks:
#   - $LOGLOOM_BIN
#   - $HOME/.local/bin/logloom
#   - /opt/venv/bin/logloom   (ready for when a reliable install method exists)
#
# When a public wheel, correct package name, or GitHub release becomes available,
# we will add a LOGLOOM_INSTALL=public|wheel|... build-arg (exactly like ELASTR0_INSTALL).
#
# For advanced users who have a working logloom wheel/sdist:
#   docker build --build-arg ... -t ... .
#   (then COPY the wheel into the context and pip install it in a custom stage)

# Install Elastro (with temporary compilers only for the native tree-sitter bits).
# We deliberately install build deps in this layer then purge them so the
# published worker image does not contain gcc etc.
RUN set -eux; \
    # Temporary compilers + headers for tree-sitter native extensions inside elastro-client
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential python3-dev; \
    \
    case "${ELASTR0_INSTALL}" in \
      auto) \
        if [ -n "${ELASTR0_PIP_INDEX_URL}" ]; then \
          echo "Installing Elastro from private index..."; \
          /opt/venv/bin/pip install --no-cache-dir --index-url "${ELASTR0_PIP_INDEX_URL}" elastro-client; \
        else \
          echo "Installing Elastro from public PyPI (elastro-client)..."; \
          /opt/venv/bin/pip install --no-cache-dir elastro-client; \
        fi \
        ;; \
      private) \
        if [ -z "${ELASTR0_PIP_INDEX_URL}" ]; then \
          echo "ERROR: ELASTR0_INSTALL=private requires ELASTR0_PIP_INDEX_URL"; exit 1; \
        fi; \
        /opt/venv/bin/pip install --no-cache-dir --index-url "${ELASTR0_PIP_INDEX_URL}" elastro-client; \
        ;; \
      public) \
        echo "Installing Elastro from public PyPI (elastro-client)..."; \
        /opt/venv/bin/pip install --no-cache-dir elastro-client; \
        ;; \
      wheel) \
        echo "Installing Elastro from local wheel (elastro_client-*.whl recommended from GitHub release)..."; \
        /opt/venv/bin/pip install --no-cache-dir /tmp/elastro_client-*.whl /tmp/elastro-*.whl; \
        ;; \
      skip) \
        echo "Skipping Elastro installation (ELASTR0_INSTALL=skip) — agents will lack code RAG"; \
        ;; \
      *) \
        echo "ERROR: Unknown ELASTR0_INSTALL value: ${ELASTR0_INSTALL}"; exit 1; \
        ;; \
    esac; \
    \
    # Purge compilers to keep the final worker image lean
    apt-get purge -y --auto-remove build-essential python3-dev; \
    rm -rf /var/lib/apt/lists/* /root/.cache /tmp/*.whl 2>/dev/null || true

# Hard assertion + functional smoke test. Fails the *build* (not just runtime)
# if the critical binary for code-aware agents is missing or broken.
# This is the single source of truth that elastro_query_ast will work inside workers.
RUN set -eux; \
    if [ "${ELASTR0_INSTALL}" != "skip" ]; then \
      if [ ! -x /opt/venv/bin/elastro ]; then \
        echo "FATAL: elastro binary not found at /opt/venv/bin/elastro after installation step."; \
        echo "       PyPI name is 'elastro-client'. GitHub: https://github.com/Fremen-Labs/elastro/releases/tag/v1.3.59"; \
        echo "       Rebuild with --build-arg ELASTR0_INSTALL=public (or =wheel after downloading the release wheel)."; \
        ls -la /opt/venv/bin/ || true; \
        exit 1; \
      fi; \
      # Functional verification — the CLI must actually start (catches packaging / entrypoint bugs)
      /opt/venv/bin/elastro --help >/dev/null 2>&1 || /opt/venv/bin/elastro --version >/dev/null 2>&1 || (echo "FATAL: elastro binary exists but is not executable/functional"; exit 1); \
      echo "SUCCESS: elastro binary present and functional at /opt/venv/bin/elastro"; \
    fi && \
    chown -R flume:flume /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"

COPY --from=builder /flume /usr/local/bin/flume

# Copy pre-built frontend dist for dashboard serving
COPY --from=frontend --chown=flume:flume /dist/ /app/frontend/dist/

# LogLoom graph for runtime enrichment (optional — zero-overhead if missing)
COPY --from=builder --chown=flume:flume /src/logloom-graph.json /app/logloom-graph.json

# Agent system prompts consumed by the worker manager's LLM subsystem
COPY --from=builder --chown=flume:flume /src/src/agents/ /app/agents/

ENV LOGLOOM_GRAPH_PATH=/app/logloom-graph.json
ENV FLUME_AGENTS_DIR=/app/agents
ENV FLUME_STATIC_ROOT=/app/frontend/dist

WORKDIR /app
USER flume

EXPOSE 8090 8765

ENTRYPOINT ["flume"]
CMD ["start", "--native"]
