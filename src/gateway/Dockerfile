# ─────────────────────────────────────────────────────────────────────────────
# flume-gateway — multi-stage build for the Go LLM Gateway service.
#
# Produces a ~15MB statically-linked binary with zero external dependencies.
# Alpine base provides wget for healthcheck without adding bloat.
# ─────────────────────────────────────────────────────────────────────────────

FROM golang:1.24.2-alpine AS builder

RUN apk add --no-cache git

WORKDIR /build

# Cache module downloads separately from source
COPY go.mod go.sum ./
RUN go mod download

# Copy gateway source
COPY src/gateway/ ./src/gateway/

# Build static binary
RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags="-s -w" \
    -o /gateway \
    ./src/gateway/cmd/

# ─────────────────────────────────────────────────────────────────────────────
# Runtime stage — zero-attack-surface distroless with the binary
# ─────────────────────────────────────────────────────────────────────────────

# We use alpine to ensure /bin/sh and wget are available for the docker-compose healthcheck.
FROM alpine:latest

COPY --from=builder /gateway /gateway

EXPOSE 8090

# Kubernetes probes use HTTP (GET /livez, GET /readyz) — no exec/wget needed.
ENTRYPOINT ["/gateway"]
