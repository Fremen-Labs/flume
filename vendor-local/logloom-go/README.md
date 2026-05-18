# LogLoom Go SDK

Official Go runtime bridge for [LogLoom](../../README.md) — enriches `log/slog` output with code-provenance metadata from the build-time AST graph.

## Installation

```bash
go get github.com/Fremen-Labs/logloom-go/logloom
```

## Usage

```go
import "github.com/Fremen-Labs/logloom-go/logloom"

// Wrap your existing handler — one line.
base := slog.NewJSONHandler(os.Stdout, nil)
logger := slog.New(logloom.NewHandler(base))

logger.Info("user login failed", slog.String("user_id", "123"))
```

**Output** (when `logloom-graph.json` is present):
```json
{
  "msg": "user login failed",
  "user_id": "123",
  "logloom_node_id": "ll:abc123def456",
  "logloom_module": "src/auth/service",
  "logloom_function": "authenticate",
  "logloom_tags": ["auth", "security"]
}
```

**Output** (when graph is missing — zero overhead):
```json
{
  "msg": "user login failed",
  "user_id": "123"
}
```

## Graph Discovery

The handler discovers `logloom-graph.json` automatically:

1. `LOGLOOM_GRAPH_PATH` env var (explicit path)
2. Walk up from CWD (max 10 levels)

Generate the graph with:
```bash
pip install logloom
logloom build --source . --languages go
```

## Supported Loggers

| Logger | Integration |
|---|---|
| `log/slog` (stdlib) | `logloom.NewHandler(inner)` — wraps any `slog.Handler` |
| `zerolog` | Planned |
| `zap` | Planned |
