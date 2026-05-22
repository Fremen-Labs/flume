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

RUN apk add --no-cache git ca-certificates

WORKDIR /src
COPY go.mod go.sum ./
COPY vendor-local/ vendor-local/
RUN go mod download

COPY . .

# Embed the pre-built SPA into the static assets directory
COPY --from=frontend /build/dist/ ./src/frontend/dist/

# Build a fully static binary (no cgo, no external deps)
RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags="-s -w -X main.version=$(git describe --tags --always 2>/dev/null || echo dev)" \
    -o /flume \
    ./cmd/flume

# ── Stage 3: Minimal Alpine runtime ────────────────────────────────────────
# Using Alpine instead of distroless because workers need:
#   - git: clone/push operations on work repos
#   - sh:  health check scripts in docker-compose
FROM alpine:3.21

RUN apk add --no-cache git ca-certificates tzdata && \
    adduser -D -u 1000 flume

COPY --from=builder /flume /usr/local/bin/flume

# Copy pre-built frontend dist for dashboard serving
COPY --from=frontend /build/dist/ /app/frontend/dist/

# LogLoom graph for runtime enrichment (optional — zero-overhead if missing)
COPY --from=builder /src/logloom-graph.json /app/logloom-graph.json

# Agent system prompts consumed by the worker manager's LLM subsystem
COPY --from=builder /src/src/agents/ /app/agents/

ENV LOGLOOM_GRAPH_PATH=/app/logloom-graph.json
ENV FLUME_AGENTS_DIR=/app/agents
ENV FLUME_STATIC_ROOT=/app/frontend/dist

WORKDIR /app
USER flume

EXPOSE 8090 8765

ENTRYPOINT ["flume"]
CMD ["start", "--native"]
