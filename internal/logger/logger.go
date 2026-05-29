package logger

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/Fremen-Labs/flume/internal/es"
	"github.com/Fremen-Labs/logloom-go/logloom"
)

// sensitiveFragments are substrings that trigger automatic redaction.
var sensitiveFragments = []string{"key", "token", "secret", "password", "pat", "credential"}

// maskedValue replaces secret values in log output.
const maskedValue = "***REDACTED***"

// contextKey is used for per-request logger context.
type contextKey string

const requestLoggerKey contextKey = "flume_logger"

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
	globalLevel   = &slog.LevelVar{} // Atomic level for runtime updates
)

// ConsoleHandler is a beautiful, colorized text handler for local development.
type ConsoleHandler struct {
	inner slog.Handler
}

func (h *ConsoleHandler) Enabled(ctx context.Context, l slog.Level) bool {
	return h.inner.Enabled(ctx, l)
}
func (h *ConsoleHandler) WithAttrs(as []slog.Attr) slog.Handler {
	return &ConsoleHandler{inner: h.inner.WithAttrs(as)}
}
func (h *ConsoleHandler) WithGroup(n string) slog.Handler {
	return &ConsoleHandler{inner: h.inner.WithGroup(n)}
}

func (h *ConsoleHandler) Handle(ctx context.Context, r slog.Record) error {
	level := r.Level.String()
	color := "\033[0m"
	switch r.Level {
	case slog.LevelDebug:
		level = "DBG"
		color = "\033[94m"
	case slog.LevelInfo:
		level = "INF"
		color = "\033[92m"
	case slog.LevelWarn:
		level = "WRN"
		color = "\033[93m"
	case slog.LevelError:
		level = "ERR"
		color = "\033[91m"
	}

	attrs := make(map[string]interface{})
	r.Attrs(func(a slog.Attr) bool {
		attrs[a.Key] = a.Value.Any()
		return true
	})

	attrStr := ""
	if len(attrs) > 0 {
		b, _ := json.Marshal(attrs)
		attrStr = " " + string(b)
	}

	// [LVL] HH:MM:SS entrypoint - Message {"key":"val"}
	os.Stdout.WriteString(fmt.Sprintf("%s[%s] %s %s%s\033[0m\n",
		color, level, r.Time.Format("15:04:05"), r.Message, attrStr))
	return nil
}

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

// SetLogLevel updates the global log level at runtime.
func SetLogLevel(levelStr string) {
	newLevel := parseLogLevel(levelStr)
	globalLevel.Set(newLevel)
	Log().Info("log level updated", slog.String("new_level", newLevel.String()))
}

