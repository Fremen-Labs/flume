# ─────────────────────────────────────────────────────────────────────────────
# flume — unified multi-stage build for the single Go binary.
#
# Phase 5: All application services (gateway, dashboard, worker-manager)
# compile into one statically-linked binary. The dashboard Vue SPA is
# pre-built and embedded as static assets.
#
# Produces a ~16MB binary on a distroless base with zero runtime deps.
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
RUN go mod download

COPY . .

# Embed the pre-built SPA into the static assets directory
COPY --from=frontend /build/dist/ ./src/frontend/dist/

# Build a fully static binary (no cgo, no external deps)
RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags="-s -w" \
    -o /flume \
    ./cmd/flume

# ── Stage 3: Minimal production image ───────────────────────────────────────
FROM gcr.io/distroless/static:nonroot

COPY --from=builder /flume /flume
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/

# Git is needed for worker clone/push operations — use a multi-stage
# copy from Alpine to keep the image minimal.
COPY --from=builder /usr/bin/git /usr/bin/git

EXPOSE 8090 8765 8080

ENTRYPOINT ["/flume"]
CMD ["start"]
