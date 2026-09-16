# ─────────────────────────────────────────────────────────────────────────────
# flume-gateway — multi-stage build for the Go LLM Gateway service.
# ─────────────────────────────────────────────────────────────────────────────

FROM golang:1.24.2-alpine AS builder

# hadolint ignore=DL3018
RUN apk add --no-cache git

WORKDIR /build

COPY go.mod go.sum ./
RUN go mod download

COPY src/gateway/ ./src/gateway/
COPY internal/logger/ ./internal/logger/
COPY internal/es/ ./internal/es/

RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags="-s -w" \
    -o /gateway \
    ./src/gateway/cmd/

FROM alpine:3.21

# hadolint ignore=DL3018
RUN apk add --no-cache ca-certificates wget \
    && adduser --disabled-password --uid 1000 --shell /bin/sh flume

COPY --from=builder /gateway /gateway
COPY src/gateway/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && chown flume:flume /gateway /entrypoint.sh

USER flume
EXPOSE 8090
ENTRYPOINT ["/entrypoint.sh"]
