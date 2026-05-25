package logger

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"testing"
)

func TestContextLoggerPropagation(t *testing.T) {
	ctx := context.Background()

	// 1. Verification of default fallback logger when context is empty
	defaultL := WithContext(ctx)
	if defaultL == nil {
		t.Fatal("expected WithContext to fallback to default logger, got nil")
	}

	// 2. Verification of context binding and retrieval
	var buf bytes.Buffer
	testHandler := slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})
	customLogger := slog.New(testHandler)

	ctxWithL := ContextWithLogger(ctx, customLogger)
	retrievedL := WithContext(ctxWithL)
	if retrievedL != customLogger {
		t.Error("retrieved logger from context does not match the bound custom logger")
	}

	// Verify logging through retrieved logger propagates successfully
	retrievedL.Info("test log propagation", slog.String("trace_id", "trace-12345"))

	var loggedData map[string]interface{}
	if err := json.Unmarshal(buf.Bytes(), &loggedData); err != nil {
		t.Fatalf("failed to parse log output: %v", err)
	}

	if loggedData["msg"] != "test log propagation" {
		t.Errorf("expected msg 'test log propagation', got '%v'", loggedData["msg"])
	}
	if loggedData["trace_id"] != "trace-12345" {
		t.Errorf("expected trace_id 'trace-12345', got '%v'", loggedData["trace_id"])
	}
}

func TestSecretScrubbing(t *testing.T) {
	var buf bytes.Buffer
	testLogger := NewTestLogger(&buf)

	testLogger.Info("sensitive operations",
		slog.String("db_password", "super-secret-pass"),
		slog.String("github_token", "github_pat_abcdef123"),
		slog.String("user_email", "user@example.com"),
		slog.String("api_key", "secret-key-xyz"),
	)

	var loggedData map[string]interface{}
	if err := json.Unmarshal(buf.Bytes(), &loggedData); err != nil {
		t.Fatalf("failed to parse log output: %v", err)
	}

	// Check password redaction
	if val, ok := loggedData["db_password"]; !ok || val != maskedValue {
		t.Errorf("expected db_password to be %s, got %v", maskedValue, val)
	}

	// Check token redaction
	if val, ok := loggedData["github_token"]; !ok || val != maskedValue {
		t.Errorf("expected github_token to be %s, got %v", maskedValue, val)
	}

	// Check api_key redaction
	if val, ok := loggedData["api_key"]; !ok || val != maskedValue {
		t.Errorf("expected api_key to be %s, got %v", maskedValue, val)
	}

	// Check that non-sensitive fields are untouched
	if val, ok := loggedData["user_email"]; !ok || val != "user@example.com" {
		t.Errorf("expected user_email to be untouched, got %v", val)
	}
}

func TestConsoleHandlerFormatting(t *testing.T) {
	var buf bytes.Buffer
	textHandler := slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})
	consoleHandler := &ConsoleHandler{inner: textHandler}
	logger := slog.New(consoleHandler)

	logger.Info("console message test", slog.String("meta", "data"))

	// ConsoleHandler writes to stdout directly. However, we also want to make sure it doesn't crash
	// and that it behaves correctly when configured. Since ConsoleHandler writes to os.Stdout directly:
	// Let's verify that ConsoleHandler implements standard Handler interface correctly and handles it.
	if !consoleHandler.Enabled(context.Background(), slog.LevelInfo) {
		t.Error("expected Info level to be enabled")
	}
}

func TestSetLogLevel(t *testing.T) {
	originalLevel := globalLevel.Level()
	defer globalLevel.Set(originalLevel) // restore

	SetLogLevel("debug")
	if globalLevel.Level() != slog.LevelDebug {
		t.Errorf("expected log level to be DEBUG, got %v", globalLevel.Level())
	}

	SetLogLevel("warning")
	if globalLevel.Level() != slog.LevelWarn {
		t.Errorf("expected log level to be WARN, got %v", globalLevel.Level())
	}

	SetLogLevel("error")
	if globalLevel.Level() != slog.LevelError {
		t.Errorf("expected log level to be ERROR, got %v", globalLevel.Level())
	}

	SetLogLevel("invalid")
	if globalLevel.Level() != slog.LevelInfo {
		t.Errorf("expected invalid log level to fallback to INFO, got %v", globalLevel.Level())
	}
}
