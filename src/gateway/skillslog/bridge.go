// Package skillslog provides a thin logging bridge for the skills subsystem.
//
// This breaks the import cycle between gateway and skills: the skills package
// depends on this lightweight package instead of importing gateway directly.
// The gateway package initializes the bridge at startup by calling SetLogger().
package skillslog

import (
	"context"
	"log/slog"
	"os"
)

var defaultLogger *slog.Logger

func init() {
	// Fallback logger until the gateway calls SetLogger()
	defaultLogger = slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
}

// SetLogger is called by the gateway at startup to inject the real, secure logger.
func SetLogger(l *slog.Logger) {
	if l != nil {
		defaultLogger = l
	}
}

// Log returns the current package-level logger.
func Log() *slog.Logger {
	return defaultLogger
}

// WithContext returns a logger from context, or falls back to the default.
func WithContext(ctx context.Context) *slog.Logger {
	// Check for the gateway's context logger key
	if l, ok := ctx.Value(contextKey("gateway_logger")).(*slog.Logger); ok {
		return l
	}
	return defaultLogger
}

// ContextWithLogger stores a logger in context.
func ContextWithLogger(ctx context.Context, l *slog.Logger) context.Context {
	return context.WithValue(ctx, contextKey("gateway_logger"), l)
}

type contextKey string
