package gateway

import (
	"context"
	"io"
	"log/slog"
	"os"
	"strings"
	"sync"
	"time"
)

// ─────────────────────────────────────────────────────────────────────────────
// Structured JSON logger with automatic secret masking.
//
// Uses Go 1.21+ log/slog for sub-millisecond structured logging.  All output
// is JSON to stdout for Docker log aggregation (Elastic / Loki / CloudWatch).
//
// Secret fields (containing "key", "token", "secret", "password") are
// automatically redacted in log output to prevent credential leaks.
// ─────────────────────────────────────────────────────────────────────────────

// sensitiveFragments are substrings that trigger automatic redaction.
var sensitiveFragments = []string{"key", "token", "secret", "password", "pat", "credential"}

// maskedValue replaces secret values in log output.
const maskedValue = "***REDACTED***"

// contextKey is used for per-request logger context.
type contextKey string

const requestLoggerKey contextKey = "gateway_logger"

// secureHandler wraps an slog.Handler and redacts sensitive attribute values.
type secureHandler struct {
	inner slog.Handler
}

func (h *secureHandler) Enabled(ctx context.Context, level slog.Level) bool {
	return h.inner.Enabled(ctx, level)
}

func (h *secureHandler) Handle(ctx context.Context, r slog.Record) error {
	// Clone the record and scrub sensitive attrs
	cleaned := slog.NewRecord(r.Time, r.Level, r.Message, r.PC)
	r.Attrs(func(a slog.Attr) bool {
		cleaned.AddAttrs(scrubAttr(a))
		return true
	})
	return h.inner.Handle(ctx, cleaned)
}

func (h *secureHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	scrubbed := make([]slog.Attr, len(attrs))
	for i, a := range attrs {
		scrubbed[i] = scrubAttr(a)
	}
	return &secureHandler{inner: h.inner.WithAttrs(scrubbed)}
}

func (h *secureHandler) WithGroup(name string) slog.Handler {
	return &secureHandler{inner: h.inner.WithGroup(name)}
}

// scrubAttr redacts attribute values whose key name suggests a secret.
func scrubAttr(a slog.Attr) slog.Attr {
	keyLower := strings.ToLower(a.Key)
	for _, frag := range sensitiveFragments {
		if strings.Contains(keyLower, frag) {
			return slog.String(a.Key, maskedValue)
		}
	}
	// Recurse into groups
	if a.Value.Kind() == slog.KindGroup {
		attrs := a.Value.Group()
		scrubbed := make([]slog.Attr, len(attrs))
		for i, ga := range attrs {
			scrubbed[i] = scrubAttr(ga)
		}
		return slog.Attr{Key: a.Key, Value: slog.GroupValue(scrubbed...)}
	}
	return a
}

var (
	defaultLogger *slog.Logger
	loggerOnce    sync.Once
)

// parseLogLevel converts an environment string to slog.Level.
func parseLogLevel(s string) slog.Level {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}

// InitLogger initializes the global gateway logger.  Safe to call multiple
// times; only the first call takes effect.
func InitLogger() *slog.Logger {
	loggerOnce.Do(func() {
		level := parseLogLevel(os.Getenv("FLUME_GATEWAY_LOG_LEVEL"))
		jsonHandler := slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
			Level:     level,
			AddSource: level == slog.LevelDebug,
		})
		defaultLogger = slog.New(&secureHandler{inner: jsonHandler})
		slog.SetDefault(defaultLogger)
	})
	return defaultLogger
}

// Log returns the package-level logger (initializes on first call).
func Log() *slog.Logger {
	if defaultLogger == nil {
		return InitLogger()
	}
	return defaultLogger
}

// WithContext returns a logger enriched with per-request fields.
func WithContext(ctx context.Context) *slog.Logger {
	if l, ok := ctx.Value(requestLoggerKey).(*slog.Logger); ok {
		return l
	}
	return Log()
}

// ContextWithLogger stores a logger in the context for downstream handlers.
func ContextWithLogger(ctx context.Context, l *slog.Logger) context.Context {
	return context.WithValue(ctx, requestLoggerKey, l)
}

// RequestLogger creates a child logger with per-request fields.  Intended to
// be called at the start of each HTTP handler.
func RequestLogger(requestID, provider, model, agentRole string) *slog.Logger {
	return Log().With(
		slog.String("request_id", requestID),
		slog.String("provider", provider),
		slog.String("model", model),
		slog.String("agent_role", agentRole),
	)
}

// LogDuration logs the duration of an operation.  Usage:
//
//	defer LogDuration(ctx, "ollama_chat")()
func LogDuration(ctx context.Context, operation string) func() {
	start := time.Now()
	return func() {
		WithContext(ctx).Info("operation completed",
			slog.String("operation", operation),
			slog.Float64("duration_ms", float64(time.Since(start).Microseconds())/1000.0),
		)
	}
}

// NewTestLogger creates a logger that writes to the provided writer.  Useful
// for capturing log output in tests.
func NewTestLogger(w io.Writer) *slog.Logger {
	h := slog.NewJSONHandler(w, &slog.HandlerOptions{Level: slog.LevelDebug})
	return slog.New(&secureHandler{inner: h})
}