// InitLogger initializes the global logger. Safe to call multiple
// times; only the first call takes effect.
func InitLogger() *slog.Logger {
	loggerOnce.Do(func() {
		initialLevel := parseLogLevel(os.Getenv("FLUME_GATEWAY_LOG_LEVEL"))
		globalLevel.Set(initialLevel)

		jsonMode := os.Getenv("FLUME_JSON_LOGS") != "false"
		var baseHandler slog.Handler

		if jsonMode {
			baseHandler = slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
				Level:     globalLevel,
				AddSource: initialLevel == slog.LevelDebug,
			})
		} else {
			// Fake handler to reuse the Level logic, but ConsoleHandler intercepts Handle
			baseHandler = slog.NewTextHandler(io.Discard, &slog.HandlerOptions{Level: globalLevel})
			baseHandler = &ConsoleHandler{inner: baseHandler}
		}

		// Handler chain: secureHandler → logloom → baseHandler
		defaultLogger = slog.New(&secureHandler{inner: logloom.NewHandler(baseHandler)})
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

// RequestLogger creates a child logger with per-request fields.
func RequestLogger(requestID, provider, model, agentRole string) *slog.Logger {
	return Log().With(
		slog.String("request_id", requestID),
		slog.String("provider", provider),
		slog.String("model", model),
		slog.String("agent_role", agentRole),
	)
}

// LogDuration logs the duration of an operation.
func LogDuration(ctx context.Context, operation string) func() {
	start := time.Now()
	return func() {
		WithContext(ctx).Info("operation completed",
			slog.String("operation", operation),
			slog.Float64("duration_ms", float64(time.Since(start).Microseconds())/1000.0),
		)
	}
}

// NewTestLogger creates a logger that writes to the provided writer. Useful
// for capturing log output in tests.
func NewTestLogger(w io.Writer) *slog.Logger {
	h := slog.NewJSONHandler(w, &slog.HandlerOptions{Level: slog.LevelDebug})
	return slog.New(&secureHandler{inner: h})
}

// TaskLogger returns a logger pre-enriched with task context.
// It attempts to attach Logloom node information when available.
func TaskLogger(ctx context.Context, taskID string, extra ...any) *slog.Logger {
	attrs := []any{
		slog.String("task_id", taskID),
	}
	attrs = append(attrs, extra...)

	l := WithContext(ctx).With(attrs...)

	// If the current task has an associated ll_node (from Logloom graph),
	// it can be injected here in the future via task metadata.
	return l
}

// ─── Execution Thought Bridge (Phase 0 Reasoning Persistence) ─────────────────
//
// Best-effort, non-blocking append of agent reasoning / state transitions into
// the task document via the existing "flume-append-execution-thought" Painless
// script (seeded at bootstrap in orchestrator/elastic.go).
//
// Design requirements (from grok-design-doc):
//   - 2s context timeout, never blocks the calling worker goroutine.
//   - On failure/timeout: emit structured "reasoning_bridge_failure" log (rich
//     attrs for Logloom) + increment conceptual metric. **Never silent** — the
//     reasoning text is already captured by the preceding slog + Logloom handler.
//   - Wire both LogAgentReasoning and LogStateTransition (the primary sources
//     of 20+ call sites in runner.go).

var bridgeES *es.Client

// SetESBridge wires the Elasticsearch client for the reasoning persistence bridge.
// Called once at worker/dashboard startup (e.g. in manager or server init).
// Safe to call with nil (bridge becomes a no-op).
func SetESBridge(c *es.Client) {
	bridgeES = c
}

func appendExecutionThoughtNonBlocking(ctx context.Context, taskID, agentRole, text string, meta map[string]any) {
	if bridgeES == nil || taskID == "" {
		return
	}

	// Launch in goroutine so the worker is never blocked (design: non-blocking).
	go func() {
		bctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()

		entry := map[string]interface{}{
			"ts":         time.Now().UTC().Format(time.RFC3339),
			"agent_role": agentRole,
			"thought":    text,  // Must be "thought" — frontend ThoughtEntry interface expects this key
			"meta":       meta,
		}
		params := map[string]interface{}{
			"entry": entry,
			"touch": time.Now().UTC().Format(time.RFC3339),
		}

		// Quick hardening (Phase 0+): client-side retry on conflict + server-side
		// retry_on_conflict (via the ES client helper). This dramatically increases
		// the chance that thoughts land even under heavy concurrent updates
		// (sweeper + multiple workers + status transitions).
		var lastErr error
		const maxAttempts = 4
		for attempt := 0; attempt < maxAttempts; attempt++ {
			err := bridgeES.UpdateDocByScript(bctx, "agent-task-records", taskID, "flume-append-execution-thought", params)
			if err == nil {
				return // success
			}
			lastErr = err

			// Only retry on version conflicts (the dominant case we saw in practice)
			if strings.Contains(err.Error(), "version_conflict") || strings.Contains(err.Error(), "409") {
				backoff := time.Duration(30*(attempt+1)) * time.Millisecond
				time.Sleep(backoff)
				continue
			}
			// Non-conflict errors (network, 404, etc.) — no point retrying hard
			break
		}

		if lastErr != nil {
			// Mandatory: never silent. Rich structured log feeds Logloom + dashboards.
			Log().Warn("reasoning_bridge_failure",
				slog.String("task_id", taskID),
				slog.String("agent_role", agentRole),
				slog.String("error", lastErr.Error()),
				slog.String("script", "flume-append-execution-thought"),
				slog.String("reasoning_preview", firstNChars(text, 120)),
				slog.Int("attempts", maxAttempts),
			)
			// The original reasoning text is already present in the preceding
			// slog.Info("agent reasoning"...) line (captured by Logloom handler).
		}
	}()
}

func firstNChars(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

// LogStateTransition is a convenience helper for the work queue state machine.
// It ensures consistent structured logging for all planned → ready → in_progress etc. transitions.
func LogStateTransition(ctx context.Context, taskID, fromStatus, toStatus string, reason string, extra ...any) {
	attrs := []any{
		slog.String("task_id", taskID),
		slog.String("from_status", fromStatus),
		slog.String("to_status", toStatus),
		slog.String("reason", reason),
	}
	attrs = append(attrs, extra...)
	TaskLogger(ctx, taskID).Info("task state transition", attrs...)

	// Phase 0 bridge: best-effort persistence so UI drawers are populated.
	meta := map[string]any{"from": fromStatus, "to": toStatus}
	appendExecutionThoughtNonBlocking(ctx, taskID, "system", reason, meta)
}

// LogAgentReasoning captures the agent's internal reasoning / thoughts.
// This is what should feed the "agent reasoning popout" in the work queue UI.
//
// Phase 0 change: after emitting the Logloom-enriched slog line, we also
// fire a best-effort non-blocking append to the task's execution_thoughts[]
// via the stored Painless script. This fixes the "empty reasoning in UI"
// symptom reported in the 258-item explosion post-mortem.
func LogAgentReasoning(ctx context.Context, taskID, agentRole, reasoning string, metadata ...any) {
	attrs := []any{
		slog.String("task_id", taskID),
		slog.String("agent_role", agentRole),
		slog.String("reasoning", reasoning),
	}
	attrs = append(attrs, metadata...)
	TaskLogger(ctx, taskID).Info("agent reasoning", attrs...)

	// Convert variadic metadata to map for the ES entry (best-effort).
	// Callers pass either map[string]any (preferred) or key/value pairs.
	meta := map[string]any{}
	for _, m := range metadata {
		switch v := m.(type) {
		case map[string]any:
			for k, val := range v {
				meta[k] = val
			}
		default:
			// Skip non-map entries (e.g. slog.Attr from legacy callers)
		}
	}
	appendExecutionThoughtNonBlocking(ctx, taskID, agentRole, reasoning, meta)
}

// LogLLMCall logs details of an LLM invocation with proper structure.
func LogLLMCall(ctx context.Context, taskID, provider, model string, promptTokens, completionTokens int, durationMs float64, err error, extra ...any) {
	attrs := []any{
		slog.String("task_id", taskID),
		slog.String("provider", provider),
		slog.String("model", model),
		slog.Int("prompt_tokens", promptTokens),
		slog.Int("completion_tokens", completionTokens),
		slog.Float64("duration_ms", durationMs),
	}
	if err != nil {
		attrs = append(attrs, slog.String("error", err.Error()))
	}
	attrs = append(attrs, extra...)
	level := slog.LevelInfo
	if err != nil {
		level = slog.LevelError
	}
	TaskLogger(ctx, taskID).Log(ctx, level, "llm call", attrs...)
}
