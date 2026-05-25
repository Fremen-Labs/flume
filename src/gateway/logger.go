package gateway

import (
	"context"
	"io"
	"log/slog"

	"github.com/Fremen-Labs/flume/internal/logger"
)

// InitLogger initializes the global gateway logger. Delegated to internal/logger.
func InitLogger() *slog.Logger {
	return logger.InitLogger()
}

// Log returns the package-level logger. Delegated to internal/logger.
func Log() *slog.Logger {
	return logger.Log()
}

// WithContext returns a logger enriched with per-request fields. Delegated to internal/logger.
func WithContext(ctx context.Context) *slog.Logger {
	return logger.WithContext(ctx)
}

// ContextWithLogger stores a logger in the context. Delegated to internal/logger.
func ContextWithLogger(ctx context.Context, l *slog.Logger) context.Context {
	return logger.ContextWithLogger(ctx, l)
}

// RequestLogger creates a child logger with per-request fields. Delegated to internal/logger.
func RequestLogger(requestID, provider, model, agentRole string) *slog.Logger {
	return logger.RequestLogger(requestID, provider, model, agentRole)
}

// LogDuration logs the duration of an operation. Delegated to internal/logger.
func LogDuration(ctx context.Context, operation string) func() {
	return logger.LogDuration(ctx, operation)
}

// SetLogLevel updates the gateway's global log level at runtime. Delegated to internal/logger.
func SetLogLevel(levelStr string) {
	logger.SetLogLevel(levelStr)
}

// NewTestLogger creates a logger that writes to the provided writer. Delegated to internal/logger.
func NewTestLogger(w io.Writer) *slog.Logger {
	return logger.NewTestLogger(w)
}
