# ─── Flume Unified Binary ────────────────────────────────────────────────────
# Multi-stage build producing a single statically-linked binary.
# Replaces the Python Dockerfile (dashboard + worker) and gateway Dockerfile.
#
# Build:  docker build -f Dockerfile.go -t flume:latest .
# Run:    docker run flume:latest start
# ─────────────────────────────────────────────────────────────────────────────

FROM golang:1.24-alpine AS builder

RUN apk add --no-cache git ca-certificates

WORKDIR /build

# Cache dependency download layer
COPY go.mod go.sum ./
COPY vendor-local/ vendor-local/
RUN go mod download

# Copy source and build
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags="-s -w -X main.version=$(git describe --tags --always 2>/dev/null || echo dev)" \
    -o /flume \
    ./cmd/flume

# ─── Runtime Image ───────────────────────────────────────────────────────────
FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=builder /flume /flume
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/

# LogLoom graph for runtime enrichment (optional — zero-overhead if missing)
COPY --from=builder /build/logloom-graph.json /logloom-graph.json

ENV LOGLOOM_GRAPH_PATH=/logloom-graph.json

EXPOSE 8090 8765

ENTRYPOINT ["/flume"]
CMD ["start"]
