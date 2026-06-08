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
# but the resulting /opt/venv/bin/elastro (or logloom) is missing or non-functional.
# Defaults: both ELASTR0_INSTALL=public and LOGLOOM_INSTALL=public (for logloom,
# "public" auto-fetches the GitHub wheel since no suitable PyPI package exists).
ARG ELASTR0_INSTALL=public
ARG ELASTR0_PIP_INDEX_URL=""

# LogLoom CLI for dashboard-side project AST graph generation + ES shipping
# during "Plan New Work" / project creation + clone.
# NOTE: There is no "logloom" package published on PyPI under that name (the
# public path therefore downloads the wheel from the GitHub release for
# convenience and reproducibility). 
# For a specific/local wheel: use --build-arg LOGLOOM_INSTALL=wheel after placing
# logloom-*.whl in the build context (see GitHub release v0.3.7).
ARG LOGLOOM_INSTALL=public

# Create isolated venv (even on python-slim base) so we control exact packages
# and can keep the final layer free of build tools.
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip

# ── LogLoom CLI (for dashboard-side project AST ingestion on "Plan New Work") ─
# LogLoom provides rich structural AST (call graph, signatures, models, tags, etc.)
# and is pushed to `flume-logloom-ast`. It complements Elastro RAG.
#
# Installation is controlled by LOGLOOM_INSTALL (modeled after ELASTR0_INSTALL).
#
# Default (public): auto-downloads the wheel from the GitHub release (no PyPI
# package named "logloom" exists for `pip install logloom`).
#   docker build -t flume .
#
# For a specific version / airgapped / reproducible (recommended for CI):
#   curl -L -o logloom-0.3.7-py3-none-any.whl \
#     https://github.com/Fremen-Labs/logloom/releases/download/v0.3.7/logloom-0.3.7-py3-none-any.whl
#   docker build --build-arg LOGLOOM_INSTALL=wheel -t flume .
#
# The binary will be at /opt/venv/bin/logloom and available to the dashboard
# during project clone (so both Elastro + LogLoom AST indexes get populated).

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
    # LogLoom CLI (v0.3.7+) — required for rich structural AST + ES ship on "Plan New Work"
    case "${LOGLOOM_INSTALL}" in \
      wheel) \
        echo "Installing LogLoom from local wheel (https://github.com/Fremen-Labs/logloom/releases/tag/v0.3.7)..."; \
        pip_logloom_whl=$(ls /tmp/logloom-*.whl 2>/dev/null | head -1 || true); \
        if [ -z "$pip_logloom_whl" ]; then echo "ERROR: expected logloom-*.whl in /tmp for LOGLOOM_INSTALL=wheel"; ls -l /tmp/ || true; exit 1; fi; \
        /opt/venv/bin/pip install --no-cache-dir "${pip_logloom_whl}[build]"; \
        ;; \
      public|auto) \
        echo "Installing LogLoom from GitHub release wheel (public PyPI package does not exist under 'logloom' name; CLI requires [build] extra for click etc)..."; \
        curl -fL -o /tmp/logloom-0.3.7-py3-none-any.whl https://github.com/Fremen-Labs/logloom/releases/download/v0.3.7/logloom-0.3.7-py3-none-any.whl; \
        /opt/venv/bin/pip install --no-cache-dir "/tmp/logloom-0.3.7-py3-none-any.whl[build]"; \
        rm -f /tmp/logloom-0.3.7-py3-none-any.whl; \
        ;; \
      skip) \
        echo "Skipping LogLoom installation (LOGLOOM_INSTALL=skip) — LogLoom AST ingest unavailable in container mode"; \
        ;; \
      *) \
        echo "ERROR: Unknown LOGLOOM_INSTALL value: ${LOGLOOM_INSTALL}"; exit 1; \
        ;; \
    esac; \
    \
    # Purge compilers to keep the final worker image lean
    apt-get purge -y --auto-remove build-essential python3-dev; \
    rm -rf /var/lib/apt/lists/* /root/.cache /tmp/*.whl 2>/dev/null || true

# Hard assertion + functional smoke test. Fails the *build* (not just runtime)
# if the critical binaries for code intelligence are missing or broken.
# (Only runs the checks for the non-skipped ones; build args control installation.)
RUN set -eux; \
    if [ "${ELASTR0_INSTALL}" != "skip" ]; then \
      if [ ! -x /opt/venv/bin/elastro ]; then \
        echo "FATAL: elastro binary not found at /opt/venv/bin/elastro after installation step."; \
        echo "       PyPI name is 'elastro-client'. GitHub: https://github.com/Fremen-Labs/elastro/releases/tag/v1.3.59"; \
        echo "       Rebuild with --build-arg ELASTR0_INSTALL=public (or =wheel after downloading the release wheel)."; \
        ls -la /opt/venv/bin/ || true; \
        exit 1; \
      fi; \
      /opt/venv/bin/elastro --help >/dev/null 2>&1 || /opt/venv/bin/elastro --version >/dev/null 2>&1 || (echo "FATAL: elastro binary exists but is not executable/functional"; exit 1); \
      echo "SUCCESS: elastro binary present and functional at /opt/venv/bin/elastro"; \
    fi; \
    \
    if [ "${LOGLOOM_INSTALL}" != "skip" ]; then \
      if [ ! -x /opt/venv/bin/logloom ]; then \
        echo "FATAL: logloom binary not found at /opt/venv/bin/logloom after installation step."; \
        echo "       Public (default) auto-downloads the wheel from the release. For custom: --build-arg LOGLOOM_INSTALL=wheel after placing logloom-*.whl in build context (from https://github.com/Fremen-Labs/logloom/releases/tag/v0.3.7)."; \
        ls -la /opt/venv/bin/ || true; \
        exit 1; \
      fi; \
      /opt/venv/bin/logloom --help >/dev/null 2>&1 || /opt/venv/bin/logloom --version >/dev/null 2>&1 || (echo "FATAL: logloom binary exists but is not executable/functional"; exit 1); \
      echo "SUCCESS: logloom binary present and functional at /opt/venv/bin/logloom"; \
    fi && \
    chown -R flume:flume /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"

COPY --from=builder /flume /usr/local/bin/flume

# Copy pre-built frontend dist for dashboard serving
COPY --from=frontend --chown=flume:flume /dist/ /app/frontend/dist/

# Agent system prompts consumed by the worker manager's LLM subsystem
COPY --from=builder --chown=flume:flume /src/src/agents/ /app/agents/

ENV FLUME_AGENTS_DIR=/app/agents
ENV FLUME_STATIC_ROOT=/app/frontend/dist

WORKDIR /app
USER flume

EXPOSE 8090 8765

ENTRYPOINT ["flume"]
CMD ["start", "--native"]
