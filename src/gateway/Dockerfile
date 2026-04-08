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
# Runtime stage — minimal Alpine with the binary
# ─────────────────────────────────────────────────────────────────────────────

FROM alpine:3.19

# wget is used by the Docker healthcheck
RUN apk add --no-cache ca-certificates wget

COPY --from=builder /gateway /usr/local/bin/gateway

EXPOSE 8090

ENTRYPOINT ["gateway"]
